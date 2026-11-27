"""Page-level helpers for the interface: error boundaries, plain-text rendering, chart data tables,
and the in-app guide and glossary.

Nothing here computes results; it only decides how recorded results are shown.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterator
from contextlib import contextmanager

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

log = logging.getLogger("cyberliquidity.app")


@contextmanager
def guarded(section: str) -> Iterator[None]:
    """Error boundary: an unexpected error in one section is reported there, and the rest of the page still works.

    Streamlit's own control flow (``st.stop``, ``st.rerun``) raises ``BaseException`` subclasses, which pass
    through untouched. The full traceback goes to the terminal log only, never to the page.
    """
    try:
        yield
    except Exception as exc:
        log.exception("Error while rendering %s", section)
        st.error(f"**{section} could not be shown.** The rest of the app still works. "
                 "Try another setting or reload the page; the details are in the terminal window that runs the app.",
                 icon=":material/error:")
        st.caption(f"Error type: `{type(exc).__name__}`")


@st.fragment(run_every=2)
def follow_theme_changes() -> None:
    """Rerun the page once when the viewer switches between light and dark in the menu.

    Charts and buttons are styled for the active theme, but switching the theme does not rerun the script.
    This small fragment compares the theme every two seconds and reruns the page only when it changed.
    """
    current = getattr(st.context.theme, "type", None)
    seen = st.session_state.get("_theme_seen")
    st.session_state["_theme_seen"] = current
    if seen is not None and current is not None and current != seen:
        st.rerun()


_MARKDOWN_SPECIAL = re.compile(r"([\\`*_{}\[\]()<>#+\-.!|~$:])")


def plain(text: str) -> str:
    """Escape Markdown so text from a scenario file is shown literally (no links, formatting or icons)."""
    return _MARKDOWN_SPECIAL.sub(r"\\\1", text)


def figure_table(fig: go.Figure) -> pd.DataFrame | None:
    """The numbers behind a bar or line chart, read from the figure itself (so the table always matches the chart).

    One row per category or x value, one column per series. Returns None for charts without x/y series.
    """
    columns: dict[str, dict] = {}
    order: list = []
    horizontal = False
    for trace in fig.data:
        if trace.type not in ("bar", "scatter") or trace.x is None or trace.y is None:
            continue
        horizontal = getattr(trace, "orientation", None) == "h"
        keys, values = (trace.y, trace.x) if horizontal else (trace.x, trace.y)
        name = trace.name or f"Series {len(columns) + 1}"
        series = columns.setdefault(name, {})
        for key, value in zip(keys, values):
            if key not in series and key not in order:
                order.append(key)
            series[key] = value
    if not columns:
        return None
    axis = fig.layout.yaxis if horizontal else fig.layout.xaxis
    first = (axis.title.text if axis and axis.title and axis.title.text else "Category")
    table = pd.DataFrame({first: order})
    for name, series in columns.items():
        table[name] = [series.get(key) for key in order]
    return table


def show_chart(fig: go.Figure, key: str, *, table: bool = True, note: str | None = None) -> None:
    """A Plotly chart plus, for screen readers and exact values, a collapsible table of the same numbers."""
    st.plotly_chart(fig, theme=None, key=key, config={"displaylogo": False})
    if not table:
        return
    data = figure_table(fig)
    if data is None:
        return
    with st.expander("Show the numbers as a table", icon=":material/table_view:"):
        value_axis = fig.layout.xaxis if any(getattr(t, "orientation", None) == "h" for t in fig.data) else fig.layout.yaxis
        unit = value_axis.title.text if value_axis and value_axis.title and value_axis.title.text else None
        if unit:
            st.caption(f"Values: {unit}" + (f". {note}" if note else ""))
        st.dataframe(data, hide_index=True, key=f"{key}__table")


QUICK_START = [
    ("Pick a scenario", "Start with **Illustrative regional supply chain** (the default). Every organisation and number in "
                        "it is fictional."),
    ("Choose what goes wrong (run B)", "Pick the service that fails, when the outage starts and how long it lasts. "
                                       "Run A is the same scenario with nothing going wrong."),
    ("Choose a response (run C)", "Pick one of six interventions, such as a backup payment provider or a cash buffer. "
                                  "All six are always compared against the same run B."),
    ("Read the answer", "The **Overview** tab says in plain language how many payments are late, who runs short of cash "
                        "and which response helps most. Every tab after it shows the evidence."),
]

GLOSSARY = {
    "Obligation / payment": "Money one business owes another, due in a given hour (wages, invoices, tax, loan repayments).",
    "Service": "Something a payment needs to work: a cloud region, an identity provider, a payment provider or a bank.",
    "Route": "The chain of services a payment travels through. If any service on the route is down, the payment waits.",
    "Run A / B / C": "A = baseline (no disruption). B = the same scenario with the disruption. C = run B plus one response.",
    "On time / delayed": "On time means settled by the end of the hour it was due. Delayed means settled later, or not at all "
                         "within the time window.",
    "Held up directly": "The payment's route needs a service that the disruption took down or overloaded.",
    "Contagion (held up indirectly)": "The payer's route works, but the payer is short of cash because money it was "
                                      "expecting from others is itself delayed.",
    "Contagion depth (steps)": "How many links the shortage travelled: step 1 waits for a payment that was held up "
                               "directly, step 2 waits for a step-1 payment, and so on.",
    "Gridlock": "Payments that wait for each other in a circle: each payer needs the money another payer is waiting to send.",
    "Overdue value × hours": "The value of late payments multiplied by how many hours they were late. A measure of how "
                             "intense the disruption was, not a loss estimate.",
    "Peak overdue value": "The largest total value of late payments at the end of any one hour.",
    "Liquidity buffer": "Cash paid to businesses in advance, so they can keep paying while incoming money is late.",
    "Committed credit line": "A loan facility drawn automatically only when a payment would otherwise fail for lack of cash.",
    "Backup payment provider": "A second payment route that takes over after an activation delay.",
    "Multilateral offsetting": "The payment system settles a group of waiting payments together, so that only the net "
                               "amounts have to be covered.",
    "Payroll-protection reserve": "A treasury rule that holds back routine payments to keep cash for urgent ones (payroll, "
                                  "tax). It adds no money.",
    "Liquidity backstop": "The smallest advance funding per business that makes lack of cash irrelevant in this run, "
                          "found by exact search. Payments held up by a broken route stay late anyway.",
    "Robustness check": "The same comparison repeated under ten changed assumptions (other timing, balances, capacity, "
                        "settlement rules) to see whether the best response stays best.",
    "Minimax regret": "Choosing the response whose worst shortfall against the best response, across all variants, is "
                      "smallest.",
    "Digest": "A fingerprint of a scenario or result. The same input always gives the same digest, so anyone can check "
              "that a run was reproduced exactly.",
}


def render_quick_start(columns: bool = True) -> None:
    if columns:
        cols = st.columns(len(QUICK_START))
        for i, (col, (title, text)) in enumerate(zip(cols, QUICK_START), start=1):
            with col.container(border=True, height="stretch"):
                st.markdown(f"**{i} · {title}**")
                st.caption(text)
    else:
        for i, (title, text) in enumerate(QUICK_START, start=1):
            st.markdown(f"**{i} · {title}.** {text}")


def render_glossary() -> None:
    st.markdown("\n".join(f"- **{term}**: {meaning}" for term, meaning in GLOSSARY.items()))
