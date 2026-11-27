"""The shareable HTML briefing report (built from a comparison; no browser needed)."""

from __future__ import annotations

import pytest

pytest.importorskip("plotly")

from cyberliquidity import DISCLAIMER  # noqa: E402
from cyberliquidity.analysis import plans_from_candidates, robustness  # noqa: E402
from cyberliquidity.briefing import briefing  # noqa: E402
from cyberliquidity.cascade import analyse_cascade  # noqa: E402
from cyberliquidity.compare import compare, run_key  # noqa: E402
from cyberliquidity.sweep import liquidity_backstop  # noqa: E402
from cyberliquidity.variants import replace, stress  # noqa: E402
from tests.conftest import load  # noqa: E402
from ui.charts import RunView  # noqa: E402
from ui.report import _md, build_report  # noqa: E402


def test_report_is_self_contained_and_carries_the_evidence():
    s = load("default_72h.json")
    ivs = list(s.candidate_interventions)
    c = compare(s, ivs, packages=[ivs[:2]])
    runs = {"A": RunView("A", "A · Baseline", "baseline", c.runs["A"]), "B": RunView("B", "B · Stress", "stress", c.runs["B"])}
    for iv in ivs:
        runs[run_key(iv)] = RunView(run_key(iv), f"C · {iv.type}", iv.type, c.runs[run_key(iv)])
    for k in c.package_keys():
        runs[k] = RunView(k, c.labels[k], "combination", c.runs[k])
    backstop = liquidity_backstop(stress(s))
    paragraphs = briefing(c, backstop=backstop)
    page = build_report(c, runs, ["A", "B", run_key(ivs[0])], paragraphs, analyse_cascade(c.runs["B"]), backstop,
                        ["**B:** Nimbus Cloud unavailable"], ["An assumption"]).decode("utf-8")
    assert page.startswith("<!doctype html>") and '<meta charset="utf-8">' in page
    assert DISCLAIMER in page and "The answer" in page and "<strong>20 of 138 payments are delayed</strong>" in page
    assert "Plotly.newPlot" in page and "<script src=" not in page  # library embedded: works offline
    assert c.runs["B"].digest()[:24] in page  # reproduction digests
    assert "What liquidity can and cannot do" in page and "Combinations" in page
    assert "How robust is the ranking?" not in page  # only when a robustness result is passed


def test_report_carries_the_robustness_grid_and_formats_count_metrics():
    s = load("cluster_gridlock_48h.json")
    ivs = list(s.candidate_interventions)
    c = compare(s, ivs)
    runs = {"A": RunView("A", "A · Baseline", "baseline", c.runs["A"]), "B": RunView("B", "B · Stress", "stress", c.runs["B"])}
    for iv in ivs:
        runs[run_key(iv)] = RunView(run_key(iv), f"C · {iv.type}", iv.type, c.runs[run_key(iv)])
    robust = robustness(replace(s, disruptions=(), interventions=()), s.disruptions, plans_from_candidates(s),
                        metric="delayed_count")
    paragraphs = briefing(c, "delayed_count", robust=robust)
    assert any(p.startswith("**How robust is this ranking?**") for p in paragraphs)
    page = build_report(c, runs, ["A", "B", run_key(ivs[0])], paragraphs, analyse_cascade(c.runs["B"]), None,
                        ["**B:** East Cloud Region unavailable"], ["An assumption"], metric="delayed_count",
                        robust=robust).decode("utf-8")
    assert "How robust is the ranking?" in page and "★" in page
    assert "number of delayed obligations" in page.lower()
    assert "27.00 EUR" not in page  # a count is not formatted as money


def test_markdown_subset_is_escaped():
    assert _md("**bold** _it_ `x` <script>") == "<strong>bold</strong> <em>it</em> <code>x</code> &lt;script&gt;"
