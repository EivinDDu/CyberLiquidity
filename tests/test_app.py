"""Headless smoke tests of the Streamlit interface (runs the real script; no browser)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest  # noqa: E402

from cyberliquidity import DISCLAIMER  # noqa: E402

APP = str(Path(__file__).resolve().parents[1] / "app" / "streamlit_app.py")
TABS = ["Overview", "Contagion", "Network", "Timeline", "Interventions & evidence", "Stress lab", "Scenario editor",
        "Model & assumptions"]


def _run(at: AppTest) -> AppTest:
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    return at


def _app(tab: str | None = None) -> AppTest:
    at = AppTest.from_file(APP, default_timeout=120)
    if tab:
        at.session_state["tab"] = tab
    return _run(at)


def _text(at: AppTest) -> str:
    return "\n".join(m.value for m in at.markdown) + "\n".join(c.value for c in at.caption)


def test_default_scenario_renders_with_disclaimer_and_the_answer():
    at = _app()
    assert any(DISCLAIMER in i.value for i in at.info)  # the synthetic-data notice is always on the page
    text = _text(at)
    assert "#### The answer" in text and "payments are delayed" in text and "run short of cash" in text
    assert "Baseline vs stress vs intervention" in text
    assert ":orange-badge[:material/science: Synthetic data]" in text
    assert len(at.dataframe) >= 4


def test_footer_reports_the_integrity_audit_of_every_run():
    at = _app()
    footer = next(c.value for c in at.caption if "Integrity audit" in c.value)
    runs = int(re.search(r"for all (\d+) runs on this page", footer).group(1))
    assert runs >= 8 and "**Integrity audit passed**" in footer  # A, B, the six responses and the pairs
    assert DISCLAIMER in footer and "scenario digest" in footer


def test_guide_and_export_menus():
    at = _app()
    text = _text(at)
    assert "1 · Pick a scenario" in text and "4 · Read the answer" in text  # quick start (Overview and Guide menu)
    assert "**Gridlock**" in text  # glossary
    labels = [b.proto.label for b in at.get("download_button")]
    assert labels == ["Download briefing report (.html)", "Download all runs (.zip)", "Download stressed scenario (B) JSON"]


def test_a_scenario_that_fails_the_checks_is_explained_not_simulated():
    at = AppTest.from_file(APP, default_timeout=120)
    at.session_state["edited:default_72h.json"] = '{"id": "x", "participants": "<b>not a list</b>"}'
    _run(at)
    assert [e.value for e in at.error] == ["**The edited scenario is not a valid scenario.** Nothing was simulated."]
    problems = next(m.value for m in at.markdown if m.value.startswith("- "))
    assert "participants\\: Input should be a valid array" in problems and "<b>" not in problems  # escaped Markdown
    assert not at.tabs  # stopped before anything was simulated


def test_error_boundary_contains_a_failure_to_its_section():
    def page():
        import streamlit as st

        from ui.layout import guarded

        with guarded("The first section"):
            raise ValueError("boom")
        st.markdown("after the first section")
        with guarded("The second section"):
            st.stop()  # Streamlit's own control flow passes through
        st.markdown("never shown")

    at = AppTest.from_function(page)
    at.run()
    assert not at.exception
    assert at.error[0].value.startswith("**The first section could not be shown.**")
    assert [m.value for m in at.markdown] == ["after the first section"]
    assert at.caption[0].value == "Error type: `ValueError`"


@pytest.mark.parametrize("tab", TABS)
def test_every_tab_renders(tab):
    at = _app(tab)
    if tab == "Stress lab":
        text = _text(at)
        assert "1 · Which service matters most?" in text and "3 · When is the worst time for it to start?" in text
        assert "Longest outage with the outcome intact" in str([df.value.columns.tolist() for df in at.dataframe])


@pytest.mark.parametrize("kind", ["backup_route", "liquidity_buffer", "credit_line", "faster_recovery", "offsetting",
                                  "priority_reserve"])
def test_each_intervention_can_be_selected(kind):
    at = _app()
    at.sidebar.radio[0].set_value(kind)
    _run(at)


def test_the_answer_and_the_evidence_include_the_robustness_check():
    at = _app("Interventions & evidence")
    text = _text(at)
    assert "How robust is the ranking?" in text and "How robust is this ranking?" in text
    assert "minimax" in text
    columns = [df.value.columns.tolist() for df in at.dataframe]
    assert ["Intervention", "Best (or within 1%)", "Largest shortfall against the best", "In variant", "Reduction range",
            "Makes it worse in"] in columns


def test_the_cluster_scenario_shows_offsetting_and_the_reserve_trade_off():
    at = _app()
    at.sidebar.selectbox[0].set_value("cluster_gridlock_48h.json")
    _run(at)
    answer = next(m.value for m in at.markdown if m.value.startswith("**Most effective"))
    assert "Multilateral offsetting" in answer and "gridlock" in answer
    assert "Payroll-protection reserve (**" in answer and "higher**" in answer
    # a narrower reserve rule (one firm, short look-ahead) is configurable and still renders
    at.sidebar.multiselect(key="cluster-gridlock-48h:reserve_parts").set_value(["birch"])
    at.sidebar.slider(key="cluster-gridlock-48h:reserve_lookahead").set_value(4)
    _run(at)


@pytest.mark.parametrize("layer", ["Operational dependencies", "Financial obligations", "Payment routing (coupling)"])
def test_network_layers_render(layer):
    at = _app("Network")
    at.radio(key="net_layer").set_value(layer)
    _run(at)


def test_other_scenario_and_disruption_controls():
    at = _app()
    at.sidebar.selectbox[0].set_value("three_participant.json")
    _run(at)
    at = _app()
    at.sidebar.selectbox(key="default-72h:service").set_value("idp_keygate")
    _run(at)
    assert any("Not independent" in w.value for w in at.sidebar.warning)


def test_concurrent_disruption_reaches_the_briefing():
    at = _app()
    at.sidebar.number_input(key="default-72h:n_extra").set_value(1)
    _run(at)
    assert "Concurrently:" in _text(at)
    assert " and " in next(m.value for m in at.markdown if m.value.startswith("**If**"))


def test_scenario_editor_applies_validated_edits_and_rejects_invalid_ones():
    at = _app("Scenario editor")
    at.number_input(key="ed:shift").set_value(72)  # pushes every payment outside the 72 h horizon
    next(b for b in at.button if b.label == "Validate and apply").click()
    _run(at)
    assert any("failed validation" in e.value for e in at.error)
    assert "edited:default_72h.json" not in at.session_state

    at = _app("Scenario editor")
    at.number_input(key="ed:bal_pct").set_value(300)  # triple every opening balance
    next(b for b in at.button if b.label == "Validate and apply").click()
    _run(at)
    assert "edited:default_72h.json" in at.session_state
    assert any("edited" in i.value for i in at.sidebar.info)
