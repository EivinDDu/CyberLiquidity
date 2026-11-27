"""The 0.4 speed-ups are exact: each fast path gives the same answer as the slow one it replaces."""

from __future__ import annotations

import pytest

from cyberliquidity import simulate, sweep
from cyberliquidity.engine import simulate_until
from cyberliquidity.generator import random_scenario
from cyberliquidity.operational import OperationalModel
from cyberliquidity.sweep import liquidity_backstop
from cyberliquidity.variants import make_disruption, replace, stress, with_settings
from tests.conftest import load


def test_simulate_until_without_stopping_is_simulate():
    s = stress(load("cluster_gridlock_48h.json"))
    full = simulate_until(s, lambda hour, settled, blocked: False)
    assert full is not None and full.digest() == simulate(s).digest()
    seen = []
    assert simulate_until(s, lambda hour, settled, blocked: seen.append(hour) or hour == 5) is None
    assert seen == [0, 1, 2, 3, 4, 5]  # called once at the end of every hour, stopped at once


@pytest.mark.parametrize("case", ["default", "default_next_step", "keygate", "three", "cluster"])
def test_backstop_early_stop_and_reuse_change_nothing(case, monkeypatch):
    d = load("default_72h.json")
    scenario = {
        "default": stress(d),
        "default_next_step": with_settings(stress(d), liquidity_recycling="next_step"),
        "keygate": stress(d, make_disruption("idp_keygate", 30, 10, recovery_hours=3)),
        "three": stress(load("three_participant.json")),
        "cluster": stress(load("cluster_gridlock_48h.json")),
    }[case]
    fast = liquidity_backstop(scenario)
    monkeypatch.setattr(sweep, "simulate_until", lambda s, stop: simulate(s))  # every trial runs to the horizon
    slow = liquidity_backstop(scenario)
    assert slow.runs_stopped_early == 0
    for field in ("allocation", "peaks", "rows", "verified_sufficient", "tight", "not_tight", "route_limited_count",
                  "route_limited_value", "credit_summary", "stress_summary"):
        assert getattr(fast, field) == getattr(slow, field), field
    assert fast.runs_used <= slow.runs_used


def test_backstop_stops_failing_trials_early():
    b = liquidity_backstop(stress(load("default_72h.json")))
    assert b.verified_sufficient and b.runs_stopped_early > 0
    assert b.runs_stopped_early < b.runs_used


@pytest.mark.parametrize("seed", range(12))
def test_cached_service_status_matches_a_fresh_evaluation(seed):
    s = random_scenario(seed)
    op = OperationalModel(s)
    for hour in range(s.horizon_hours):
        assert op.status_at(hour) == op._evaluate(hour, None)
    assert op.status_at(None) == op._evaluate(None, None)


def test_block_flags_filled_by_the_engine_match_the_definitions():
    for seed in range(40):
        r = simulate(random_scenario(seed))
        for b in r.blocks:
            assert b.operational == any(x in ("SERVICE_UNAVAILABLE", "CAPACITY_EXHAUSTED", "ROUTE_NOT_YET_ACTIVE")
                                        for x in b.reasons)
            assert b.cash == ("INSUFFICIENT_CASH" in b.reasons)
            expected = ("operational_and_cash" if b.operational and b.cash else "operational_only" if b.operational
                        else "cash_awaiting_inflows" if b.overdue_inflows else "cash_other")
            assert b.category == expected


def test_shared_route_checks_are_not_mutated_downstream():
    from cyberliquidity.explain import Explainer
    from cyberliquidity.metrics import block_rows

    r = simulate(stress(load("default_72h.json")))
    before = [tuple(tuple(sorted(c.items())) for c in b.route_checks) for b in r.blocks]
    block_rows(r)
    explainer = Explainer(r)
    for oid in sorted({b.obligation_id for b in r.blocks})[:20]:
        explainer.explain(oid)
    after = [tuple(tuple(sorted(c.items())) for c in b.route_checks) for b in r.blocks]
    assert before == after


def test_large_random_network_is_unchanged_by_the_offsetting_shortcut():
    # capacity-limited network with offsetting: exercises both the shortcut and the rebuild path
    s = random_scenario(10_004, participants=40, services=12, routes=6, obligations=800, horizon=72)
    assert any(iv.type == "offsetting" for iv in s.interventions)
    r = simulate(s)
    assert r.totals["offsetting_batches"] >= 1
    assert simulate(replace(s)).digest() == r.digest()  # deterministic, and validated copy runs the same
