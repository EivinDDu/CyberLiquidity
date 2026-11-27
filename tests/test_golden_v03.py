"""The 0.4 engine is faster but must not change a single result of the 0.3 rules.

``golden_v03_digests.json`` holds the event-and-ledger digests that the 0.3.0 engine
produced for every case below: all three built-in scenarios with every candidate
intervention, every valid pair, both alternative settlement rules and the cash
reference, plus 60 random networks that exercise every rule (offsetting, reserves,
credit lines, backups, buffers, faster recovery, degradation and recovery ramps).
"""

from __future__ import annotations

import json
from pathlib import Path

from cyberliquidity import simulate
from cyberliquidity.generator import random_scenario
from cyberliquidity.models import Scenario
from cyberliquidity.variants import (
    baseline,
    cash_unconstrained,
    make_credit_line,
    make_offsetting,
    make_reserve,
    stress,
    with_intervention,
    with_settings,
)

ROOT = Path(__file__).resolve().parents[1]
GOLDEN = ROOT / "tests" / "golden_v03_digests.json"


def v03_cases() -> dict[str, Scenario]:
    cases: dict[str, Scenario] = {}
    for name in ("default_72h.json", "three_participant.json", "cluster_gridlock_48h.json"):
        s = Scenario.model_validate_json((ROOT / "scenarios" / name).read_text(encoding="utf-8"))
        tag = name.split(".")[0]
        b = stress(s)
        cases[f"{tag}:baseline"] = baseline(s)
        cases[f"{tag}:stress"] = b
        cases[f"{tag}:cash_reference"] = cash_unconstrained(b)
        cases[f"{tag}:fifo"] = with_settings(b, ordering="due_id")
        cases[f"{tag}:next_step"] = with_settings(b, liquidity_recycling="next_step")
        ivs = list(s.candidate_interventions)
        for iv in ivs:
            cases[f"{tag}:+{iv.id}"] = with_intervention(b, iv)
            cases[f"{tag}:next_step+{iv.id}"] = with_intervention(with_settings(b, liquidity_recycling="next_step"), iv)
        for i, first in enumerate(ivs):
            for second in ivs[i + 1:]:
                cases[f"{tag}:+{first.id}+{second.id}"] = with_intervention(b, first, second)
        everyone = [p.id for p in s.participants]
        cases[f"{tag}:+offsetting+reserve_p2"] = with_intervention(b, make_offsetting(id="o"),
                                                                   make_reserve(None, 2, 12, id="r"))
        cases[f"{tag}:+offsetting+credit"] = with_intervention(
            b, make_offsetting(id="o"), make_credit_line(everyone[:3], max(1, s.obligations[0].amount // 2), 0, id="cl"))
    for seed in range(60):
        cases[f"random:{seed}"] = random_scenario(seed)
    for seed in range(2):
        cases[f"random_large:{seed}"] = random_scenario(10_000 + seed, participants=40, services=12, routes=6,
                                                       obligations=800, horizon=72)
    return cases


def test_v03_rules_are_unchanged():
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    cases = v03_cases()
    assert set(cases) == set(golden["digests"])
    mismatches = [name for name, scenario in cases.items()
                  if simulate(scenario).digest(engine_version=golden["engine_version"]) != golden["digests"][name]]
    assert not mismatches, mismatches
