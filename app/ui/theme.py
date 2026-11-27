"""Colour roles for light and dark mode.

Values follow the dataviz reference palette; both categorical sets below were run
through its validator (adjacent CVD separation passes in both modes; a few light-mode
hues sit under 3:1 contrast, so every chart ships a legend and a table view). Colour
follows the *entity* within a chart family: a run type keeps its colour in every run
chart, a blocking category in every category chart. Runs take categorical slots 1-7 in
fixed order (stress, backup, buffer, faster recovery, credit line, offsetting, reserve;
the seven were re-validated together in both modes). Slots 5-7 also name blocking
categories, but no chart shows run colours and category colours together.
Combinations of interventions use a neutral ink. Charts that compare all seven runs
at once on one row (the robustness grid) use the diverging blue-red scale and printed
values instead of categorical hues, because only three slots stay separable when every
pair can touch.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Palette:
    mode: str
    surface: str
    text: str
    text2: str
    grid: str
    axis: str
    band: str  # shading for disruption windows
    runs: dict[str, str] = field(default_factory=dict)
    categories: dict[str, str] = field(default_factory=dict)
    status: dict[str, str] = field(default_factory=dict)
    status_fill: dict[str, str] = field(default_factory=dict)
    sequential: tuple[str, ...] = ()
    diverging: tuple[str, ...] = ()  # worse (red) ... neutral ... better (blue), three steps per arm


_SEQ = ("#cce3fd", "#9dc8f7", "#6aabf1", "#338ced", "#2370cd", "#1757ac", "#0e3d7e")

LIGHT = Palette(
    mode="light", surface="#ffffff", text="#191919", text2="#666666", grid="#e7e7e7", axis="#c2c2c2",
    band="rgba(25,25,25,0.06)",
    runs={"baseline": "#8c8c8c", "stress": "#257de0", "backup_route": "#f47230",
          "liquidity_buffer": "#00b894", "faster_recovery": "#f2a600", "credit_line": "#ee7fac",
          "offsetting": "#008a17", "priority_reserve": "#6a4ce2", "combination": "#3d3d3d"},
    categories={"operational_only": "#6a4ce2", "operational_and_cash": "#ee7fac",
                "cash_awaiting_inflows": "#008a17", "cash_other": "#8c8c8c"},
    status={"available": "#0aac23", "degraded": "#fbb315", "unavailable": "#dc3c3d", "serious": "#f18a59"},
    status_fill={"available": "#ffffff", "degraded": "#fff3d4", "unavailable": "#fde3e3", "serious": "#fde8dc",
                 "neutral": "#f4f4f4"},
    sequential=_SEQ,
    diverging=("#a82526", "#ec4647", "#f6b4b4", "#efefef", "#9dc8f7", "#338ced", "#1757ac"),
)

DARK = Palette(
    mode="dark", surface="#191919", text="#efefef", text2="#9c9c9c", grid="#303030", axis="#474747",
    band="rgba(239,239,239,0.07)",
    runs={"baseline": "#8c8c8c", "stress": "#338ced", "backup_route": "#e76624",
          "liquidity_buffer": "#00a888", "faster_recovery": "#d28e05", "credit_line": "#e24d8b",
          "offsetting": "#0aac23", "priority_reserve": "#9887ef", "combination": "#cfcfcf"},
    categories={"operational_only": "#9887ef", "operational_and_cash": "#e24d8b",
                "cash_awaiting_inflows": "#0aac23", "cash_other": "#8c8c8c"},
    status={"available": "#0aac23", "degraded": "#fbb315", "unavailable": "#dc3c3d", "serious": "#f18a59"},
    status_fill={"available": "#191919", "degraded": "#3a3015", "unavailable": "#3d1a1a", "serious": "#3b2418",
                 "neutral": "#242424"},
    # Low values ("no capacity lost") recede toward the dark surface; losses brighten.
    sequential=("#1e2733", "#1b3a63", "#1757ac", "#2370cd", "#338ced", "#6aabf1", "#b6d5f8"),
    # Larger effects brighten away from the dark surface on both arms.
    diverging=("#f7c1c1", "#eb6263", "#5c2323", "#383838", "#1b3a63", "#338ced", "#b6d5f8"),
)

CATEGORY_LABELS = {
    "operational_only": "Operational only (route unavailable / at capacity)",
    "operational_and_cash": "Operational and insufficient cash",
    "cash_awaiting_inflows": "Insufficient cash, awaiting overdue inflows (propagation)",
    "cash_other": "Insufficient cash, no overdue inflows",
}

STATUS_ICON = {"available": "✓", "degraded": "⚠", "unavailable": "✕"}

# Contagion kinds reuse the blocking-category colours: direct = operational, propagated = cash awaiting inflows.
KIND_CATEGORY = {"direct": "operational_only", "propagated": "cash_awaiting_inflows", "unlinked": "cash_other"}

FONT = "system-ui, -apple-system, 'Segoe UI', sans-serif"


def palette_for(theme_type: str | None) -> Palette:
    return DARK if theme_type == "dark" else LIGHT


def run_colour(pal: Palette, run_kind: str) -> str:
    return pal.runs.get(run_kind, pal.runs["stress"])
