"""Differential testing: the engine must agree with an independent reference model.

``tests/reference_model.py`` re-implements the operational and settlement rules from the
model note without sharing code with the engine. Both are run on seeded random scenarios
(random dependency graphs, capacities, schedules, disruptions with degradation and
recovery ramps, every intervention type, both orderings and both recycling rules) and
must produce the same settlements, routes, balances, blocking reasons, credit usage and
offsetting batches.
"""

from __future__ import annotations

import pytest

from cyberliquidity import simulate
from cyberliquidity.generator import random_scenario
from tests.reference_model import reference_simulate

BATCHES = 12
PER_BATCH = 25  # 300 random scenarios in total


def _scenario(seed: int):
    size = seed % 3
    if size == 0:
        return random_scenario(seed)
    if size == 1:
        return random_scenario(seed, participants=5, services=4, routes=2, obligations=40, horizon=12, max_amount=150)
    return random_scenario(seed, participants=16, services=9, routes=5, obligations=220, horizon=36, max_disruptions=3)


def _compare(seed: int) -> None:
    scenario = _scenario(seed)
    engine = simulate(scenario)
    ref = reference_simulate(scenario)
    tag = f"seed {seed}"
    settled = {k: o.settled_hour for k, o in engine.outcomes.items() if o.settled_hour is not None}
    assert settled == ref.settled_hour, tag
    assert {k: o.route_used for k, o in engine.outcomes.items() if o.route_used} == ref.route_used, tag
    order = [(e.hour, e.obligation_id) for e in engine.events if e.type == "SETTLEMENT"]
    assert order == ref.settlement_order, tag
    assert [h.balances for h in engine.hours] == ref.balances, tag
    assert [h.credit_drawn for h in engine.hours] == ref.credit_drawn, tag
    assert engine.credit_peak == ref.credit_peak, tag
    assert {(b.obligation_id, b.hour): b.reasons for b in engine.blocks} == ref.reasons, tag
    batches = [(e.hour, tuple(e.data["obligations"])) for e in engine.events if e.type == "OFFSETTING_BATCH"]
    assert batches == ref.batches, tag


@pytest.mark.parametrize("batch", range(BATCHES))
def test_engine_matches_reference_model_on_random_scenarios(batch):
    for seed in range(batch * PER_BATCH, (batch + 1) * PER_BATCH):
        _compare(seed)


def test_random_scenarios_exercise_every_mechanism():
    """Guard against a generator that silently stops covering the interesting cases."""
    seen = set()
    for seed in range(BATCHES * PER_BATCH):
        scenario = _scenario(seed)
        r = simulate(scenario)
        seen.update(reason for b in r.blocks for reason in b.reasons)
        seen.update(e.type for e in r.events if e.type in ("CREDIT_DRAW", "CREDIT_REPAYMENT", "FUNDING", "OFFSETTING_BATCH"))
        seen.update("held_by_reserve" for b in r.blocks if b.held_by_reserve)
        seen.update("batch_credit_draw" for e in r.events if e.type == "CREDIT_DRAW" and "offsetting_batch" in e.data)
        seen.update("via_backup" for o in r.outcomes.values() if o.via_backup)
        seen.update(d.severity_pct < 100 and "degradation" or "outage" for d in scenario.disruptions)
        seen.update("recovery" for d in scenario.disruptions if d.recovery_hours)
        seen.add(scenario.settings.ordering)
        seen.add(scenario.settings.liquidity_recycling)
    expected = {"SERVICE_UNAVAILABLE", "CAPACITY_EXHAUSTED", "ROUTE_NOT_YET_ACTIVE", "INSUFFICIENT_CASH",
                "CREDIT_DRAW", "CREDIT_REPAYMENT", "FUNDING", "via_backup", "outage", "degradation", "recovery",
                "OFFSETTING_BATCH", "held_by_reserve", "batch_credit_draw",
                "priority_due_id", "due_id", "within_step", "next_step"}
    assert expected <= seen, expected - seen
