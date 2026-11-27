"""Recommendation robustness: one-at-a-time assumption variants and minimax regret (no probabilities)."""

from __future__ import annotations

from pathlib import Path

import pytest

from cyberliquidity import simulate
from cyberliquidity.analysis import assumption_variants, plans_from_candidates, robustness
from cyberliquidity.briefing import briefing, robustness_paragraph
from cyberliquidity.cli import main
from cyberliquidity.compare import compare
from cyberliquidity.metrics import summarize
from cyberliquidity.variants import replace, stress, with_intervention
from tests.conftest import load

SCENARIOS = Path(__file__).resolve().parents[1] / "scenarios"


@pytest.fixture(scope="module")
def default():
    return load("default_72h.json")


@pytest.fixture(scope="module")
def default_robust(default):
    return robustness(replace(default, disruptions=(), interventions=()), default.disruptions,
                      plans_from_candidates(default))


def test_variants_change_one_assumption_at_a_time(default):
    base = replace(default, disruptions=(), interventions=())
    variants = assumption_variants(base, default.disruptions)
    keys = [info["key"] for info, _, _ in variants]
    assert keys == ["configured", "recycling", "ordering", "start_earlier", "start_later", "duration_shorter",
                    "duration_longer", "balances_lower", "balances_higher", "capacity_lower"]
    d = default.disruptions[0]
    by_key = {info["key"]: (scenario, ds) for info, scenario, ds in variants}
    assert by_key["recycling"][0].settings.liquidity_recycling == "next_step"
    assert by_key["ordering"][0].settings.ordering == "due_id"
    assert by_key["start_later"][1][0].start_hour == d.start_hour + 3
    assert by_key["duration_shorter"][1][0].duration_hours == 6 and by_key["duration_longer"][1][0].duration_hours == 18
    assert by_key["balances_lower"][0].participants[0].initial_balance == base.participants[0].initial_balance * 3 // 4
    assert all(s.capacity_per_hour is None or s.capacity_per_hour <= o.capacity_per_hour
               for s, o in zip(by_key["capacity_lower"][0].services, base.services))
    # everything else is untouched in each variant
    for key, (scenario, _ds) in by_key.items():
        assert scenario.obligations == base.obligations, key


def test_a_shift_that_hits_the_start_of_the_horizon_is_not_duplicated(default):
    base = replace(default, disruptions=(), interventions=())
    early = default.disruptions[0].model_copy(update={"start_hour": 1})
    keys = [info["key"] for info, _, _ in assumption_variants(base, [early])]
    assert "start_earlier" in keys  # hour 1 - 3 clamps to hour 0, which differs from hour 1
    at_zero = default.disruptions[0].model_copy(update={"start_hour": 0})
    keys = [info["key"] for info, _, _ in assumption_variants(base, [at_zero])]
    assert "start_earlier" not in keys


def test_configured_variant_reproduces_the_comparison(default, default_robust):
    c = compare(default)
    configured = default_robust["variants"][0]
    assert configured["stress_value"] == c.summaries["B"]["overdue_value_hours"]
    for iv in default.candidate_interventions:
        assert configured["values"][iv.id] == c.summaries[f"C:{iv.id}"]["overdue_value_hours"]
    assert default_robust["configured_best"] == [c.ranking()[0]["run"].removeprefix("C:")]


def test_regret_wins_and_minimax_are_consistent(default_robust):
    for v in default_robust["variants"]:
        best = min(v["values"].values())
        assert all(v["regret"][k] == v["values"][k] - best for k in v["values"])
        assert set(v["best"]) == {k for k, x in v["values"].items() if x == best}
        assert set(v["best"]) <= set(v["practically_best"])
    summary = default_robust["summary"]
    minimax = default_robust["minimax_regret"]
    assert summary[minimax]["max_regret"] == min(x["max_regret"] for x in summary.values())
    # in this scenario the backup stays best except when a 6 h outage is halved (then faster recovery removes it all)
    assert default_robust["configured_best"] == ["backup_beta"] and minimax == "backup_beta"
    assert [b["variant"] for b in default_robust["configured_best_beaten_in"]["backup_beta"]] == ["duration_shorter"]
    assert not default_robust["robust"]


def test_plans_follow_the_moved_outage(default):
    plans = plans_from_candidates(default)
    d = default.disruptions[0].model_copy(update={"start_hour": 40, "id": "Dx"})
    backup = plans["backup_beta"](default, d)
    assert backup.active_from_hour == 40 + (35 - 32)  # same activation delay after the outage starts
    assert plans["faster_recovery"](default, d).disruption == "Dx"


def test_zero_disruption_variants_count_harm_from_a_rule():
    cluster = load("cluster_gridlock_48h.json")
    r = robustness(replace(cluster, disruptions=(), interventions=()), cluster.disruptions, plans_from_candidates(cluster))
    later = next(v for v in r["variants"] if v["key"] == "start_later")
    assert later["stress_value"] == 0  # the vehicle maker has paid before the outage starts
    assert later["values"]["reserve_payroll"] > 0  # the reserve rule creates delays on its own
    assert "reserve_payroll" not in later["practically_best"]
    assert r["summary"]["reserve_payroll"]["worsens_in"] == 10
    # offsetting needs each net payer to cover its net position: with 25 % less cash the ring unravels
    lower = next(v for v in r["variants"] if v["key"] == "balances_lower")
    assert lower["reduction_pct"]["offsetting"] == 0


def test_briefing_paragraph_and_negative_reductions(default, default_robust):
    text = robustness_paragraph(default_robust)
    assert text.startswith("**How robust is this ranking?**") and "9 of the 10" in text and "not weighted" in text
    cluster = load("cluster_gridlock_48h.json")
    paragraphs = briefing(compare(cluster))
    ranking = next(p for p in paragraphs if p.startswith("**Most effective"))
    assert "Payroll-protection reserve (**12% higher**)" in ranking and "% lower)" in ranking
    assert "it does not reduce the number of delayed priority-1 payments (8 of 10)" in ranking


def test_robustness_needs_a_disruption(default):
    with pytest.raises(ValueError):
        robustness(default, [], plans_from_candidates(default))


def test_cli_robustness_and_brief(capsys, tmp_path):
    assert main(["robustness", str(SCENARIOS / "cluster_gridlock_48h.json")]) == 0
    out = capsys.readouterr().out
    assert "Outage 50% shorter" in out and "How robust is this ranking?" in out
    target = tmp_path / "robust.csv"
    assert main(["robustness", str(SCENARIOS / "default_72h.json"), "--out", str(target)]) == 0
    assert target.read_text().splitlines()[0].startswith("variant,label,stress_value")
    assert main(["brief", str(SCENARIOS / "default_72h.json")]) == 0
    assert "How robust is this ranking?" in capsys.readouterr().out


def test_reserve_metrics_match_the_records():
    cluster = load("cluster_gridlock_48h.json")
    iv = next(x for x in cluster.candidate_interventions if x.type == "priority_reserve")
    r = simulate(with_intervention(stress(cluster), iv))
    m = summarize(r)
    held = {b.obligation_id for b in r.blocks if b.held_by_reserve}
    assert m["reserve_held_count"] == len(held) > 0
    assert all(r.outcomes[o].priority > iv.protected_priority for o in held)  # protected payments are never held
    assert m["urgent_count"] == sum(1 for o in r.outcomes.values() if o.priority == 1)
