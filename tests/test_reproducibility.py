"""Determinism, metrics definitions, explanations, exports and the committed default fixture."""

from __future__ import annotations

import io
import json
import zipfile

from cyberliquidity import Mechanism, Scenario, simulate
from cyberliquidity.compare import compare
from cyberliquidity.explain import explain
from cyberliquidity.export import bundle_zip, run_files, scenario_digest, scenario_json
from cyberliquidity.metrics import delay_distribution, summarize
from cyberliquidity.synthetic import build_default_scenario
from cyberliquidity.variants import baseline, cash_unconstrained, stress
from tests.conftest import load


def test_identical_inputs_give_identical_results():
    s = stress(load("default_72h.json"))
    first, second = simulate(s), simulate(s)
    assert first.digest() == second.digest()
    assert first.outcomes == second.outcomes
    round_tripped = Scenario.model_validate_json(scenario_json(s))
    assert scenario_digest(round_tripped) == scenario_digest(s)
    assert simulate(round_tripped).digest() == first.digest()


def test_committed_default_scenario_matches_the_generator():
    assert scenario_digest(load("default_72h.json")) == scenario_digest(build_default_scenario())


def test_default_scenario_properties():
    s = load("default_72h.json")
    assert s.synthetic and s.horizon_hours == 72 and len(s.participants) == 12
    assert summarize(simulate(baseline(s)))["delayed_count"] == 0  # clean reference run
    stressed = simulate(stress(s))
    mechanisms = {o.mechanism for o in stressed.outcomes.values()}
    assert {Mechanism.DIRECT_OPERATIONAL, Mechanism.FINANCIAL_PROPAGATION} <= mechanisms
    reference = summarize(simulate(cash_unconstrained(stress(s))))
    assert reference["blocked_cash_count"] == 0 and reference["delayed_count"] > 0  # route-limited delays remain


def test_metric_definitions_on_the_hand_example(three):
    r = simulate(stress(three))
    m = summarize(r)
    assert m["overdue_value_hours"] == 100 + 200 == sum(o.amount * o.overdue_hours for o in r.outcomes.values())
    assert (m["peak_overdue_value"], m["peak_overdue_hour"]) == (200, 2)
    assert delay_distribution(r) == [{"delay_hours": 1, "count": 1, "value": 100}, {"delay_hours": 2, "count": 1, "value": 100}]
    assert (m["outage_end_hour"], m["backlog_at_outage_end_value"], m["backlog_cleared_hour"], m["backlog_hours_to_clear"]) == (3, 200, 3, 1)
    assert m["category_value_hours"] == {"operational_only": 200, "operational_and_cash": 0,
                                         "cash_awaiting_inflows": 100, "cash_other": 0}
    assert not any("loss" in key for key in m)  # delayed value is never labelled a loss


def test_explanation_trace_is_built_from_recorded_events(three):
    r = simulate(stress(three))
    e = explain(r, "O2")
    assert [step.kind for step in e.chain] == [
        "disruption", "service", "payment_blocked", "payment_outcome", "cash_shortfall", "payment_outcome"]
    assert e.chain_text() == ("Disruption D1 → Provider used by A unavailable → O1 A pays B blocked → "
                              "O1 A pays B delayed 2 h → Participant B cash insufficient → O2 B pays C delayed 1 h")
    expected_types = {"disruption": "SERVICE_STATE", "service": "SERVICE_STATE", "payment_blocked": "BLOCKED",
                      "cash_shortfall": "BLOCKED", "payment_outcome": "SETTLEMENT"}
    for step in e.chain:
        assert step.event_ids, step
        for event_id in step.event_ids:
            assert r.event(event_id).type == expected_types[step.kind]
    assert e.linked_inflows[0]["obligation_id"] == "O1"


def test_default_payroll_trace_reaches_the_cloud_outage():
    r = simulate(stress(load("default_72h.json")))
    payroll = next(o for o in r.outcomes.values() if o.purpose == "payroll" and o.mechanism is Mechanism.FINANCIAL_PROPAGATION)
    chain = explain(r, payroll.obligation_id).chain
    assert chain[0].kind == "disruption" and chain[0].ref == "D1"
    assert any(step.ref == "psp_alpha" for step in chain) and chain[-1].ref == payroll.obligation_id


def test_exports_are_complete_and_reproducible(three):
    c = compare(three)
    files = run_files(c.runs["B"], "B")
    assert set(files) == {"scenario.json", "summary.json", "obligations.csv", "events.csv", "ledger.csv",
                          "blocks.csv", "hourly.csv", "balances.csv", "services.csv"}
    assert files["obligations.csv"].splitlines()[0].startswith("obligation_id,payer,payer_name")
    assert len(files["events.csv"].splitlines()) == len(c.runs["B"].events) + 1
    archive = bundle_zip(c.runs, c.labels)
    assert archive == bundle_zip(c.runs, c.labels)  # byte-identical archives
    with zipfile.ZipFile(io.BytesIO(archive)) as z:
        manifest = json.loads(z.read("cyberliquidity_bundle/manifest.json"))
        for key, info in manifest["runs"].items():
            rerun = simulate(Scenario.model_validate_json(z.read(f"cyberliquidity_bundle/{key}/scenario.json")))
            assert rerun.digest() == info["results_digest"]
