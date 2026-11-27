"""Contagion generations, the stress lab, intervention packages and the plain-language briefing."""

from __future__ import annotations

import pytest

from cyberliquidity import Scenario, simulate
from cyberliquidity.analysis import criticality_scan, duration_profile, impact_surface, start_profile
from cyberliquidity.briefing import briefing
from cyberliquidity.cascade import analyse_cascade
from cyberliquidity.compare import compare, interaction
from cyberliquidity.metrics import summarize
from cyberliquidity.sweep import liquidity_backstop
from cyberliquidity.variants import (
    make_backup,
    make_buffer,
    make_disruption,
    make_faster_recovery,
    replace,
    stress,
    suggest_backup,
)
from tests.conftest import load, tiny


def test_cascade_on_the_hand_example(three):
    c = analyse_cascade(simulate(stress(three)))
    assert {k: (n.kind, n.generation, n.parents) for k, n in c.nodes.items()} == {
        "O1": ("direct", 0, ()), "O2": ("propagated", 1, ("O1",))}
    assert c.nodes["O1"].root_services == ("provider_a",)
    assert c.indirectly_affected() == ["B"]  # B's own route works; only its incoming money was late
    assert c.participants["B"].first_cash_short_hour == 2 and c.participants["B"].largest_shortfall == 100
    assert c.max_generation == 1 and [g["value_hours"] for g in c.by_generation] == [200, 100]


def test_cascade_depth_in_the_high_value_channel_case():
    s = load("default_72h.json")
    c = analyse_cascade(simulate(stress(s, make_disruption("portal_north", 31, 12))))
    summary = c.summary()
    assert summary["max_generation"] == 4 and summary["direct_count"] == 1
    chain = [g["payers"] for g in c.by_generation]
    assert chain == [["ext_agency"], ["meridian"], ["silverfern"], ["thistle"], ["copperleaf"]]
    assert set(c.indirectly_affected()) == {"meridian", "silverfern", "thistle", "copperleaf"}


def test_gridlock_without_an_operational_origin_is_unlinked():
    # P and Q each owe the other 100 with no cash; nothing is disrupted, and no netting exists.
    data = tiny(obligations=[
        {"id": "pq", "payer": "P", "recipient": "Q", "amount": 100, "due_hour": 1, "routes": ["r"]},
        {"id": "qp", "payer": "Q", "recipient": "P", "amount": 100, "due_hour": 1, "routes": ["r"]},
    ])
    data["participants"][0]["initial_balance"] = 0
    c = analyse_cascade(simulate(Scenario.model_validate(data)))
    assert {n.kind for n in c.nodes.values()} == {"unlinked"} and c.max_generation is None
    assert c.by_generation == [{"generation": None, "kind": "unlinked", "count": 2, "value": 200,
                                "value_hours": 200 * 5, "payers": ["P", "Q"]}]


def test_criticality_scan_ranks_services_and_flags_common_mode_backups():
    s = load("default_72h.json")
    rows = criticality_scan(s, 32, 12, backup_delay_hours=3)
    values = [r["overdue_value_hours"] for r in rows]
    assert values == sorted(values, reverse=True) and len(rows) == len(s.services)
    top = rows[0]
    assert top["service"] == "idp_keygate" and top["backup_independent"] is False  # every route needs KeyGate
    assert set(top["routes_unavailable"]) == {r.id for r in s.routes}
    nimbus = next(r for r in rows if r["service"] == "cloud_nimbus")
    assert nimbus["best_intervention"] == "backup_route" and nimbus["max_generation"] == 2
    assert next(r for r in rows if r["service"] == "portal_north")["overdue_value_hours"] == 0  # misses its only payment


def test_duration_profile_finds_the_longest_tolerated_outage():
    s = load("default_72h.json")
    plans = {"backup_route": lambda sc, d: make_backup(["alpha_north"], "beta_south", d.start_hour + 3),
             "faster_recovery": lambda sc, d: make_faster_recovery(d.id, 6)}
    profile = duration_profile(s, "cloud_nimbus", 32, [2, 4, 6, 8, 12, 16, 24], plans=plans, outcome="priority_on_time")
    tol = profile["tolerance"]
    assert tol["none"]["longest_tolerated_h"] == 6 and tol["none"]["first_breach_h"] == 8
    assert tol["faster_recovery"]["longest_tolerated_h"] == 12
    assert tol["backup_route"]["longest_tolerated_h"] == 24 and tol["backup_route"]["first_breach_h"] is None
    none = [r for r in profile["rows"] if r["plan"] == "none"]
    assert [r["duration_h"] for r in none] == [2, 4, 6, 8, 12, 16, 24]
    assert all(a["overdue_value_hours"] <= b["overdue_value_hours"] for a, b in zip(none, none[1:]))


def test_start_profile_and_surface_show_calendar_dependence():
    s = load("default_72h.json")
    rows = start_profile(s, "portal_north", 6, range(24, 40))
    hit = {r["start_hour"]: r["overdue_value_hours"] for r in rows}
    assert hit[31] > 0 and hit[32] == 0  # the channel's one payment is due in hour 31
    surface = impact_surface(s, "cloud_nimbus", [8, 32, 56], [4, 12])
    assert surface["durations"] == [4, 12] and len(surface["z"]) == 2 and len(surface["z"][0]) == 3
    # the 12 h outage from hour 32 is exactly the default stress run B
    assert surface["z"][1][1] == summarize(simulate(stress(s)))["overdue_value_hours"] == 185_500_000


def test_packages_and_the_independence_benchmark(three):
    buffer = three.candidate_interventions[0]
    recovery = make_faster_recovery("D1", 2, id="fast")
    c = compare(stress(three), [buffer, recovery], packages=[(buffer, recovery)])
    assert c.package_keys() == ["P:buffer_B+fast"] and len(c.intervention_keys()) == 2
    x = c.interaction("P:buffer_B+fast")
    # B: 300 value-hours. Buffer alone removes 100 (B pays on time); recovery alone removes 300 (no outage).
    assert x["separate_reductions"] == {"C:buffer_B": 100, "C:fast": 300} and x["combined_reduction"] == 300
    assert x["independent_pct"] == pytest.approx(100.0) and x["verdict"] == "roughly independent"
    synthetic = {"B": {"overdue_value_hours": 100}, "C:a": {"overdue_value_hours": 50},
                 "C:b": {"overdue_value_hours": 50}, "P:ab": {"overdue_value_hours": 0}}
    y = interaction(synthetic, "overdue_value_hours", ["C:a", "C:b"], "P:ab")
    assert y["independent_pct"] == pytest.approx(75.0) and y["verdict"] == "complementary"
    synthetic["P:ab"] = {"overdue_value_hours": 50}
    assert interaction(synthetic, "overdue_value_hours", ["C:a", "C:b"], "P:ab")["verdict"] == "overlapping"


def test_briefing_answers_the_central_question_from_recorded_results():
    s = load("default_72h.json")
    st = replace(s, disruptions=(make_disruption("portal_north", 31, 12),), interventions=())
    sg = suggest_backup(st, st.disruptions[0])
    ivs = [make_backup(sg.primary_routes, sg.backup_route, 34, id="backup"),
           make_buffer([p.id for p in s.participants], 2_000_000, id="buffer"), make_faster_recovery("D1", 6)]
    c = compare(st, ivs, packages=[ivs[:2]])
    text = "\n".join(briefing(c, backstop=liquidity_backstop(stress(st))))
    assert "North High-Value Channel is unavailable for 12 h from hour 31" in text
    assert f"**{c.summaries['B']['delayed_count']} of 138 payments are delayed**" in text
    assert "up to 4 steps away from the outage" in text
    assert "Meridian Distribution (from hour 32)" in text and "None of them lost a payment route" in text
    assert "Most effective single intervention in this run" in text and "Backup payment provider (85% lower)" in text
    assert "Synthetic scenario" in text and "not economic loss" in text
    assert " loss " not in text.replace("not economic loss", "")


def test_briefing_when_nothing_is_delayed():
    s = load("default_72h.json")
    st = replace(s, disruptions=(make_disruption("portal_north", 40, 4),), interventions=())
    text = " ".join(briefing(compare(st, [])))
    assert "no payment is delayed" in text
