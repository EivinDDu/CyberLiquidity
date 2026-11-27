"""Committed credit lines, the exact liquidity backstop, and backward compatibility with v0.1."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from cyberliquidity import Scenario, Status, simulate
from cyberliquidity.generator import random_scenario
from cyberliquidity.metrics import summarize
from cyberliquidity.models import (
    BufferAllocation,
    CreditLineIntervention,
    LiquidityBufferIntervention,
    PriorityReserveIntervention,
)
from cyberliquidity.sweep import liquidity_backstop
from cyberliquidity.variants import (
    baseline,
    cash_unconstrained,
    make_credit_line,
    make_disruption,
    replace,
    stress,
    with_intervention,
    with_settings,
)
from tests.conftest import ROOT, load, tiny


def _hand(limit: int, balance: int = 0, available_from: int = 0, inflow: bool = True, **settings) -> Scenario:
    """P owes outsider X 100 in hour 1 (priority 1); X pays P 100 in hour 1 (priority 2)."""
    obligations = [{"id": "out", "payer": "P", "recipient": "X", "amount": 100, "due_hour": 1, "priority": 1, "routes": ["r"]}]
    if inflow:
        obligations.append({"id": "in", "payer": "X", "recipient": "P", "amount": 100, "due_hour": 1, "priority": 2, "routes": ["r"]})
    data = tiny(obligations=obligations)
    data["participants"][0]["initial_balance"] = balance
    data["interventions"] = [{"type": "credit_line", "id": "cl", "available_from_hour": available_from,
                              "facilities": [{"participant": "P", "limit": limit}]}]
    if settings:
        data["settings"] = settings
    return Scenario.model_validate(data)


def test_draw_exactly_the_shortfall_and_repay_at_the_end_of_the_hour():
    r = simulate(_hand(limit=100, balance=30))
    types = [(e.type, e.obligation_id or e.participant_id, e.amount) for e in r.events if e.hour == 1]
    assert types == [("OBLIGATION_DUE", "out", 100), ("OBLIGATION_DUE", "in", 100), ("CREDIT_DRAW", "out", 70),
                     ("SETTLEMENT", "out", 100), ("SETTLEMENT", "in", 100), ("CREDIT_REPAYMENT", "P", 70)]
    kinds = [(x.kind, x.debit_account, x.credit_account, x.amount) for x in r.ledger]
    assert kinds == [("credit_draw", "credit:cl", "P", 70), ("external_outflow", "P", "X", 100),
                     ("external_inflow", "X", "P", 100), ("credit_repayment", "P", "credit:cl", 70)]
    assert r.outcomes["out"].status is Status.SETTLED_ON_TIME
    assert r.credit_peak == {"P": 70} and r.hours[1].credit_drawn == {"P": 0} and r.hours[1].balances["P"] == 30
    m = summarize(r)
    assert (m["credit_drawn_total"], m["credit_repaid_total"], m["credit_outstanding_at_horizon"]) == (70, 70, 0)
    assert m["conservation_ok"]


def test_a_binding_limit_does_not_draw_and_the_payment_waits_for_the_inflow():
    r = simulate(_hand(limit=60))
    order = [e.obligation_id for e in r.events if e.type == "SETTLEMENT"]
    assert order == ["in", "out"]  # 0 cash + 60 credit < 100: "out" waits until X's payment arrives in the hour
    assert r.credit_peak == {"P": 0} and not any(e.type == "CREDIT_DRAW" for e in r.events)
    assert r.outcomes["out"].status is Status.SETTLED_ON_TIME


def test_credit_is_unusable_before_it_is_available_and_outstanding_draws_stay_visible():
    r = simulate(_hand(limit=100, available_from=3, inflow=False))
    blocked = [b for b in r.blocks if b.obligation_id == "out"]
    assert [b.hour for b in blocked] == [1, 2] and all(b.reasons == ("INSUFFICIENT_CASH",) for b in blocked)
    assert all(b.available_credit == 0 for b in blocked)
    assert r.outcomes["out"].settled_hour == 3 and r.outcomes["out"].delay_hours == 2
    assert r.totals["credit_outstanding"] == 100 and summarize(r)["credit_outstanding_value_hours"] == 100 * 3


def test_next_step_recycling_keeps_received_cash_out_of_this_hour():
    # Under next_step the inflow cannot fund "out" in hour 1; the 60 limit is not enough either.
    r = simulate(_hand(limit=60, liquidity_recycling="next_step"))
    assert r.outcomes["out"].settled_hour == 2  # hour 2: the 100 received in hour 1 is now usable
    r = simulate(_hand(limit=100, liquidity_recycling="next_step"))
    assert r.outcomes["out"].settled_hour == 1 and r.credit_peak == {"P": 100}


def test_credit_line_validation():
    data = tiny()
    data["interventions"] = [{"type": "credit_line", "id": "a", "facilities": [{"participant": "X", "limit": 5}]},
                             {"type": "credit_line", "id": "b", "facilities": [{"participant": "P", "limit": 5}],
                              "available_from_hour": 99},
                             {"type": "credit_line", "id": "c", "facilities": [{"participant": "P", "limit": 5}]}]
    with pytest.raises(ValidationError) as exc:
        Scenario.model_validate(data)
    text = str(exc.value)
    assert "'X' is not a participant inside the accounting boundary" in text
    assert "available_from_hour 99 is outside the horizon" in text
    assert "participant 'P' is covered by more than one applied credit line" in text
    data["interventions"] = [{"type": "credit_line", "id": "d", "facilities": [{"participant": "P", "limit": 0}]}]
    with pytest.raises(ValidationError):
        Scenario.model_validate(data)


def test_credit_line_and_prefunded_buffer_give_identical_outcomes():
    """Model property (docs/MODEL.md 7.4): same limits, same hour -> same settlements, less liquidity used."""
    s = stress(load("default_72h.json"))
    everyone = [p.id for p in s.participants]
    for hour in (0, 33):
        credit = simulate(with_intervention(s, make_credit_line(everyone, 400_000, hour, id="cl")))
        buffer = simulate(with_intervention(s, LiquidityBufferIntervention(
            id="buf", funding_hour=hour, allocations=tuple(BufferAllocation(participant=p, amount=400_000) for p in everyone))))
        assert {k: o.settled_hour for k, o in credit.outcomes.items()} == {k: o.settled_hour for k, o in buffer.outcomes.items()}
        assert summarize(credit)["credit_peak_total"] < summarize(buffer)["intervention_funding"]
    for seed in range(60):
        sc = random_scenario(seed)
        lines = [iv for iv in sc.interventions if isinstance(iv, CreditLineIntervention)]
        if not lines:
            continue
        cl = lines[0]
        as_buffer = LiquidityBufferIntervention(id="as_buffer", funding_hour=cl.available_from_hour, allocations=tuple(
            BufferAllocation(participant=f.participant, amount=f.limit) for f in cl.facilities))
        other = replace(sc, interventions=tuple(iv for iv in sc.interventions if iv is not cl) + (as_buffer,))
        a, b = simulate(sc), simulate(other)
        assert {k: o.settled_hour for k, o in a.outcomes.items()} == {k: o.settled_hour for k, o in b.outcomes.items()}, seed


def test_backstop_on_the_hand_example(three):
    b = liquidity_backstop(stress(three))
    assert b.peaks == {"B": 100} and b.allocation == {"B": 100}
    assert b.verified_sufficient and b.all_tight
    assert b.route_limited_count == 1  # A -> B itself is blocked by the outage; liquidity cannot help it


@pytest.mark.parametrize("recycling", ["within_step", "next_step"])
def test_backstop_is_verified_sufficient_and_locally_smallest(recycling):
    s = with_settings(stress(load("default_72h.json")), liquidity_recycling=recycling)
    b = liquidity_backstop(s)
    assert b.verified_sufficient and b.all_tight
    assert all(b.allocation[p] <= b.peaks[p] for p in b.allocation)
    # Independent re-check of the claim, outside the function: the allocation reproduces the
    # unlimited-liquidity outcome, and one unit less for the largest allocation does not.
    reference = simulate(cash_unconstrained(s))
    funded = simulate(with_intervention(s, LiquidityBufferIntervention(id="alloc", allocations=tuple(
        BufferAllocation(participant=p, amount=v) for p, v in b.allocation.items()))))
    assert {k: o.settled_hour for k, o in funded.outcomes.items()} == {k: o.settled_hour for k, o in reference.outcomes.items()}
    largest = max(b.allocation, key=b.allocation.get)
    less = dict(b.allocation, **{largest: b.allocation[largest] - 1})
    worse = simulate(with_intervention(s, LiquidityBufferIntervention(id="alloc", allocations=tuple(
        BufferAllocation(participant=p, amount=v) for p, v in less.items() if v > 0))))
    assert {k: o.settled_hour for k, o in worse.outcomes.items()} != {k: o.settled_hour for k, o in reference.outcomes.items()} \
        or any(x.cash and not x.operational and x.payer == largest for x in worse.blocks)


def test_backstop_theorems_hold_on_random_scenarios():
    """Peaks always suffice; with next_step recycling they are also tight (docs/MODEL.md 7.5).

    Offsetting may stay in the scenario (sufficiency still follows: with non-binding credit no
    payment is ever cash-blocked, so no batch forms); a reserve rule is outside the argument
    and is rejected by the backstop (see the test below).
    """
    for seed in range(40):
        sc = random_scenario(seed, participants=10, obligations=90)
        sc = replace(sc, interventions=tuple(iv for iv in sc.interventions
                                             if not isinstance(iv, (CreditLineIntervention, PriorityReserveIntervention))))
        within = liquidity_backstop(with_settings(sc, liquidity_recycling="within_step"), refine=False)
        nxt = liquidity_backstop(with_settings(sc, liquidity_recycling="next_step"), refine=False)
        assert within.verified_sufficient and nxt.verified_sufficient, seed
        assert not nxt.not_tight, seed


def test_backstop_reports_when_liquidity_is_irrelevant():
    s = load("default_72h.json")
    keygate = stress(s, make_disruption("idp_keygate", 32, 12))
    b = liquidity_backstop(keygate)
    assert b.verified_sufficient and b.route_limited_count > 40
    assert b.total_allocation < b.total_peak / 5  # most credit-line peaks are not needed as pre-funding
    clean = liquidity_backstop(baseline(s))
    assert clean.allocation == {} and clean.verified_sufficient and "No pre-funding is needed" in clean.verdict(str)


def test_v01_rules_are_unchanged():
    """Scenarios using only v0.1 features reproduce the v0.1 engine's events and ledger byte for byte."""
    golden = json.loads((ROOT / "tests" / "golden_v01_digests.json").read_text())
    s, t = load("default_72h.json"), load("three_participant.json")
    from cyberliquidity.variants import with_intervention as wi
    cases = {
        "default_baseline": baseline(s),
        "default_stress": stress(s),
        **{f"default_stress+{iv.id}": wi(stress(s), iv) for iv in s.candidate_interventions},
        "default_keygate_10h": stress(s, make_disruption("idp_keygate", 30, 10, recovery_hours=3)),
        "default_fifo": with_settings(stress(s), ordering="due_id"),
        "default_next_step": with_settings(stress(s), liquidity_recycling="next_step"),
        "default_cash_reference": cash_unconstrained(stress(s)),
        "three_stress": stress(t),
        **{f"three_stress+{iv.id}": wi(stress(t), iv) for iv in t.candidate_interventions},
    }
    assert set(cases) == set(golden["digests"])
    for name, scenario in cases.items():
        assert simulate(scenario).digest(engine_version=golden["engine_version"]) == golden["digests"][name], name
