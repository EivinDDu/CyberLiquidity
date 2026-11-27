"""Gridlock resolution (multilateral offsetting) and the priority-reserve treasury rule (0.3).

Hand-checkable examples for each rule in docs/MODEL.md sections 7.8-7.9, the accounting of
offsetting batches, digests pinned to the 0.2 engine, and the circular-trade fixture.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pytest
from pydantic import ValidationError

from cyberliquidity import simulate
from cyberliquidity.compare import compare
from cyberliquidity.engine import OPERATIONAL_REASONS
from cyberliquidity.explain import explain
from cyberliquidity.export import scenario_json
from cyberliquidity.metrics import summarize
from cyberliquidity.models import (
    Currency,
    ExternalParty,
    Obligation,
    OffsettingIntervention,
    Participant,
    PriorityReserveIntervention,
    Route,
    Scenario,
    Service,
)
from cyberliquidity.synthetic_cluster import build_cluster_scenario
from cyberliquidity.variants import (
    baseline,
    make_backup,
    make_buffer,
    make_credit_line,
    make_offsetting,
    make_reserve,
    replace,
    stress,
    with_intervention,
    with_settings,
)

ROOT = Path(__file__).resolve().parents[1]


def tiny(payments, balances, horizon=3, capacity=None, interventions=(), externals=("X",)):
    """Single-route test network; payments are (payer, recipient, amount, due_hour[, priority])."""
    obligations = []
    for i, p in enumerate(payments, 1):
        payer, recipient, amount, due = p[:4]
        priority = p[4] if len(p) > 4 else 3
        obligations.append(Obligation(id=f"o{i}", payer=payer, recipient=recipient, amount=amount, due_hour=due,
                                      priority=priority, routes=("r",)))
    return Scenario(
        id="tiny", name="Tiny test network", currency=Currency(code="XTS", minor_units=0), horizon_hours=horizon,
        services=(Service(id="s", name="Service", kind="other", capacity_per_hour=capacity),),
        routes=(Route(id="r", name="Route", path=("s",)),),
        participants=tuple(Participant(id=k, name=k, initial_balance=v) for k, v in balances.items()),
        external_parties=tuple(ExternalParty(id=x, name=x) for x in externals),
        obligations=tuple(obligations),
        interventions=tuple(interventions),
    )


def batches(result):
    return [e for e in result.events if e.type == "OFFSETTING_BATCH"]


# ------------------------------------------------------------------ offsetting


def test_a_pure_cycle_is_gridlocked_without_offsetting_and_settles_with_it():
    ring = [("A", "B", 100, 0), ("B", "C", 100, 0), ("C", "A", 100, 0)]
    plain = simulate(tiny(ring, {"A": 0, "B": 0, "C": 0}))
    assert all(o.settled_hour is None for o in plain.outcomes.values())
    r = simulate(tiny(ring, {"A": 0, "B": 0, "C": 0}, interventions=[make_offsetting()]))
    assert all(o.settled_hour == 0 and o.via_offsetting for o in r.outcomes.values())
    (batch,) = batches(r)
    assert batch.data["obligations"] == ["o1", "o2", "o3"]
    assert batch.data["gross_value"] == 300 and batch.data["net_value"] == 0 and batch.data["liquidity_saved"] == 300
    # every position nets to zero, so no cash moves at all
    assert not [e for e in r.ledger if e.kind.startswith("offset_")]
    m = summarize(r)
    assert m["settled_via_offsetting_count"] == 3 and m["offsetting_liquidity_saved"] == 300


def test_cash_moves_net_through_a_clearing_account_that_ends_empty():
    pair = [("A", "B", 100, 0), ("B", "A", 60, 0), ("B", "X", 30, 0)]
    r = simulate(tiny(pair, {"A": 50, "B": 0}, interventions=[make_offsetting()]))
    (batch,) = batches(r)
    assert batch.data["net_positions"] == {"A": -40, "B": 10}
    entries = [(e.kind, e.debit_account, e.credit_account, e.amount) for e in r.ledger]
    assert entries == [
        ("offset_debit", "A", "clearing:offsetting", 40),
        ("external_outflow", "clearing:offsetting", "X", 30),
        ("offset_credit", "clearing:offsetting", "B", 10),
    ]
    assert r.hours[0].balances == {"A": 10, "B": 10}
    assert r.totals["external_outflow"] == 30
    # each obligation is still discharged in full by exactly one SETTLEMENT event
    settled = Counter(e.obligation_id for e in r.events if e.type == "SETTLEMENT")
    assert settled == {"o1": 1, "o2": 1, "o3": 1}
    assert all(e.data["offsetting_batch"] == 1 for e in r.events if e.type == "SETTLEMENT")


def test_removal_takes_the_last_payment_of_the_largest_uncovered_participant():
    # Together A is 40 short; dropping its last queued payment (o2) leaves a covered pair.
    payments = [("A", "B", 100, 0), ("A", "C", 50, 0), ("B", "A", 90, 0)]
    r = simulate(tiny(payments, {"A": 20, "B": 0, "C": 0}, interventions=[make_offsetting()]))
    (batch,) = batches(r)
    assert batch.data["obligations"] == ["o1", "o3"]
    assert r.outcomes["o2"].settled_hour is None  # A has 10 left, less than 50
    assert r.hours[0].balances == {"A": 10, "B": 10, "C": 0}


def test_ties_go_to_the_lower_id_and_shared_capacity_is_respected():
    """Capacity 2 per hour: o1 and o2 are tried first; A and C are both 10 short (a tie).

    The rule removes A's payment first (lower id). C is then still short, its payment goes,
    and the remaining set never covers: no batch. (Removing C's payment first would have
    let o2 and o3 offset; the greedy rule does not search for the best set.) Without
    capacity limits the result cannot depend on the removal order, because each removal
    only improves the remover's own position.
    """
    payments = [("C", "B", 10, 0), ("A", "B", 20, 0), ("B", "A", 10, 0), ("C", "X", 20, 0)]
    r = simulate(tiny(payments, {"A": 10, "B": 0, "C": 0}, horizon=1, capacity=2, interventions=[make_offsetting()]))
    assert batches(r) == []
    used = Counter(e.hour for e in r.events if e.type == "SETTLEMENT")
    assert all(n <= 2 for n in used.values())
    # with ample capacity the same payments do offset (o1+o2+o3+o4 cannot: C would be 30 short)
    roomy = simulate(tiny(payments, {"A": 10, "B": 0, "C": 0}, horizon=1, interventions=[make_offsetting()]))
    assert [b.data["obligations"] for b in batches(roomy)] == [["o2", "o3"]]


def test_offsetting_never_settles_a_payment_whose_route_is_down():
    s = tiny([("A", "B", 100, 0), ("B", "A", 100, 0)], {"A": 0, "B": 0}, interventions=[make_offsetting()])
    s = replace(s, disruptions=(make_disruption_for_tiny(),))
    r = simulate(s)
    assert batches(r)[0].hour == 2  # the service is back in hour 2
    assert all(any(x in OPERATIONAL_REASONS for x in b.reasons) for b in r.blocks)


def make_disruption_for_tiny():
    from cyberliquidity.variants import make_disruption

    return make_disruption("s", 0, 2)


def test_offsetting_waits_for_its_activation_hour():
    ring = [("A", "B", 100, 0), ("B", "A", 100, 0)]
    r = simulate(tiny(ring, {"A": 0, "B": 0}, horizon=4, interventions=[make_offsetting(active_from_hour=2)]))
    assert [b.hour for b in batches(r)] == [2]
    assert r.outcomes["o1"].delay_hours == 2


def test_the_loop_after_a_batch_and_next_step_recycling_of_batch_proceeds():
    """P's last payment (o4) is removed first; then o2 goes too, and P ends the batch 10 up.

    o4 now fits, but only in a single settlement after the batch: under within-step
    recycling in the same hour, under next-step recycling (batch proceeds count as
    received this hour) one hour later.
    """
    payments = [("P", "Q", 20, 0, 1), ("P", "X", 100, 0, 2), ("Q", "P", 30, 0, 3), ("P", "X", 10, 0, 4)]
    base = tiny(payments, {"P": 0, "Q": 10}, interventions=[make_offsetting()])
    within = simulate(base)
    later = simulate(with_settings(base, liquidity_recycling="next_step"))
    for r in (within, later):
        assert [b.data["obligations"] for b in batches(r)] == [["o1", "o3"]]
        assert r.outcomes["o2"].settled_hour is None and not r.outcomes["o4"].via_offsetting
    assert within.outcomes["o4"].settled_hour == 0
    assert later.outcomes["o4"].settled_hour == 1


def test_a_net_payer_with_a_credit_line_draws_exactly_its_net_shortfall():
    payments = [("A", "B", 100, 0), ("B", "A", 60, 0)]
    r = simulate(tiny(payments, {"A": 10, "B": 0}, interventions=[make_offsetting(), make_credit_line(["A"], 50)]))
    (draw,) = [e for e in r.events if e.type == "CREDIT_DRAW"]
    assert draw.amount == 30 and draw.data["offsetting_batch"] == 1 and draw.obligation_id is None
    assert r.credit_peak == {"A": 30}


def test_offsetting_batches_reconcile_with_the_gross_obligations():
    s = build_cluster_scenario()
    r = simulate(with_intervention(stress(s), make_offsetting()))
    obligations = s.obligations_by_id()
    internal = s.internal_ids()
    for batch in batches(r):
        net = Counter()
        for oid in batch.data["obligations"]:
            ob = obligations[oid]
            net[ob.payer] -= ob.amount
            if ob.recipient in internal:
                net[ob.recipient] += ob.amount
        # a participant's net ledger movement equals its gross obligations in the batch
        # (payments to outside recipients are part of the payer's net debit)
        moved = Counter()
        for e in r.ledger:
            if e.event_id == batch.event_id and e.kind == "offset_debit":
                moved[e.debit_account] -= e.amount
            elif e.event_id == batch.event_id and e.kind == "offset_credit":
                moved[e.credit_account] += e.amount
        assert {k: v for k, v in net.items() if v} == {k: v for k, v in moved.items() if v}
        assert batch.data["net_positions"] == dict(sorted(net.items()))


# -------------------------------------------------------------------- reserve


def test_the_reserve_holds_a_routine_payment_to_protect_payroll():
    payments = [("A", "B", 50, 1), ("A", "X", 80, 5, 1)]  # invoice at hour 1, payroll (priority 1) at hour 5
    plain = simulate(tiny(payments, {"A": 100, "B": 0}, horizon=8))
    assert plain.outcomes["o1"].delay_hours == 0 and plain.outcomes["o2"].settled_hour is None
    r = simulate(tiny(payments, {"A": 100, "B": 0}, horizon=8, interventions=[make_reserve()]))
    assert r.outcomes["o2"].delay_hours == 0  # payroll protected ...
    assert r.outcomes["o1"].settled_hour is None  # ... at the invoice's expense
    held = [b for b in r.blocks if b.obligation_id == "o1"]
    assert held[0].held_by_reserve and held[0].reserved == 80 and held[0].reasons == ("INSUFFICIENT_CASH",)
    # after payroll the invoice is blocked by a real shortfall (20 < 50), not by the rule
    assert not held[-1].held_by_reserve and held[-1].reserved == 0
    assert summarize(r)["reserve_held_count"] == 1
    assert "held it back" in explain(r, "o1").chain_text()


def test_the_lookahead_window_limits_what_is_reserved():
    payments = [("A", "B", 50, 1), ("A", "X", 80, 5, 1)]
    r = simulate(tiny(payments, {"A": 100, "B": 0}, horizon=8, interventions=[make_reserve(lookahead_hours=2)]))
    assert r.outcomes["o1"].delay_hours == 0  # payroll at hour 5 is outside [1, 3)


def test_the_reserve_counts_undrawn_credit_and_only_covers_listed_participants():
    payments = [("A", "B", 50, 1), ("A", "X", 80, 5, 1), ("B", "C", 50, 1), ("B", "X", 80, 5, 1)]
    balances = {"A": 100, "B": 100, "C": 0}
    r = simulate(tiny(payments, balances, horizon=8, interventions=[make_reserve(["A"]), make_credit_line(["A"], 40)]))
    assert r.outcomes["o1"].delay_hours == 0  # 100 + 40 credit - 50 >= 80
    assert r.outcomes["o3"].delay_hours == 0  # B is not covered by the rule


def test_validation_of_the_new_interventions():
    s = tiny([("A", "B", 10, 0)], {"A": 10, "B": 0})
    with pytest.raises(ValidationError, match="at most one offsetting"):
        with_intervention(s, make_offsetting(id="x"), make_offsetting(id="y"))
    with pytest.raises(ValidationError, match="more than one applied reserve rule"):
        with_intervention(s, make_reserve(id="x"), make_reserve(["A"], id="y"))
    with pytest.raises(ValidationError, match="not a participant inside the accounting boundary"):
        with_intervention(s, make_reserve(["X"]))
    with pytest.raises(ValidationError, match="participant list is empty"):
        with_intervention(s, PriorityReserveIntervention(id="r", participants=()))
    with pytest.raises(ValidationError, match="outside the horizon"):
        with_intervention(s, OffsettingIntervention(id="o", active_from_hour=99))
    # candidates are alternatives, so overlapping reserve rules are allowed there
    replace(s, candidate_interventions=(make_reserve(id="x"), make_reserve(["A"], id="y")))


def test_the_backstop_rejects_a_reserve_rule():
    from cyberliquidity.sweep import liquidity_backstop

    s = tiny([("A", "B", 10, 0)], {"A": 10, "B": 0}, interventions=[make_reserve()])
    with pytest.raises(ValueError, match="priority-reserve"):
        liquidity_backstop(s)


# ------------------------------------------------------- regression and fixture


def test_v02_rules_are_unchanged():
    """Scenarios using only 0.2 features reproduce the 0.2 engine's events and ledger byte for byte."""
    golden = json.loads((ROOT / "tests" / "golden_v02_digests.json").read_text())
    default = Scenario.model_validate_json((ROOT / "scenarios" / "default_72h.json").read_text())
    three = Scenario.model_validate_json((ROOT / "scenarios" / "three_participant.json").read_text())
    everyone = [p.id for p in default.participants]
    producers = ["oakridge", "silverfern", "thistle", "copperleaf"]
    b = stress(default)
    cases = {
        "default_stress+credit_all_h0": with_intervention(b, make_credit_line(everyone, 400_000, 0, id="cl")),
        "default_stress+credit_all_h33": with_intervention(b, make_credit_line(everyone, 400_000, 33, id="cl")),
        "default_stress+credit_producers_next_step": with_intervention(
            with_settings(b, liquidity_recycling="next_step"), make_credit_line(producers, 1_000_000, 0, id="cl")),
        "default_stress+credit+buffer+backup": with_intervention(
            b, make_credit_line({"silverfern": 900_000, "oakridge": 300_000}, id="cl"),
            make_buffer(["thistle"], 500_000, id="buf"), make_backup(["alpha_north"], "beta_south", 35, id="bk")),
        "three_stress+credit_B": with_intervention(stress(three), make_credit_line(["B"], 100, 0, id="cl")),
        "three_stress+credit_B_small": with_intervention(stress(three), make_credit_line(["B"], 60, 0, id="cl")),
    }
    assert set(cases) == set(golden)
    for name, scenario in cases.items():
        assert simulate(scenario).digest(engine_version="0.2.0") == golden[name], name


def test_the_cluster_fixture_matches_its_generator():
    committed = (ROOT / "scenarios" / "cluster_gridlock_48h.json").read_text(encoding="utf-8")
    assert committed == scenario_json(build_cluster_scenario())


def test_the_cluster_scenario_shows_gridlock_and_its_resolution():
    s = build_cluster_scenario()
    assert summarize(simulate(baseline(s)))["delayed_count"] == 0
    c = compare(s)
    B = c.summaries["B"]
    lsm = c.summaries["C:offsetting"]
    reserve = c.summaries["C:reserve_payroll"]
    assert B["participants_cash_short_count"] == 8 and B["urgent_delayed_count"] == 8
    assert lsm["offsetting_batches"] == 1 and lsm["settled_via_offsetting_count"] == 7
    assert lsm["offsetting_liquidity_saved"] == 7_200_000  # EUR 72,000 of gross payments needed no cash
    assert lsm["overdue_value_hours"] < B["overdue_value_hours"]
    assert reserve["overdue_value_hours"] > B["overdue_value_hours"]  # protecting payroll makes it worse here
    # no gridlock in the default supply chain: nothing for offsetting to do
    default = Scenario.model_validate_json((ROOT / "scenarios" / "default_72h.json").read_text())
    r = simulate(with_intervention(stress(default), make_offsetting()))
    assert r.totals["offsetting_batches"] == 0
