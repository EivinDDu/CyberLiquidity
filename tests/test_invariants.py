"""Accounting and operational invariants, checked independently of the engine's own guards.

Every test recomputes what it checks from the recorded ledger, events and service
status rather than trusting the engine's summary fields.
"""

from __future__ import annotations

from collections import Counter, defaultdict

import pytest

from cyberliquidity import Status, simulate
from cyberliquidity.compare import compare
from cyberliquidity.generator import random_scenario
from cyberliquidity.variants import (
    baseline,
    cash_unconstrained,
    make_backup,
    make_buffer,
    make_credit_line,
    make_disruption,
    make_faster_recovery,
    make_offsetting,
    make_reserve,
    stress,
    with_intervention,
    with_settings,
)
from tests.conftest import load


def _variants():
    s = load("default_72h.json")
    out = {"baseline": baseline(s), "stress": stress(s)}
    for iv in s.candidate_interventions:
        out[f"stress+{iv.id}"] = with_intervention(stress(s), iv)
    for svc, start, dur, sev in [("idp_keygate", 30, 10, 100), ("cloud_stratus", 32, 24, 100),
                                 ("portal_north", 31, 18, 100), ("psp_alpha", 20, 8, 60)]:
        out[f"{svc}"] = stress(s, make_disruption(svc, start, dur, severity_pct=sev, recovery_hours=3))
    out["keygate+backup"] = with_intervention(out["idp_keygate"], make_backup(["alpha_north"], "beta_south", 33))
    out["stratus+buffer+backup"] = with_intervention(
        out["cloud_stratus"], make_buffer(["silverfern", "thistle"], 500_000), make_backup(["beta_south"], "alpha_north", 35))
    out["portal+recovery"] = with_intervention(out["portal_north"], make_faster_recovery("D1", 9))
    out["fifo"] = with_settings(stress(s), ordering="due_id")
    out["next_step"] = with_settings(stress(s), liquidity_recycling="next_step")
    out["cash_reference"] = cash_unconstrained(stress(s))
    out["three_stress"] = stress(load("three_participant.json"))
    everyone = [p.id for p in s.participants]
    out["stress+credit"] = with_intervention(stress(s), make_credit_line(everyone, 500_000, 33))
    out["portal+credit+backup"] = with_intervention(
        out["portal_north"], make_credit_line(["meridian", "silverfern"], 3_000_000, 0),
        make_backup(["hv_north"], "beta_south", 34))
    out["next_step+credit"] = with_settings(out["stress+credit"], liquidity_recycling="next_step")
    for seed in (3, 11, 29, 47, 101, 202):
        out[f"random_{seed}"] = random_scenario(seed, participants=12, services=8, routes=4, obligations=160, horizon=30)
    cluster = load("cluster_gridlock_48h.json")
    out["cluster_stress"] = stress(cluster)
    out["cluster+offsetting"] = with_intervention(stress(cluster), make_offsetting())
    out["cluster+offsetting+credit"] = with_intervention(stress(cluster), make_offsetting(),
                                                         make_credit_line(["dunlin", "cobalt"], 100_000))
    out["cluster+offsetting+reserve"] = with_intervention(stress(cluster), make_offsetting(), make_reserve())
    out["cluster_next_step+offsetting"] = with_settings(out["cluster+offsetting"], liquidity_recycling="next_step")
    out["stress+reserve"] = with_intervention(stress(s), make_reserve(protected_priority=2))
    return out


VARIANTS = _variants()


@pytest.fixture(params=sorted(VARIANTS), scope="module")
def result(request):
    return simulate(VARIANTS[request.param])


def test_each_payment_settles_at_most_once(result):
    counts = Counter(e.obligation_id for e in result.events if e.type == "SETTLEMENT")
    assert all(n == 1 for n in counts.values())
    payment_kinds = {"internal_transfer", "external_inflow", "external_outflow"}
    ledger_refs = Counter(entry.reference for entry in result.ledger if entry.kind in payment_kinds)
    assert all(n == 1 for n in ledger_refs.values())
    # A payment settled inside an offsetting batch moves cash net (offset_debit / offset_credit
    # entries of the batch); only its payment to a recipient outside the boundary is a gross entry.
    internal = result.scenario.internal_ids()
    netted = {e.obligation_id for e in result.events
              if e.type == "SETTLEMENT" and "offsetting_batch" in e.data and e.data["recipient"] in internal}
    assert sum(ledger_refs.values()) == sum(counts.values()) - len(netted)
    assert not netted & set(ledger_refs)


def test_ledger_replay_reproduces_balances_and_debits_equal_credits(result):
    internal = result.scenario.internal_ids()
    balances = dict(result.initial_balances)
    by_hour = defaultdict(list)
    for entry in result.ledger:
        by_hour[entry.hour].append(entry)
    for snapshot in result.hours:
        for entry in by_hour[snapshot.hour]:
            assert entry.amount > 0
            if entry.debit_account in internal:
                balances[entry.debit_account] -= entry.amount
                assert balances[entry.debit_account] == entry.debit_balance_after
            if entry.credit_account in internal:
                balances[entry.credit_account] += entry.amount
                assert balances[entry.credit_account] == entry.credit_balance_after
            # an internal transfer moves the same amount out of one account and into another
            if entry.kind == "internal_transfer":
                assert entry.debit_account in internal and entry.credit_account in internal
        assert balances == snapshot.balances


def test_balances_never_negative(result):
    assert all(b >= 0 for snapshot in result.hours for b in snapshot.balances.values())


def test_cash_is_conserved_within_the_boundary(result):
    initial = sum(result.initial_balances.values())
    flows = Counter()
    by_hour = defaultdict(Counter)
    for entry in result.ledger:
        by_hour[entry.hour][entry.kind] += entry.amount
    for snapshot in result.hours:
        flows.update(by_hour[snapshot.hour])
        expected = (initial + flows["external_inflow"] - flows["external_outflow"] + flows["intervention_funding"]
                    + flows["credit_draw"] - flows["credit_repayment"])
        assert sum(snapshot.balances.values()) == expected
        # outstanding credit equals draws minus repayments so far, and never exceeds a facility's limit
        assert sum(snapshot.credit_drawn.values()) == flows["credit_draw"] - flows["credit_repayment"]
        limits = {c.participant: c.limit for c in result.credit_lines}
        assert all(0 <= v <= limits[p] for p, v in snapshot.credit_drawn.items())
    assert result.totals["intervention_funding"] == sum(f.amount for f in result.funding)


def test_shared_capacity_is_respected_across_all_users(result):
    paths = {r.id: r.path for r in result.scenario.routes}
    used = Counter()
    for e in result.events:
        if e.type == "SETTLEMENT":
            for sid in paths[e.data["route"]]:
                used[(e.hour, sid)] += 1
    for (hour, sid), count in used.items():
        capacity = result.service_status[hour][sid].capacity
        assert capacity is None or count <= capacity


def test_settlements_use_only_usable_active_eligible_routes_and_never_early(result):
    paths = {r.id: r.path for r in result.scenario.routes}
    obligations = result.scenario.obligations_by_id()
    for e in result.events:
        if e.type != "SETTLEMENT":
            continue
        ob = obligations[e.obligation_id]
        assert e.hour >= ob.due_hour
        eligible = dict(result.eligible_routes[ob.id])
        route = e.data["route"]
        assert route in eligible and e.hour >= eligible[route]
        assert all(result.service_status[e.hour][sid].pct > 0 for sid in paths[route])


def test_every_overdue_obligation_has_recorded_reasons_each_hour(result):
    blocks = {(b.obligation_id, b.hour): b for b in result.blocks}
    for o in result.outcomes.values():
        last = o.settled_hour if o.settled_hour is not None else result.scenario.horizon_hours
        for hour in range(o.due_hour, last):
            assert blocks[(o.obligation_id, hour)].reasons
        assert o.blocked_hours == o.overdue_hours == (last - o.due_hour)


def test_unsettled_obligations_stay_visible_at_the_horizon(result):
    unsettled = {o.obligation_id for o in result.outcomes.values() if o.status is Status.UNSETTLED_AT_HORIZON}
    reported = {e.obligation_id for e in result.events if e.type == "UNSETTLED_AT_HORIZON"}
    assert unsettled == reported
    assert result.hours[-1].overdue_count == len(unsettled)


def test_unavailable_required_service_is_never_bypassed():
    """No settlement may pass through a route whose path contains an unavailable service."""
    for r in (simulate(VARIANTS["idp_keygate"]), simulate(VARIANTS["keygate+backup"])):
        for e in r.events:
            if e.type == "SETTLEMENT":
                assert not (30 <= e.hour < 40), "KeyGate is a dependency of every route; nothing can settle"


def test_comparison_runs_share_one_payment_schedule():
    c = compare(load("default_72h.json"))
    schedules = {tuple(r.scenario.obligations) for r in c.runs.values()}
    assert len(schedules) == 1
