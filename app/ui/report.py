"""Self-contained HTML briefing report (one file, works offline: Plotly is embedded once).

Every number comes from the comparison passed in; the report adds no analysis of its
own. It is meant for sharing a single scenario run with colleagues who do not run the
app, together with the digests needed to reproduce it.
"""

from __future__ import annotations

import datetime as _dt
import html
import re
from collections.abc import Sequence

import plotly.offline

from cyberliquidity import DISCLAIMER, ENGINE_VERSION
from cyberliquidity.cascade import KIND_LABELS, Cascade
from cyberliquidity.compare import Comparison
from cyberliquidity.export import scenario_digest
from cyberliquidity.metrics import HEADLINE_METRICS, RANKING_LABELS
from cyberliquidity.sweep import BackstopResult

from . import charts
from .charts import RunView
from .theme import LIGHT

_CSS = """
:root { color-scheme: light; }
body { font-family: system-ui, -apple-system, 'Segoe UI', sans-serif; color: #191919; background: #ffffff;
       margin: 0; line-height: 1.5; }
main { max-width: 1040px; margin: 0 auto; padding: 32px 24px 64px; }
h1 { font-size: 1.6rem; margin: 0 0 4px; }
h2 { font-size: 1.15rem; margin: 36px 0 8px; padding-top: 8px; border-top: 1px solid #e7e7e7; }
p, li { max-width: 72ch; }
.sub { color: #666666; margin: 0 0 16px; }
.banner { background: #fff3d4; border: 1px solid #fbb315; border-radius: 6px; padding: 10px 14px; margin: 16px 0; }
.answer { background: #f6f9fe; border: 1px solid #cce3fd; border-radius: 8px; padding: 6px 18px; }
table { border-collapse: collapse; width: 100%; margin: 8px 0 16px; font-size: 0.86rem; }
th, td { text-align: left; padding: 6px 8px; border-bottom: 1px solid #e7e7e7; vertical-align: top; }
th { color: #666666; font-weight: 600; background: #fafafa; }
td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; }
code { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 0.82rem; }
.small { color: #666666; font-size: 0.85rem; }
details { margin: 8px 0 16px; }
"""


def _md(text: str) -> str:
    """Escape, then render the small Markdown subset the briefing uses (bold, italics, code)."""
    out = html.escape(text)
    out = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", out)
    out = re.sub(r"(?<![\w])_(.+?)_(?![\w])", r"<em>\1</em>", out)
    out = re.sub(r"`(.+?)`", r"<code>\1</code>", out)
    return out


def _table(headers: Sequence[str], rows: Sequence[Sequence[object]], numeric: Sequence[int] = ()) -> str:
    head = "".join(f'<th class="{"num" if i in numeric else ""}">{html.escape(str(h))}</th>' for i, h in enumerate(headers))
    body = "".join(
        "<tr>" + "".join(f'<td class="{"num" if i in numeric else ""}">{html.escape(str(c))}</td>' for i, c in enumerate(r)) + "</tr>"
        for r in rows
    )
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def _fig(fig) -> str:
    return fig.to_html(full_html=False, include_plotlyjs=False, config={"displaylogo": False, "responsive": True})


def _metric_kind(metric: str) -> str:
    if metric.endswith("value_hours"):
        return "money_hours"
    return "money" if metric.endswith("_value") else "count"


def build_report(comparison: Comparison, runs: dict[str, RunView], abc: Sequence[str], briefing_paragraphs: Sequence[str],
                 cascade: Cascade, backstop: BackstopResult | None, configuration: Sequence[str],
                 assumptions: Sequence[str], metric: str = "overdue_value_hours", robust: dict | None = None,
                 robust_labels: dict[str, str] | None = None) -> bytes:
    pal = LIGHT
    stress_run = comparison.runs["B"]
    s = stress_run.scenario
    cur = s.currency
    money = cur.format
    names = s.names()
    summaries = comparison.summaries
    singles = comparison.intervention_keys()
    parts: list[str] = []

    parts.append(f"<h1>CyberLiquidity briefing · {html.escape(s.name)}</h1>")
    parts.append(f'<p class="sub">Generated {_dt.datetime.now(_dt.timezone.utc):%Y-%m-%d %H:%M} UTC · engine {ENGINE_VERSION} · '
                 f"scenario digest <code>{scenario_digest(s)[:16]}…</code></p>")
    parts.append(f'<div class="banner"><strong>{html.escape(DISCLAIMER)}</strong> All organisations and data are fictional. '
                 "Overdue value is not economic loss, and a temporary cash shortfall is not insolvency.</div>")

    parts.append("<h2>The answer</h2><div class=\"answer\">" + "".join(f"<p>{_md(p)}</p>" for p in briefing_paragraphs) + "</div>")

    parts.append("<h2>What was run</h2><ul>" + "".join(f"<li>{_md(c)}</li>" for c in configuration) + "</ul>")

    abc_views = [runs[k] for k in abc]
    parts.append("<h2>Overdue payments over time</h2>")
    parts.append(_fig(charts.overdue_chart(abc_views, pal, cur, "value")))
    parts.append("<h2>What held payments up</h2>")
    parts.append(_fig(charts.mechanism_bars([rv for rv in abc_views if rv.key != "A"], summaries, pal, cur)))
    parts.append("<h2>How far the disruption travelled (run B)</h2>")
    if cascade.by_generation:
        parts.append(_fig(charts.generation_bars(cascade, pal, cur)))
    exposure = [x for x in cascade.participants.values() if x.cash_short or x.delayed_out_count]
    if exposure:
        parts.append(_table(
            ["Participant", "Short of cash from hour", "Hours short", "Own route ever blocked", "Reached at step",
             "Delayed payments out", "Delayed payments in", "Lowest balance"],
            [[names[x.participant], x.first_cash_short_hour if x.cash_short else "–", x.cash_short_hours,
              "yes" if x.route_blocked else "no", "–" if x.generation is None else x.generation,
              f"{x.delayed_out_count} · {money(x.delayed_out_value)}", f"{x.delayed_in_count} · {money(x.delayed_in_value)}",
              money(x.min_balance)]
             for x in sorted(exposure, key=lambda x: (x.first_cash_short_hour is None, x.first_cash_short_hour or 0, x.participant))],
            numeric=(1, 2, 4, 7)))
    nodes = sorted(cascade.nodes.values(), key=lambda n: (-n.value_hours, n.obligation_id))[:40]
    if nodes:
        parts.append("<details><summary>Delayed obligations in run B (largest value × hours first)</summary>")
        parts.append(_table(["Obligation", "Payer → recipient", "Purpose", "Amount", "Overdue h", "How", "Step", "Waiting for"],
                            [[n.obligation_id, f"{names.get(n.payer, n.payer)} → {names.get(n.recipient, n.recipient)}",
                              n.purpose, money(n.amount), n.overdue_hours, KIND_LABELS[n.kind].split(":")[0],
                              "–" if n.generation is None else n.generation, ", ".join(n.parents[:3])] for n in nodes],
                            numeric=(3, 4, 6)))
        parts.append("</details>")

    parts.append("<h2>Interventions against the same stressed run</h2>")
    ranking = comparison.ranking(metric)
    parts.append(f"<p>Ranked by {html.escape(RANKING_LABELS.get(metric, metric))} (lower is less disruption). "
                 "This ranking describes this scenario under its assumptions only.</p>")
    kind = _metric_kind(metric)
    label = RANKING_LABELS.get(metric, metric)
    as_axis = (lambda v: cur.to_major(v)) if kind != "count" else (lambda v: v)
    rows = [{"label": runs["B"].label, "kind": "stress", "value": as_axis(summaries["B"][metric]), "raw": summaries["B"][metric]}]
    for k in singles:
        rows.append({"label": runs[k].label, "kind": runs[k].kind, "value": as_axis(summaries[k][metric]), "raw": summaries[k][metric]})
    parts.append(_fig(charts.intervention_bars(rows, pal, label[:1].upper() + label[1:], lambda v: _fmt(v, kind, money))))

    def change(r: dict) -> str:
        if r["reduction_pct"] is None:
            return "–"
        return f"{abs(r['reduction_pct']):.0f}% higher (worse)" if r["reduction"] < 0 else f"{r['reduction_pct']:.0f}% lower"

    parts.append(_table(["Rank", "Intervention", label[:1].upper() + label[1:], "Change against B"],
                        [[r["rank"], runs[r["run"]].label, _fmt(r["value"], kind, money), change(r)] for r in ranking],
                        numeric=(0, 2)))
    for k in singles:
        parts.append(f"<p><strong>{html.escape(runs[k].label)}</strong></p><ul>"
                     + "".join(f"<li>{_md(line)}</li>" for line in comparison.effects[k]) + "</ul>")
    if robust is not None and robust.get("metric") == metric and robust["variants"]:
        labels = robust_labels or robust.get("labels", {})
        parts.append("<h2>How robust is the ranking?</h2>")
        parts.append("<p>The same comparison re-run with one assumption changed at a time. Each cell is the share of that "
                     "variant's stress disruption removed (★ = best in the row). The variants are not weighted.</p>")
        parts.append(_fig(charts.robustness_heatmap(robust, labels, pal, cur)))
        parts.append(_table(
            ["Intervention", "Best (or within 1%)", "Largest shortfall against the best", "Makes it worse in"],
            [[labels.get(k, k), f"{x['practical_wins']} of {x['tested']}", _fmt(x["max_regret"], kind, money),
              f"{x['worsens_in']} of {x['tested']}"]
             for k, x in sorted(robust["summary"].items(), key=lambda kv: (kv[1]["max_regret"], kv[0]))],
            numeric=(2,)))
    packages = comparison.package_keys()
    if packages:
        parts.append("<h2>Combinations</h2>")
        prow = []
        for k in packages:
            x = comparison.interaction(k, metric)
            if x["combined_pct"] is not None:
                prow.append([runs[k].label, f"{x['combined_pct']:.0f}%", f"{x['independent_pct']:.0f}%", x["verdict"],
                             f"{money(x['gain_over_best_single'])}·h"])
        parts.append(_table(["Combination", "Removed together", "Expected if independent", "In this run", "Gain over best single"],
                            prow, numeric=(1, 2, 4)))
    if backstop is not None:
        parts.append("<h2>What liquidity can and cannot do</h2>")
        parts.append(f"<p>{_md(backstop.verdict(money))}</p>")
        if backstop.rows:
            parts.append(_table(["Participant", "Pre-funding needed", "Credit-line peak", "First draw (hour)", "Tight"],
                                [[names[r["participant"]], money(r["allocation"]), money(r["peak_draw"]), r["first_draw_hour"],
                                  {True: "yes", False: "no", None: "–"}[r["tight"]]] for r in backstop.rows],
                                numeric=(1, 2, 3)))

    parts.append("<h2>Headline metrics</h2>")
    keys = list(abc) + [k for k in singles if k not in abc]
    parts.append(_table(["Metric", *[runs[k].label for k in keys]],
                        [[label, *[_fmt(summaries[k].get(key), kind, money) for k in keys]] for key, label, kind in HEADLINE_METRICS]))

    parts.append("<h2>Key assumptions</h2><ul>" + "".join(f"<li>{_md(a)}</li>" for a in assumptions) + "</ul>")
    parts.append('<p class="small">The full model note is docs/MODEL.md in the CyberLiquidity repository.</p>')
    parts.append("<h2>Reproduce</h2><p>Download the run bundle from the app (or run <code>cyberliquidity compare</code>); "
                 "each run's <code>scenario.json</code> must reproduce these results digests:</p>")
    parts.append(_table(["Run", "Scenario digest", "Results digest"],
                        [[runs[k].label, scenario_digest(r.scenario)[:24] + "…", r.digest()[:24] + "…"]
                         for k, r in comparison.runs.items() if k in runs]))
    parts.append(f'<p class="small">{html.escape(DISCLAIMER)}</p>')

    page = (
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        f"<title>CyberLiquidity briefing · {html.escape(s.name)}</title><style>{_CSS}</style>"
        f"<script>{plotly.offline.get_plotlyjs()}</script></head><body><main>"
        + "".join(parts) + "</main></body></html>"
    )
    return page.encode("utf-8")


def _fmt(value, kind: str, money) -> str:
    if value is None:
        return "–"
    if kind == "money":
        return money(value)
    if kind == "money_hours":
        return f"{money(value)}·h"
    if kind == "hours":
        return f"{value:.2f}" if isinstance(value, float) else str(value)
    return f"{value:,}" if isinstance(value, int) else str(value)
