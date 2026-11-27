"""Plotly figure builders. Every chart reads recorded simulation results; nothing is hardcoded.

Rules applied (see the dataviz guidance): one y-axis per chart, colour follows the
entity (run type or blocking category), 2px lines, thin bars with rounded ends,
hairline grid, a legend whenever there are two or more series, tooltips on hover.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

import plotly.graph_objects as go
from plotly.subplots import make_subplots

from cyberliquidity.engine import CATEGORIES, SimulationResult
from cyberliquidity.metrics import delay_distribution
from cyberliquidity.models import Currency
from cyberliquidity.sweep import SweepResult

from .theme import CATEGORY_LABELS, FONT, Palette, run_colour


@dataclass(frozen=True)
class RunView:
    key: str
    label: str
    # baseline | stress | backup_route | liquidity_buffer | faster_recovery | credit_line | offsetting
    # | priority_reserve | combination
    kind: str
    result: SimulationResult


def _num_format(currency: Currency) -> str:
    return f",.{currency.minor_units}f"


def _layout(fig: go.Figure, pal: Palette, *, height: int, x_title: str | None = "Hour",
            y_title: str | None = None, legend: bool = True, hovermode: str | bool = "x unified") -> go.Figure:
    fig.update_layout(
        height=height,
        margin=dict(l=8, r=8, t=36 if legend else 12, b=8),
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font=dict(family=FONT, size=12, color=pal.text2),
        showlegend=legend,
        legend=dict(orientation="h", x=0, xanchor="left", y=1.02, yanchor="bottom",
                    font=dict(color=pal.text2), bgcolor="rgba(0,0,0,0)"),
        hovermode=hovermode,
        hoverlabel=dict(bgcolor=pal.surface, bordercolor=pal.axis, font=dict(color=pal.text, family=FONT)),
        barcornerradius=4,
    )
    fig.update_xaxes(showgrid=False, zeroline=False, linecolor=pal.axis, tickcolor=pal.axis, ticks="outside",
                     title_text=x_title, title_font=dict(color=pal.text2), automargin=True, title_standoff=6)
    fig.update_yaxes(gridcolor=pal.grid, gridwidth=1, zeroline=False, linecolor=pal.axis,
                     title_text=y_title, title_font=dict(color=pal.text2), rangemode="tozero",
                     automargin=True, title_standoff=6)
    return fig


def disruption_windows(result: SimulationResult) -> list[tuple[int, int, str]]:
    windows = []
    for d in result.effective_disruptions:
        if d.duration_hours:
            windows.append((d.start_hour, d.end_hour, f"{d.id} outage" if d.severity_pct == 100 else f"{d.id} degraded"))
        if d.recovery_hours:
            windows.append((d.end_hour, d.restored_hour, f"{d.id} recovery"))
    return windows


def _shade(fig: go.Figure, windows: Sequence[tuple[int, int, str]], pal: Palette, **where) -> None:
    for start, end, label in windows:
        fig.add_vrect(x0=start - 0.5, x1=end - 0.5, fillcolor=pal.band, line_width=0, layer="below",
                      annotation_text=label, annotation_position="top left",
                      annotation_font=dict(color=pal.text2, size=10), **where)


def overdue_chart(runs: Sequence[RunView], pal: Palette, currency: Currency, measure: str = "value") -> go.Figure:
    fig = go.Figure()
    fmt = _num_format(currency)
    for rv in runs:
        hours = [h.hour for h in rv.result.hours]
        if measure == "value":
            ys = [currency.to_major(h.overdue_value) for h in rv.result.hours]
            template = f"%{{y:{fmt}}} {currency.code}"
        else:
            ys = [h.overdue_count for h in rv.result.hours]
            template = "%{y} obligations"
        fig.add_trace(go.Scatter(x=hours, y=ys, name=rv.label, mode="lines",
                                 line=dict(color=run_colour(pal, rv.kind), width=2), hovertemplate=template))
    windows = next((disruption_windows(rv.result) for rv in runs if rv.kind == "stress"), [])
    _shade(fig, windows, pal)
    title = f"Overdue value ({currency.code})" if measure == "value" else "Overdue obligations"
    return _layout(fig, pal, height=320, y_title=title)


def delay_histogram(runs: Sequence[RunView], pal: Palette, include_on_time: bool = False) -> go.Figure:
    fig = go.Figure()
    any_bar = False
    for rv in runs:
        rows = [r for r in delay_distribution(rv.result) if include_on_time or r["delay_hours"] > 0]
        any_bar = any_bar or bool(rows)
        fig.add_trace(go.Bar(x=[r["delay_hours"] for r in rows], y=[r["count"] for r in rows], name=rv.label,
                             marker=dict(color=run_colour(pal, rv.kind)),
                             hovertemplate="%{y} payments settled %{x} h late"))
    fig.update_layout(barmode="group", bargap=0.35, bargroupgap=0.1)
    if not any_bar:
        fig.add_annotation(text="No completed payment was late", showarrow=False, x=0.5, y=0.5,
                           xref="paper", yref="paper", font=dict(color=pal.text2))
    return _layout(fig, pal, height=320, x_title="Settlement delay (hours)", y_title="Completed payments",
                   hovermode="closest")


def blocked_by_reason(result: SimulationResult, pal: Palette, currency: Currency, measure: str = "value") -> go.Figure:
    fig = go.Figure()
    fmt = _num_format(currency)
    hours = [h.hour for h in result.hours]
    for cat in CATEGORIES:
        if measure == "value":
            ys = [currency.to_major(h.category_values[cat]) for h in result.hours]
            template = f"%{{y:{fmt}}} {currency.code}"
        else:
            ys = [h.category_counts[cat] for h in result.hours]
            template = "%{y} obligations"
        fig.add_trace(go.Bar(x=hours, y=ys, name=CATEGORY_LABELS[cat], marker=dict(color=pal.categories[cat], line=dict(width=0)),
                             hovertemplate=template))
    fig.update_layout(barmode="stack", bargap=0.15)
    _shade(fig, disruption_windows(result), pal)
    y = f"Overdue value ({currency.code})" if measure == "value" else "Overdue obligations"
    return _layout(fig, pal, height=340, y_title=y)


def service_heatmap(result: SimulationResult, pal: Palette) -> go.Figure:
    services = result.scenario.services
    hours = [h.hour for h in result.hours]
    z, text = [], []
    for svc in services:
        row, row_text = [], []
        for hour, status in enumerate(result.service_status):
            st = status[svc.id]
            used = result.hours[hour].service_used.get(svc.id, 0)
            row.append(100 - st.pct)
            cap = "not throughput-limited" if st.capacity is None else f"capacity {st.capacity}/h, used {used}"
            row_text.append(f"{svc.name}<br>hour {hour}: {st.state.value} ({st.pct}% capacity)<br>{cap}")
        z.append(row)
        text.append(row_text)
    n = len(pal.sequential)
    scale = [[i / (n - 1), c] for i, c in enumerate(pal.sequential)]
    fig = go.Figure(go.Heatmap(
        z=z, x=hours, y=[s.name for s in services], zmin=0, zmax=100, colorscale=scale, xgap=1, ygap=2,
        hovertext=text, hoverinfo="text",
        colorbar=dict(title=dict(text="Capacity lost %", font=dict(color=pal.text2)), tickfont=dict(color=pal.text2),
                      thickness=10, outlinewidth=0),
    ))
    fig.update_yaxes(autorange="reversed", gridcolor="rgba(0,0,0,0)")
    return _layout(fig, pal, height=60 + 34 * len(services), legend=False, hovermode="closest")


def capacity_usage(result: SimulationResult, service_id: str, pal: Palette) -> go.Figure:
    hours = [h.hour for h in result.hours]
    used = [h.service_used.get(service_id, 0) for h in result.hours]
    caps = [status[service_id].capacity for status in result.service_status]
    fig = go.Figure()
    fig.add_trace(go.Bar(x=hours, y=used, name="Payments processed", marker=dict(color=pal.runs["stress"]),
                         hovertemplate="%{y} processed"))
    if any(c is not None for c in caps):
        fig.add_trace(go.Scatter(x=hours, y=caps, name="Effective capacity", mode="lines",
                                 line=dict(color=pal.text2, width=2, shape="hvh"), hovertemplate="capacity %{y}"))
    fig.update_layout(bargap=0.2)
    _shade(fig, disruption_windows(result), pal)
    return _layout(fig, pal, height=280, y_title="Payments per hour")


def balance_multiples(runs: Sequence[RunView], participants: Sequence[str], names: dict[str, str], pal: Palette,
                      currency: Currency) -> go.Figure:
    cols = 3 if len(participants) > 2 else max(1, len(participants))
    rows = max(1, math.ceil(len(participants) / cols))
    fig = make_subplots(rows=rows, cols=cols, shared_xaxes=True, subplot_titles=[names[p] for p in participants],
                        vertical_spacing=min(0.12, 0.5 / rows), horizontal_spacing=0.06)
    fmt = _num_format(currency)
    for index, pid in enumerate(participants):
        r, c = divmod(index, cols)
        for rv in runs:
            ys = [currency.to_major(h.balances[pid]) for h in rv.result.hours]
            fig.add_trace(go.Scatter(x=[h.hour for h in rv.result.hours], y=ys, name=rv.label, legendgroup=rv.key,
                                     showlegend=index == 0, mode="lines",
                                     line=dict(color=run_colour(pal, rv.kind), width=1.6),
                                     hovertemplate=f"%{{y:{fmt}}} {currency.code}"),
                          row=r + 1, col=c + 1)
    fig.update_annotations(font=dict(color=pal.text, size=12))
    height = 110 + 190 * rows
    _layout(fig, pal, height=height, x_title=None)
    # Keep the shared legend clear of the first row's panel titles.
    fig.update_layout(margin=dict(t=84), legend=dict(y=1 + 52 / max(1, height - 110), yanchor="bottom"))
    fig.update_xaxes(title_text=None)
    return fig


def mechanism_bars(runs: Sequence[RunView], summaries: dict[str, dict], pal: Palette, currency: Currency) -> go.Figure:
    fig = go.Figure()
    labels = [rv.label for rv in runs]
    fmt = _num_format(currency)
    for cat in CATEGORIES:
        values = [currency.to_major(summaries[rv.key]["category_value_hours"][cat]) for rv in runs]
        fig.add_trace(go.Bar(y=labels, x=values, name=CATEGORY_LABELS[cat], orientation="h",
                             marker=dict(color=pal.categories[cat], line=dict(width=2, color=pal.surface)),
                             hovertemplate=f"%{{x:{fmt}}} {currency.code}·h"))
    fig.update_layout(barmode="stack", bargap=0.45)
    fig.update_yaxes(autorange="reversed", gridcolor="rgba(0,0,0,0)")
    fig.update_xaxes(showgrid=True, gridcolor=pal.grid)
    return _layout(fig, pal, height=110 + 46 * len(runs), x_title=f"Overdue value × hours ({currency.code}·h)",
                   hovermode="closest")


def intervention_bars(rows: Sequence[dict], pal: Palette, x_title: str, value_text) -> go.Figure:
    """rows: dicts with label, kind, value (already in display units)."""
    fig = go.Figure(go.Bar(
        y=[r["label"] for r in rows], x=[r["value"] for r in rows], orientation="h",
        marker=dict(color=[run_colour(pal, r["kind"]) for r in rows]),
        text=[value_text(r["raw"]) for r in rows], textposition="outside", textfont=dict(color=pal.text),
        cliponaxis=False, hovertemplate="%{y}: %{text}<extra></extra>",
    ))
    fig.update_layout(bargap=0.45)
    fig.update_yaxes(autorange="reversed", gridcolor="rgba(0,0,0,0)")
    fig.update_xaxes(showgrid=True, gridcolor=pal.grid)
    _layout(fig, pal, height=90 + 44 * len(rows), x_title=x_title, legend=False, hovermode="closest")
    fig.update_layout(margin=dict(r=90))
    return fig


def sweep_chart(sweep: SweepResult, pal: Palette, currency: Currency) -> go.Figure:
    xs = [currency.to_major(p.amount_each) for p in sweep.points]
    ys = [currency.to_major(p.summary["overdue_value_hours"]) for p in sweep.points]
    fmt = _num_format(currency)
    colour = pal.runs["liquidity_buffer"]
    fig = go.Figure(go.Scatter(
        x=xs, y=ys, mode="lines+markers", name="Overdue value × hours",
        line=dict(color=colour, width=2),
        marker=dict(size=10, color=[colour if p.achieved else pal.surface for p in sweep.points],
                    line=dict(color=colour, width=2)),
        customdata=[["achieved" if p.achieved else "not achieved", p.summary["delayed_count"]] for p in sweep.points],
        hovertemplate=(f"buffer %{{x:{fmt}}} {currency.code} each<br>%{{y:{fmt}}} {currency.code}·h"
                       "<br>%{customdata[1]} delayed · outcome %{customdata[0]}<extra></extra>"),
    ))
    if sweep.smallest_achieving is not None:
        p = sweep.smallest_achieving
        fig.add_annotation(x=currency.to_major(p.amount_each), y=currency.to_major(p.summary["overdue_value_hours"]),
                           text="smallest tested buffer achieving the outcome", showarrow=True, arrowcolor=pal.text2,
                           font=dict(color=pal.text2, size=11), ax=40, ay=-40)
    return _layout(fig, pal, height=300, x_title=f"Buffer per funded participant ({currency.code})",
                   y_title=f"Overdue value × hours ({currency.code}·h)", legend=False, hovermode="closest")


# ------------------------------------------------------------- v0.2 charts


def generation_bars(cascade, pal: Palette, currency: Currency) -> go.Figure:
    """Delayed value by contagion generation (0 = direct). Position carries the step; colour the kind."""
    from .theme import KIND_CATEGORY

    rows = cascade.by_generation
    labels = [("Unlinked cash shortfall" if r["generation"] is None else
               "Step 0 · direct" if r["generation"] == 0 else f"Step {r['generation']} · contagion") for r in rows]
    fmt = _num_format(currency)
    fig = go.Figure()
    for kind, name in (("direct", "Direct (operational block)"), ("propagated", "Propagated (payer short of cash)"),
                       ("unlinked", "Cash shortfall, no recorded link")):
        xs = [currency.to_major(r["value"]) if r["kind"] == kind else None for r in rows]
        if not any(x is not None for x in xs):
            continue
        fig.add_trace(go.Bar(
            y=labels, x=xs, name=name, orientation="h",
            marker=dict(color=pal.categories[KIND_CATEGORY[kind]], line=dict(width=0)),
            customdata=[[r["count"], ", ".join(r["payers"][:4]) + (" …" if len(r["payers"]) > 4 else "")] for r in rows],
            hovertemplate=f"%{{x:{fmt}}} {currency.code} in %{{customdata[0]}} payment(s)<br>payers: %{{customdata[1]}}<extra></extra>",
        ))
    fig.update_layout(barmode="overlay", bargap=0.4)
    fig.update_yaxes(autorange="reversed", gridcolor="rgba(0,0,0,0)")
    fig.update_xaxes(showgrid=True, gridcolor=pal.grid)
    return _layout(fig, pal, height=120 + 40 * max(1, len(rows)), x_title=f"Delayed value ({currency.code})",
                   hovermode="closest")


def criticality_bars(rows: Sequence[dict], names: dict[str, str], pal: Palette, currency: Currency,
                     selected: str | None = None) -> go.Figure:
    """Overdue value x hours if each service fails in turn (same timing). One hue; selected service marked."""
    fmt = _num_format(currency)
    labels = [names[r["service"]] + ("  ◀ B" if r["service"] == selected else "") for r in rows]
    fig = go.Figure(go.Bar(
        y=labels, x=[currency.to_major(r["overdue_value_hours"]) for r in rows], orientation="h",
        marker=dict(color=pal.runs["stress"], line=dict(width=0)),
        customdata=[[r["delayed_count"], r["participants_cash_short_count"],
                     "-" if r["max_generation"] is None else r["max_generation"], len(r["routes_unavailable"])] for r in rows],
        hovertemplate=(f"%{{x:{fmt}}} {currency.code}·h<br>%{{customdata[0]}} delayed · %{{customdata[1]}} short of cash"
                       "<br>contagion steps %{customdata[2]} · routes down %{customdata[3]}<extra></extra>"),
    ))
    fig.update_layout(bargap=0.4)
    fig.update_yaxes(autorange="reversed", gridcolor="rgba(0,0,0,0)")
    fig.update_xaxes(showgrid=True, gridcolor=pal.grid)
    return _layout(fig, pal, height=90 + 38 * len(rows), x_title=f"Overdue value × hours ({currency.code}·h)",
                   legend=False, hovermode="closest")


def profile_lines(rows: Sequence[dict], x_key: str, x_title: str, plan_labels: dict[str, str], plan_kinds: dict[str, str],
                  pal: Palette, currency: Currency, marker_x: int | None = None, marker_label: str = "") -> go.Figure:
    """Overdue value x hours against outage duration or start hour, one line per plan."""
    fmt = _num_format(currency)
    fig = go.Figure()
    for plan in dict.fromkeys(r["plan"] for r in rows):
        series = [r for r in rows if r["plan"] == plan]
        fig.add_trace(go.Scatter(
            x=[r[x_key] for r in series], y=[currency.to_major(r["overdue_value_hours"]) for r in series],
            name=plan_labels.get(plan, plan), mode="lines+markers",
            # dotted, because it coincides with an equal-sized buffer (a model property) and would hide it
            line=dict(color=run_colour(pal, plan_kinds.get(plan, "stress")), width=2,
                      dash="dot" if plan_kinds.get(plan) == "credit_line" else "solid"),
            marker=dict(size=8, line=dict(width=2, color=pal.surface)),
            customdata=[[r["delayed_count"], "achieved" if r.get("outcome_achieved") else "breached"
                         if "outcome_achieved" in r else "-"] for r in series],
            hovertemplate=f"%{{y:{fmt}}} {currency.code}·h · %{{customdata[0]}} delayed · outcome %{{customdata[1]}}",
        ))
    if marker_x is not None:
        fig.add_vline(x=marker_x, line=dict(color=pal.text2, width=1, dash="dot"),
                      annotation_text=marker_label, annotation_position="top", annotation_font=dict(color=pal.text2, size=10))
    return _layout(fig, pal, height=340, x_title=x_title, y_title=f"Overdue value × hours ({currency.code}·h)")


def surface_heatmap(surface: dict, pal: Palette, currency: Currency) -> go.Figure:
    fmt = _num_format(currency)
    z = [[currency.to_major(v) for v in row] for row in surface["z"]]
    n = len(pal.sequential)
    scale = [[i / (n - 1), c] for i, c in enumerate(pal.sequential)]
    fig = go.Figure(go.Heatmap(
        z=z, x=surface["starts"], y=[f"{d} h" for d in surface["durations"]], colorscale=scale, xgap=1, ygap=2,
        hovertemplate=f"start hour %{{x}} · %{{y}} outage<br>%{{z:{fmt}}} {currency.code}·h<extra></extra>",
        colorbar=dict(title=dict(text=f"{currency.code}·h", font=dict(color=pal.text2)), tickfont=dict(color=pal.text2),
                      thickness=10, outlinewidth=0),
    ))
    fig.update_yaxes(gridcolor="rgba(0,0,0,0)")
    return _layout(fig, pal, height=110 + 36 * len(surface["durations"]), x_title="Outage start hour",
                   y_title="Duration", legend=False, hovermode="closest")


def package_dumbbell(rows: Sequence[dict], pal: Palette) -> go.Figure:
    """Each combination: removal expected if its parts acted independently (hollow) vs actual (filled)."""
    fig = go.Figure()
    labels = [r["label"] for r in rows]
    for r, label in zip(rows, labels):
        fig.add_trace(go.Scatter(x=[r["independent_pct"], r["combined_pct"]], y=[label, label], mode="lines",
                                 line=dict(color=pal.axis, width=2), showlegend=False, hoverinfo="skip"))
    fig.add_trace(go.Scatter(x=[r["independent_pct"] for r in rows], y=labels, mode="markers", name="If independent",
                             marker=dict(size=11, color=pal.surface, line=dict(color=pal.runs["combination"], width=2)),
                             hovertemplate="independent benchmark %{x:.0f}%<extra></extra>"))
    fig.add_trace(go.Scatter(x=[r["combined_pct"] for r in rows], y=labels, mode="markers", name="Actual combination",
                             marker=dict(size=11, color=pal.runs["combination"], line=dict(color=pal.surface, width=2)),
                             hovertemplate="actual %{x:.0f}%<extra></extra>"))
    fig.update_yaxes(autorange="reversed", gridcolor="rgba(0,0,0,0)")
    low = min([0.0] + [r["independent_pct"] for r in rows] + [r["combined_pct"] for r in rows])  # a rule can make it worse
    fig.update_xaxes(showgrid=True, gridcolor=pal.grid, range=[low - 2 if low < 0 else 0, 102], ticksuffix="%")
    return _layout(fig, pal, height=110 + 46 * len(rows), x_title="Share of the stress disruption removed (overdue value × hours)",
                   hovermode="closest")


def robustness_heatmap(robust: dict, labels: dict[str, str], pal: Palette, currency: Currency) -> go.Figure:
    """Assumption variants (rows) x interventions (columns): share of each variant's stress disruption removed.

    Diverging scale (blue = less disruption, red = more, grey = no change), clipped at
    +-100 %; the exact value is printed in every cell and the best option per row is
    starred, so colour never carries the reading alone.
    """
    plans = list(robust["plans"])
    rows = robust["variants"]

    def wrap(text: str) -> str:  # two short lines read better than a rotated label
        words = text.split()
        if len(words) < 2 or len(text) <= 14:
            return text
        cut = min(range(1, len(words)), key=lambda i: abs(len(" ".join(words[:i])) - len(text) / 2))
        return " ".join(words[:cut]) + "<br>" + " ".join(words[cut:])

    x = [wrap(labels.get(k, k)) for k in plans]
    y = [r["label"] for r in rows]
    z, text, ink, hover = [], [], [], []
    fmt = _num_format(currency)
    for r in rows:
        zr, tr, ir, hr = [], [], [], []
        for k in plans:
            pct = r["reduction_pct"].get(k)
            value = r["values"].get(k)
            star = " ★" if k in r["best"] and r["stress_value"] and len(r["best"]) < len(plans) else ""
            if pct is None:
                tr.append("–" if value is None else ("no change" if value == 0 else "worse"))
                zr.append(None if value is None or value == 0 else -100)
            else:
                tr.append(f"{pct:.0f}%{star}")
                zr.append(max(-100.0, min(100.0, pct)))
            strength = abs(zr[-1]) if zr[-1] is not None else 0
            # dark steps in light mode and bright steps in dark mode both need the opposite ink
            ir.append(pal.surface if strength >= 60 else pal.text)
            hr.append(f"{r['label']}<br>{labels.get(k, k)}: "
                      + ("not applicable" if value is None else
                         f"{currency.to_major(value):{fmt}} {currency.code}·h against "
                         f"{currency.to_major(r['stress_value']):{fmt}} without an intervention"))
        z.append(zr)
        text.append(tr)
        ink.append(ir)
        hover.append(hr)
    n = len(pal.diverging)
    scale = [[i / (n - 1), c] for i, c in enumerate(pal.diverging)]
    fig = go.Figure(go.Heatmap(
        z=z, x=x, y=y, zmin=-100, zmax=100, zmid=0, colorscale=scale, xgap=2, ygap=2, customdata=hover,
        hovertemplate="%{customdata}<extra></extra>",
        colorbar=dict(title=dict(text="Removed", font=dict(color=pal.text2)), tickfont=dict(color=pal.text2),
                      ticksuffix="%", thickness=10, outlinewidth=0, tickvals=[-100, -50, 0, 50, 100]),
    ))
    fig.add_trace(go.Scatter(
        x=[xx for row in y for xx in x], y=[row for row in y for _ in x], mode="text",
        text=[cell for row in text for cell in row], textfont=dict(color=[c for row in ink for c in row], size=12),
        hoverinfo="skip", showlegend=False,
    ))
    fig.update_yaxes(autorange="reversed", gridcolor="rgba(0,0,0,0)", rangemode="normal")
    fig.update_xaxes(side="top", title_text=None, tickangle=0)
    return _layout(fig, pal, height=110 + 34 * len(rows), x_title=None, legend=False, hovermode="closest")


def backstop_bars(backstop, names: dict[str, str], pal: Palette, currency: Currency) -> go.Figure:
    """Per participant: pre-funding actually needed vs peak draw on an unlimited credit line."""
    fmt = _num_format(currency)
    rows = list(backstop.rows)
    labels = [names[r["participant"]] for r in rows]
    fig = go.Figure()
    fig.add_trace(go.Bar(y=labels, x=[currency.to_major(r["allocation"]) for r in rows], name="Pre-funding needed (hour 0)",
                         orientation="h", marker=dict(color=pal.runs["liquidity_buffer"], line=dict(width=2, color=pal.surface)),
                         hovertemplate=f"%{{x:{fmt}}} {currency.code}<extra>pre-funding</extra>"))
    fig.add_trace(go.Bar(y=labels, x=[currency.to_major(r["peak_draw"]) for r in rows], name="Peak draw on an unlimited credit line",
                         orientation="h", marker=dict(color=pal.runs["credit_line"], line=dict(width=2, color=pal.surface)),
                         hovertemplate=f"%{{x:{fmt}}} {currency.code}<extra>credit-line peak</extra>"))
    fig.update_layout(barmode="group", bargap=0.35, bargroupgap=0.08)
    fig.update_yaxes(autorange="reversed", gridcolor="rgba(0,0,0,0)")
    fig.update_xaxes(showgrid=True, gridcolor=pal.grid)
    return _layout(fig, pal, height=120 + 56 * max(1, len(rows)), x_title=f"Liquidity ({currency.code})", hovermode="closest")


def credit_outstanding(runs: Sequence[RunView], pal: Palette, currency: Currency) -> go.Figure:
    fmt = _num_format(currency)
    fig = go.Figure()
    for rv in runs:
        fig.add_trace(go.Scatter(x=[h.hour for h in rv.result.hours],
                                 y=[currency.to_major(sum(h.credit_drawn.values())) for h in rv.result.hours],
                                 name=rv.label, mode="lines", line=dict(color=run_colour(pal, rv.kind), width=2, shape="hv"),
                                 hovertemplate=f"%{{y:{fmt}}} {currency.code} outstanding"))
    _shade(fig, next((disruption_windows(rv.result) for rv in runs), []), pal)
    return _layout(fig, pal, height=260, y_title=f"Credit outstanding ({currency.code})",
                   legend=len(runs) > 1)
