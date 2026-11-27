"""CyberLiquidity - local research interface.

Run from the repository root (venv active):  streamlit run app/streamlit_app.py
(or double-click start.command, or `make app`).

The interface only *configures* runs and *displays* recorded results. All numbers
come from the engine in ``src/cyberliquidity``; nothing shown here is hardcoded.
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
try:  # the package is normally installed with `pip install -e .`
    import cyberliquidity  # noqa: F401
except ImportError:  # fallback when running from a fresh checkout
    sys.path.insert(0, str(ROOT / "src"))

import pandas as pd
import streamlit as st
from pydantic import TypeAdapter, ValidationError

from cyberliquidity import DISCLAIMER, Scenario, Status, __version__
from cyberliquidity.analysis import criticality_scan, duration_profile, impact_surface, robustness, start_profile
from cyberliquidity.audit import audit
from cyberliquidity.briefing import briefing, disruption_phrase, robustness_paragraph
from cyberliquidity.cascade import KIND_LABELS, analyse_cascade
from cyberliquidity.compare import INTERVENTION_LABELS, compare, run_key
from cyberliquidity.engine import CATEGORIES
from cyberliquidity.explain import Explainer
from cyberliquidity.export import bundle_zip, scenario_digest, scenario_json
from cyberliquidity.metrics import HEADLINE_METRICS, LOWER_IS_BETTER, RANKING_LABELS, mechanism_shares, obligation_rows
from cyberliquidity.models import Disruption, Intervention
from cyberliquidity.operational import OperationalModel
from cyberliquidity.safety import APP_LIMITS, ScenarioInputError, friendly_errors, parse_scenario
from cyberliquidity.sweep import OUTCOMES, buffer_sweep, liquidity_backstop
from cyberliquidity.variants import (
    make_backup,
    make_buffer,
    make_credit_line,
    make_disruption,
    make_faster_recovery,
    make_offsetting,
    make_reserve,
    replace,
    suggest_backup,
)
from ui import brand, charts, network
from ui.charts import RunView
from ui.layout import follow_theme_changes, guarded, plain, render_glossary, render_quick_start, show_chart
from ui.report import build_report
from ui.theme import CATEGORY_LABELS, STATUS_ICON, palette_for

st.set_page_config(
    page_title="CyberLiquidity", page_icon=":material/water_drop:", layout="wide",
    menu_items={"About": f"**CyberLiquidity {__version__}**: a deterministic research simulator that follows a cyber "
                         f"outage through payment delays to cash shortfalls. {DISCLAIMER}"},
)
pal = palette_for(getattr(st.context.theme, "type", None))
# Filled buttons only in light mode: white on the light-mode accent is 7:1, but on the dark-mode accent only 3.4:1
# (below WCAG AA for text), so dark mode uses outlined buttons and keeps the accent for text (5.1:1).
MAIN_BUTTON = "primary" if pal.mode == "light" else "secondary"
follow_theme_changes()
st.logo(brand.wordmark(pal), icon_image=brand.mark(pal), size="large")

# Keep Stress-lab settings when that (lazily rendered) tab is closed and reopened.
for _key in [k for k in st.session_state if isinstance(k, str) and k.startswith("lab:")]:
    st.session_state[_key] = st.session_state[_key]

SCENARIO_DIR = ROOT / "scenarios"
MODEL_NOTE = ROOT / "docs" / "MODEL.md"
UPLOAD = "Upload your own scenario…"
KINDS = ["backup_route", "liquidity_buffer", "credit_line", "faster_recovery", "offsetting", "priority_reserve"]
INTERVENTIONS = TypeAdapter(list[Intervention])

KEY_ASSUMPTIONS = [
    "Synthetic, uncalibrated network. Amounts are integer minor units of one currency; no FX.",
    "Hourly steps. A payment due in hour *h* is on time if it settles by the end of hour *h*; it cannot settle earlier.",
    "Payments settle in full or not at all: no partial payments or overdrafts. Intraday credit exists only as the "
    "explicit committed-credit-line intervention, and netting only as the explicit multilateral-offsetting "
    "intervention; every draw and every net movement is a ledger entry.",
    "Queue order: priority, then due hour, then ID. A payment that cannot settle does not block the ones behind it.",
    "Cash received during an hour can fund further payments in the same hour (within-step liquidity recycling).",
    "Each payment uses one unit of hourly capacity at every service on its route; capacity is shared by all users.",
    "A service runs at the lowest capacity of its own state and its required dependencies; an 'any of' group uses its best member.",
    "External parties sit outside the accounting boundary and are never cash-constrained, but their payments still need a working route.",
    "No behavioural responses (cancellations, penalties, renegotiation) beyond the explicit interventions; the only "
    "behavioural rule is the optional payroll-protection reserve, which holds routine payments back.",
    "Overdue value is not economic loss, and a temporary cash shortfall is not insolvency.",
]

METRIC_KIND = {key: kind for key, _, kind in HEADLINE_METRICS}
METRIC_LABEL = {key: label for key, label, _ in HEADLINE_METRICS}
METRIC_LABEL.update({
    "delayed_count": "Delayed obligations (late or unsettled)",
    "delayed_value": "Delayed value (late or unsettled)",
    "overdue_obligation_hours": "Overdue obligation-hours",
    "urgent_overdue_value_hours": "Overdue value × hours of priority-1 payments",
})
METRIC_KIND.update({"delayed_count": "count", "delayed_value": "money", "overdue_obligation_hours": "count",
                    "urgent_overdue_value_hours": "money_hours"})


# --------------------------------------------------------------- cached compute

def _interventions(text: str) -> list:
    return INTERVENTIONS.validate_python(json.loads(text))


@st.cache_resource(max_entries=48, show_spinner=False)
def cached_compare(stress_json: str, interventions_json: str, pairs: bool):
    stressed = Scenario.model_validate_json(stress_json)
    ivs = _interventions(interventions_json)
    liquidity = {"liquidity_buffer", "credit_line"}  # two liquidity instruments together say little
    packages = [(a, b) for i, a in enumerate(ivs) for b in ivs[i + 1:]
                if {a.type, b.type} != liquidity] if pairs else []
    return compare(stressed, ivs, packages=packages)


@st.cache_resource(max_entries=24, show_spinner="Computing the exact liquidity backstop…")
def cached_backstop(stress_json: str):
    return liquidity_backstop(Scenario.model_validate_json(stress_json))


def build_plans(specs: dict, horizon: int, exclude: tuple[str, ...] = ()) -> dict:
    """Intervention 'plans' that follow a moving disruption (see cyberliquidity.analysis)."""
    plans = {}
    if "backup_route" in specs and "backup_route" not in exclude:
        b = specs["backup_route"]
        plans["backup_route"] = lambda s, d, b=b: make_backup(b["primary"], b["backup"],
                                                             min(d.start_hour + b["delay"], horizon - 1), id="backup")
    if "liquidity_buffer" in specs and "liquidity_buffer" not in exclude:
        b = specs["liquidity_buffer"]
        plans["liquidity_buffer"] = lambda s, d, b=b: make_buffer(b["participants"], b["amount"], b["funding_hour"], id="buffer")
    if "credit_line" in specs and "credit_line" not in exclude:
        c = specs["credit_line"]
        plans["credit_line"] = lambda s, d, c=c: make_credit_line(c["participants"], c["limit"], c["from"], id="credit_line")
    if "faster_recovery" in specs and "faster_recovery" not in exclude:
        f = specs["faster_recovery"]
        plans["faster_recovery"] = lambda s, d, f=f: make_faster_recovery(d.id, f["reduction"], id="faster_recovery")
    if "offsetting" in specs and "offsetting" not in exclude:
        o = specs["offsetting"]
        plans["offsetting"] = lambda s, d, o=o: make_offsetting(o["from"], id="offsetting")
    if "priority_reserve" in specs and "priority_reserve" not in exclude:
        r = specs["priority_reserve"]
        plans["priority_reserve"] = lambda s, d, r=r: make_reserve(r["participants"], r["priority"], r["lookahead"],
                                                                   r["from"], id="reserve")
    return plans


@st.cache_data(max_entries=24, show_spinner="Re-running every intervention under changed assumptions…")
def cached_robustness(base_json: str, disruptions_json: str, specs_json: str, metric: str) -> dict:
    base = Scenario.model_validate_json(base_json)
    ds = [Disruption.model_validate(x) for x in json.loads(disruptions_json)]
    plans = build_plans(json.loads(specs_json), base.horizon_hours)
    return robustness(base, ds, plans, metric=metric, labels={k: INTERVENTION_LABELS[k] for k in plans})


@st.cache_data(max_entries=24, show_spinner="Running every service through the same outage…")
def lab_scan(base_json: str, start: int, duration: int, severity: int, recovery: int, delay: int, specs_json: str,
             metric: str) -> list[dict]:
    base = Scenario.model_validate_json(base_json)
    plans = build_plans(json.loads(specs_json), base.horizon_hours, exclude=("backup_route",))
    return criticality_scan(base, start, duration, severity, recovery, plans=plans, backup_delay_hours=delay, metric=metric)


@st.cache_data(max_entries=24, show_spinner="Running the outage at every tested duration…")
def lab_profile(base_json: str, service: str, start: int, durations: tuple[int, ...], severity: int, recovery: int,
                specs_json: str, outcome: str, extra_json: str) -> dict:
    base = Scenario.model_validate_json(base_json)
    return duration_profile(base, service, start, durations, severity, recovery,
                            plans=build_plans(json.loads(specs_json), base.horizon_hours), outcome=outcome,
                            extra_disruptions=[Disruption.model_validate(x) for x in json.loads(extra_json)])


@st.cache_data(max_entries=24, show_spinner="Moving the outage across the calendar…")
def lab_starts(base_json: str, service: str, duration: int, starts: tuple[int, ...], severity: int, recovery: int,
               specs_json: str) -> list[dict]:
    base = Scenario.model_validate_json(base_json)
    return start_profile(base, service, duration, starts, severity, recovery,
                         plans=build_plans(json.loads(specs_json), base.horizon_hours))


@st.cache_data(max_entries=12, show_spinner="Computing the start × duration grid…")
def lab_surface(base_json: str, service: str, starts: tuple[int, ...], durations: tuple[int, ...], severity: int) -> dict:
    return impact_surface(Scenario.model_validate_json(base_json), service, starts, durations, severity)


# ----------------------------------------------------------------- scenario

@st.cache_data(show_spinner=False)
def scenario_titles(folder: str) -> dict[str, str]:
    """Display names of the built-in scenarios (each file's "name"), keyed by file name."""
    titles = {}
    for path in sorted(Path(folder).glob("*.json")):
        try:
            titles[path.name] = str(json.loads(path.read_text(encoding="utf-8")).get("name") or path.name)
        except (OSError, ValueError, AttributeError):
            titles[path.name] = path.name
    return titles


with st.sidebar:
    st.subheader("1 · Scenario", anchor=False)
    titles = scenario_titles(str(SCENARIO_DIR))
    files = list(titles)
    options = files + [UPLOAD]
    choice = st.selectbox("Scenario", options, index=options.index("default_72h.json") if "default_72h.json" in files else 0,
                          format_func=lambda f: titles.get(f, f), label_visibility="collapsed",
                          help="The built-in scenarios are fictional and synthetic. You can also upload your own file.")
    if choice == UPLOAD:
        upload = st.file_uploader("Scenario file (.json, up to 5 MB)", type=["json"],
                                  help="The format is described in docs/scenario.schema.json. Use fictional data only.")
        if upload is None:
            st.info("Upload a scenario file to continue. It is checked before anything is simulated.",
                    icon=":material/upload_file:")
            st.stop()
        scenario_data: bytes | str = upload.getvalue()
        source = "The uploaded file"
    else:
        scenario_data = (SCENARIO_DIR / choice).read_bytes()
        source = "This scenario file"
    edit_key = f"edited:{choice}"
    edited_now = edit_key in st.session_state
    if edited_now:
        scenario_data = st.session_state[edit_key]
        source = "The edited scenario"
        st.info("Showing your **edited** version of this scenario (not saved to disk).", icon=":material/edit_note:")
        if st.button("Discard edits", key="discard_edits", icon=":material/undo:"):
            del st.session_state[edit_key]
            st.rerun()

try:  # size, encoding, structure and complexity checks (cyberliquidity.safety)
    scenario = parse_scenario(scenario_data, APP_LIMITS, source=source)
except ScenarioInputError as problem:
    st.error(f"**{problem.title}.** Nothing was simulated.", icon=":material/block:")
    if problem.problems:
        st.markdown("\n".join(f"- {plain(line)}" for line in problem.problems))
    st.caption("Fix the file and upload it again, or pick a built-in scenario in the sidebar.")
    st.stop()

cur = scenario.currency
names = scenario.names()
H = scenario.horizon_hours
sid = scenario.id
services = scenario.services_by_id()
op_model = OperationalModel(scenario)
service_ids = [s.id for s in scenario.services]
route_ids = [r.id for r in scenario.routes]
participant_ids = [p.id for p in scenario.participants]


def money(minor: int | None) -> str:
    return "–" if minor is None else cur.format(minor)


def fmt(value, kind: str) -> str:
    if value is None:
        return "–"
    if kind == "money":
        return cur.format(value)
    if kind == "money_hours":
        return f"{cur.format(value)}·h"
    if kind == "hours":
        return f"{value:.2f}" if isinstance(value, float) else str(value)
    if isinstance(value, int):
        return f"{value:,}"
    return str(value)


def compact(minor: int, signed: bool = False, unit: bool = True) -> str:
    """Short money for stat tiles: three significant digits (229k, 1.85M). Exact values are in the tables."""
    value = cur.to_major(minor)
    size = abs(value)
    for scale, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "k")):
        if size >= scale:
            scaled = value / scale
            digits = 0 if abs(scaled) >= 100 else 1 if abs(scaled) >= 10 else 2
            text = f"{scaled:.{digits}f}{suffix}"
            break
    else:
        text = f"{value:,.0f}" if float(value).is_integer() else f"{value:,.2f}"
    return f"{'+' if signed and value > 0 else ''}{text}" + (f" {cur.code}" if unit else "")


def metric(where, label: str, value, **kwargs) -> None:
    """A metric tile with a border (consistent look across tabs)."""
    kwargs.setdefault("border", True)
    where.metric(label, value, **kwargs)


def service_label(x: str) -> str:
    return f"{names[x]} ({services[x].kind.value.replace('_', ' ')})"


def lab_value(key: str, default):
    """Widget kwargs that set a default only on first render (avoids Session State conflicts)."""
    return {} if key in st.session_state else {"value": default}


def first(key: str, **defaults):
    """Like lab_value, for any default keyword (index=..., value=...)."""
    return {} if key in st.session_state else defaults


# --------------------------------------------------------------- disruption

d0 = scenario.disruptions[0] if scenario.disruptions else None
default_service = d0.service if d0 else next((s.id for s in scenario.services if s.kind.value == "payment_provider"), service_ids[0])

with st.sidebar:
    st.subheader("2 · Disruption (run B)", anchor=False,
                 help="What goes wrong. Run A is the same scenario with nothing going wrong.")
    service = st.selectbox("Service that fails", service_ids, index=service_ids.index(default_service),
                           format_func=service_label, key=f"{sid}:service")
    start = st.slider("Outage start (hour)", 0, H - 1, d0.start_hour if d0 else min(8, H - 1), key=f"{sid}:start")
    duration = st.slider("Outage duration (hours)", 1, H, d0.duration_hours if d0 and d0.duration_hours else min(12, H), key=f"{sid}:duration")
    severity = st.slider("Capacity lost (%)", 10, 100, d0.severity_pct if d0 else 100, step=5,
                         key=f"{sid}:severity", help="100 % means completely unavailable. Lower values leave a slower service with less shared capacity.")
    with st.expander("Recovery ramp"):
        recovery_hours = st.slider("Degraded hours after the outage", 0, 24, d0.recovery_hours if d0 else 0, key=f"{sid}:rech")
        recovery_pct = st.slider("Capacity during the ramp (%)", 10, 90, d0.recovery_capacity_pct if d0 else 50, step=5, key=f"{sid}:recp")
    extra_disruptions = []
    with st.expander("More disruptions at once"):
        n_extra = int(st.number_input("Additional disruptions at the same time", 0, 2, 0, key=f"{sid}:n_extra",
                                      help="For example a second cloud region or a provider failing during the first outage."))
        for i in range(n_extra):
            others = [x for x in service_ids if x != service]
            st.markdown(f"**D{i + 2}**")
            svc_i = st.selectbox("Service", others, index=min(i, len(others) - 1), format_func=service_label, key=f"{sid}:x{i}:svc")
            start_i = st.slider("Start (hour)", 0, H - 1, start, key=f"{sid}:x{i}:start")
            dur_i = st.slider("Duration (h)", 1, H, duration, key=f"{sid}:x{i}:dur")
            sev_i = st.slider("Capacity lost (%)", 10, 100, 100, step=5, key=f"{sid}:x{i}:sev")
            extra_disruptions.append(make_disruption(svc_i, start_i, dur_i, sev_i, id=f"D{i + 2}",
                                                     description="Concurrent disruption configured in the app"))

disruption_id = d0.id if d0 else "D1"
disruption = make_disruption(service, start, duration, severity, recovery_hours, recovery_pct, id=disruption_id,
                             description="Configured in the app")
all_disruptions = (disruption, *extra_disruptions)
stress_base = replace(scenario, disruptions=all_disruptions, interventions=())
stress_json = scenario_json(stress_base)

# ------------------------------------------------------------- interventions

candidates = {}
for iv in scenario.candidate_interventions:
    candidates.setdefault(iv.type, iv)
suggestion = suggest_backup(stress_base, disruption)
down_everywhere = set()
for d in all_disruptions:
    down_everywhere.update(op_model.failure_footprint(d.service, d.severity_pct)["unavailable_services"])

with st.sidebar:
    st.subheader("3 · Response (run C)", anchor=False,
                 help="What could be done about it. Each response is added to run B on its own.")
    chosen_kind = st.radio("Response shown as run C", KINDS, format_func=INTERVENTION_LABELS.get, key=f"{sid}:kind")
    st.caption("All six are compared on **Interventions & evidence**, each against the same stressed run B.")

    with st.expander("Backup payment provider", expanded=chosen_kind == "backup_route"):
        cand = candidates.get("backup_route")
        default_primary = list(suggestion.primary_routes) or (list(cand.primary_routes) if cand else route_ids[:1])
        primary = st.multiselect("Primary routes allowed to reroute", route_ids, default=default_primary,
                                 format_func=names.get, key=f"{sid}:{service}:{severity}:primary")
        backup_options = [r for r in route_ids if r not in primary] or route_ids
        default_backup = suggestion.backup_route if suggestion.backup_route in backup_options else backup_options[0]
        backup_route = st.selectbox("Backup route", backup_options, index=backup_options.index(default_backup),
                                    format_func=names.get, key=f"{sid}:{service}:{'|'.join(primary)}:backup")
        default_delay = max(0, cand.active_from_hour - d0.start_hour) if cand and d0 else 3
        activation_delay = st.slider("Activation delay after outage start (h)", 0, 24, min(default_delay, 24), key=f"{sid}:delay")
        active_from = min(start + activation_delay, H - 1)
        st.caption(f"Backup usable from hour {active_from}. It keeps its own dependencies and shared capacity.")
        common = sorted({x for p in primary for x in op_model.common_mode_failures(p, backup_route)})
        backup_spof = op_model.single_points_of_failure(backup_route)
        if any(svc in backup_spof for svc in down_everywhere):
            st.warning("Not independent of this disruption: the backup route depends on a service that this "
                       "disruption takes down. Expect little or no benefit.")
        elif common:
            st.caption("Shared single points of failure with the primary route(s): "
                       + ", ".join(names[x] for x in common) + ". Not affected by this disruption.")

    buffer_cand = candidates.get("liquidity_buffer")
    default_parts = [a.participant for a in buffer_cand.allocations] if buffer_cand else participant_ids
    default_amount = cur.to_major(buffer_cand.allocations[0].amount) if buffer_cand else cur.to_major(
        max(o.amount for o in scenario.obligations) if scenario.obligations else 0)
    step = 1.0 if cur.minor_units == 0 else 1000.0

    with st.expander("Liquidity buffer (pre-funded)", expanded=chosen_kind == "liquidity_buffer"):
        buffer_parts = st.multiselect("Participants funded", participant_ids, default=default_parts,
                                      format_func=names.get, key=f"{sid}:buffer_parts")
        buffer_amount = st.number_input(f"Amount per participant ({cur.code})", min_value=0.0, value=float(default_amount),
                                        step=step, key=f"{sid}:buffer_amount")
        funding_hour = st.slider("Funding hour", 0, H - 1, buffer_cand.funding_hour if buffer_cand else 0, key=f"{sid}:funding_hour")
        st.caption("Paid in full at the funding hour; recorded as explicit intervention funding from outside the boundary.")

    with st.expander("Committed credit line (on demand)", expanded=chosen_kind == "credit_line"):
        credit_cand = candidates.get("credit_line")
        credit_parts = st.multiselect("Participants with a facility", participant_ids,
                                      default=[f.participant for f in credit_cand.facilities] if credit_cand else default_parts,
                                      format_func=names.get, key=f"{sid}:credit_parts")
        credit_limit = st.number_input(f"Limit per participant ({cur.code})", min_value=0.0,
                                       value=float(cur.to_major(credit_cand.facilities[0].limit)) if credit_cand else float(default_amount),
                                       step=step, key=f"{sid}:credit_limit")
        credit_from = st.slider("Available from hour", 0, H - 1, credit_cand.available_from_hour if credit_cand else 0,
                                key=f"{sid}:credit_from")
        st.caption("Drawn only when a payment would otherwise fail for lack of cash; end-of-hour cash repays it. In this "
                   "model it settles exactly like a pre-funded buffer of the same size from the same hour, but only "
                   "the liquidity actually needed is drawn.")

    with st.expander("Faster service recovery", expanded=chosen_kind == "faster_recovery"):
        cand = candidates.get("faster_recovery")
        if duration > 1:
            default_cut = min(cand.duration_reduction_hours if cand else max(1, duration // 2), duration)
            reduction = st.slider("Outage shortened by (h)", 1, duration, default_cut, key=f"{sid}:reduction")
        else:
            reduction = 1
            st.caption("Outage shortened by 1 h (the whole outage).")
        st.caption(f"{disruption_id} lasts {duration - reduction} h instead of {duration} h; nothing else changes."
                   + (" Concurrent disruptions keep their duration." if extra_disruptions else ""))

    with st.expander("Multilateral offsetting (gridlock resolution)", expanded=chosen_kind == "offsetting"):
        offset_cand = candidates.get("offsetting")
        offset_from = st.slider("Active from hour", 0, H - 1, offset_cand.active_from_hour if offset_cand else 0,
                                key=f"{sid}:offset_from")
        st.caption("Every hour the payment system looks for queued payments that can settle together because each "
                   "participant's net outflow over the set is covered. Cash moves net through a clearing account. It "
                   "adds no money and cannot use a route that is down: it only helps where payments wait for each other.")

    with st.expander("Payroll-protection reserve (treasury rule)", expanded=chosen_kind == "priority_reserve"):
        reserve_cand = candidates.get("priority_reserve")
        reserve_default = (list(reserve_cand.participants) if reserve_cand and reserve_cand.participants is not None
                           else participant_ids)
        reserve_parts = st.multiselect("Participants applying the rule", participant_ids, default=reserve_default,
                                       format_func=names.get, key=f"{sid}:reserve_parts")
        reserve_priority = st.slider("Protect payments up to priority", 1, 3,
                                     reserve_cand.protected_priority if reserve_cand else 1, key=f"{sid}:reserve_priority",
                                     help="1 = only the most urgent payments (in the built-in scenarios: payroll, tax, "
                                          "loan repayments).")
        reserve_lookahead = st.slider("Keep cash for protected payments due within (h)", 1, 48,
                                      min(reserve_cand.lookahead_hours if reserve_cand else 24, 48),
                                      key=f"{sid}:reserve_lookahead")
        reserve_from = st.slider("Rule applies from hour", 0, H - 1, reserve_cand.active_from_hour if reserve_cand else 0,
                                 key=f"{sid}:reserve_from")
        st.caption("A routine payment is released only if the cash left afterwards (plus any undrawn credit) still covers "
                   "the participant's protected payments due soon. It adds no money; it decides which payments wait, "
                   "and the payments it holds back are other participants' incoming money.")

    st.subheader("4 · Ranking", anchor=False)
    pairs = st.toggle("Also run every pair of interventions", value=True, key="pairs",
                      help="Shows whether two interventions complement each other or overlap in this run.")
    rank_metric = st.selectbox("Ranking metric (lower = less disruption)", list(LOWER_IS_BETTER),
                               format_func=lambda m: METRIC_LABEL.get(m, m), key="rank_metric")

interventions = {}
if primary:
    interventions["backup_route"] = make_backup(primary, backup_route, active_from, id="backup")
if buffer_parts and cur.to_minor(buffer_amount) > 0:
    interventions["liquidity_buffer"] = make_buffer(buffer_parts, cur.to_minor(buffer_amount), funding_hour, id="buffer")
if credit_parts and cur.to_minor(credit_limit) > 0:
    interventions["credit_line"] = make_credit_line(credit_parts, cur.to_minor(credit_limit), credit_from, id="credit_line")
interventions["faster_recovery"] = make_faster_recovery(disruption.id, reduction, id="faster_recovery")
interventions["offsetting"] = make_offsetting(offset_from, id="offsetting")
if reserve_parts:
    covered = None if set(reserve_parts) == set(participant_ids) else sorted(reserve_parts)
    interventions["priority_reserve"] = make_reserve(covered, reserve_priority, reserve_lookahead, reserve_from, id="reserve")
interventions_json = json.dumps([iv.model_dump(mode="json") for iv in interventions.values()], sort_keys=True)

# The same interventions as "plans" that follow a moved outage (Stress lab and robustness check).
plan_specs: dict = {"faster_recovery": {"reduction": reduction}, "offsetting": {"from": offset_from}}
if primary:
    plan_specs["backup_route"] = {"primary": primary, "backup": backup_route, "delay": activation_delay}
if "liquidity_buffer" in interventions:
    plan_specs["liquidity_buffer"] = {"participants": buffer_parts, "amount": cur.to_minor(buffer_amount), "funding_hour": funding_hour}
if "credit_line" in interventions:
    plan_specs["credit_line"] = {"participants": credit_parts, "limit": cur.to_minor(credit_limit), "from": credit_from}
if "priority_reserve" in interventions:
    plan_specs["priority_reserve"] = {"participants": interventions["priority_reserve"].participants,
                                      "priority": reserve_priority, "lookahead": reserve_lookahead, "from": reserve_from}
specs_json = json.dumps(plan_specs, sort_keys=True)
base_json = scenario_json(replace(scenario, disruptions=(), interventions=()))
disruptions_json = json.dumps([d.model_dump(mode="json") for d in all_disruptions], sort_keys=True)

comparison = cached_compare(stress_json, interventions_json, pairs)
runs: dict[str, RunView] = {
    "A": RunView("A", "A · Baseline", "baseline", comparison.runs["A"]),
    "B": RunView("B", "B · Stress", "stress", comparison.runs["B"]),
}
for kind, iv in interventions.items():
    key = run_key(iv)
    runs[key] = RunView(key, f"C · {INTERVENTION_LABELS[kind]}", kind, comparison.runs[key])
for key in comparison.package_keys():
    runs[key] = RunView(key, comparison.labels[key], "combination", comparison.runs[key])
summaries = comparison.summaries
c_key = run_key(interventions[chosen_kind]) if chosen_kind in interventions else None
abc = [runs["A"], runs["B"]] + ([runs[c_key]] if c_key else [])
intervention_keys = [run_key(iv) for iv in interventions.values()]
_cascades: dict[str, object] = {}


def cascade_for(key: str):
    if key not in _cascades:
        _cascades[key] = analyse_cascade(runs[key].result)
    return _cascades[key]


backstop = cached_backstop(stress_json)
robust = cached_robustness(base_json, disruptions_json, specs_json, rank_metric)
brief = briefing(comparison, rank_metric, cascade=cascade_for("B"), backstop=backstop, robust=robust)

state_word = "unavailable" if severity == 100 else f"degraded to {100 - severity}% capacity"
footprint = op_model.failure_footprint(service, severity)
disruption_text = (f"**B:** {names[service]} {state_word} from hour {start} for {duration} h"
                   + (f", then {recovery_hours} h at {recovery_pct}%" if recovery_hours else "") + ". "
                   + "Directly affected: "
                   + (", ".join(names[x] for x in footprint["unavailable_services"] + footprint["degraded_services"]) or "nothing else")
                   + "; routes: " + (", ".join(names[x] for x in footprint["unavailable_routes"] + footprint["degraded_routes"]) or "none") + "."
                   + ("".join(f" **Concurrently:** {disruption_phrase(d, names)}." for d in extra_disruptions)))


def describe_intervention(kind: str) -> str:
    iv = interventions.get(kind)
    if iv is None:
        return f"**C:** {INTERVENTION_LABELS[kind]} is not configured (empty selection)."
    if kind == "backup_route":
        return (f"**C:** obligations on {', '.join(names[r] for r in iv.primary_routes)} may reroute via "
                f"{names[iv.backup_route]} from hour {iv.active_from_hour} ({activation_delay} h after the outage starts).")
    if kind == "liquidity_buffer":
        return (f"**C:** {money(iv.allocations[0].amount)} each to {', '.join(names[a.participant] for a in iv.allocations)} "
                f"in hour {iv.funding_hour} (total {money(iv.total)}, explicitly recorded).")
    if kind == "credit_line":
        return (f"**C:** committed credit lines of {money(iv.facilities[0].limit)} each for "
                f"{', '.join(names[f.participant] for f in iv.facilities)} from hour {iv.available_from_hour}, drawn on demand.")
    if kind == "offsetting":
        return f"**C:** the payment system offsets queued payments every hour from hour {iv.active_from_hour}."
    if kind == "priority_reserve":
        who = "every participant" if iv.participants is None else ", ".join(names[p] for p in iv.participants)
        return (f"**C:** {who} holds back routine payments to keep cash for priority ≤ {iv.protected_priority} payments due "
                f"within {iv.lookahead_hours} h (from hour {iv.active_from_hour}).")
    return f"**C:** the outage is {iv.duration_reduction_hours} h shorter ({duration - iv.duration_reduction_hours} h instead of {duration} h)."


def render_export() -> None:
    labels = {k: rv.label for k, rv in runs.items()}
    parameters = {
        "disruptions": [d.model_dump(mode="json") for d in all_disruptions],
        "interventions": {k: iv.model_dump(mode="json") for k, iv in interventions.items()},
        "run_c": chosen_kind,
        "pairs": pairs,
    }
    configuration = [disruption_text, *(describe_intervention(k) for k in KINDS),
                     f"Settings: queue order `{scenario.settings.ordering}`, liquidity recycling "
                     f"`{scenario.settings.liquidity_recycling}`." + (" Every pair of interventions was also run." if pairs else "")]
    st.download_button(
        "Download briefing report (.html)", icon=":material/description:", on_click="ignore",
        data=lambda: build_report(comparison, runs, [rv.key for rv in abc], brief, cascade_for("B"), backstop,
                                  configuration, KEY_ASSUMPTIONS, rank_metric, robust=robust,
                                  robust_labels={k: INTERVENTION_LABELS[k] for k in robust["plans"]}),
        file_name=f"cyberliquidity_{sid}_briefing.html", mime="text/html",
        help="One self-contained file to share: the answer, charts, tables, assumptions and reproduction digests. Works offline.",
    )
    st.download_button(
        "Download all runs (.zip)", data=lambda: bundle_zip(comparison.runs, labels, extra_manifest={"app_parameters": parameters}),
        file_name=f"cyberliquidity_{sid}.zip", mime="application/zip", on_click="ignore", icon=":material/download:",
        help="Exact scenario JSON for every run, full event ledger, cash ledger, outcomes and time series. "
             "Re-run any folder with `cyberliquidity run <folder>/scenario.json` and compare the digest.",
    )
    st.download_button("Download stressed scenario (B) JSON", data=scenario_json(comparison.runs["B"].scenario),
                       file_name=f"{sid}_stress.json", mime="application/json", on_click="ignore", icon=":material/data_object:",
                       help="The exact scenario simulated as run B. Run it again with `cyberliquidity run <file>`.")

# --------------------------------------------------------------------- page

title_col, action_col = st.columns([0.72, 0.28], vertical_alignment="center")
with title_col:
    st.header(plain(scenario.name), anchor=False)
with action_col, st.container(horizontal=True, horizontal_alignment="right"):
    with st.popover("Guide", icon=":material/help:", help="How to use this app, and what the terms mean"):
        st.markdown("##### How to use this app")
        render_quick_start(columns=False)
        with st.expander("Glossary of terms", icon=":material/menu_book:"):
            render_glossary()
    with st.popover("Export", icon=":material/download:", type=MAIN_BUTTON,
                    help="Download a shareable report, every run's raw data, or the stressed scenario"):
        render_export()
badges = [":orange-badge[:material/science: Synthetic data]", f":gray-badge[{H}-hour window]", f":gray-badge[{cur.code}]"]
if edited_now:
    badges.append(":violet-badge[:material/edit_note: Edited in this session]")
st.markdown(" ".join(badges))
if scenario.description:
    st.caption(plain(scenario.description))
st.info(f"{DISCLAIMER} All organisations and data are fictional.", icon=":material/info:")

TAB_NAMES = ["Overview", "Contagion", "Network", "Timeline", "Interventions & evidence", "Stress lab",
             "Scenario editor", "Model & assumptions"]
(tab_overview, tab_contagion, tab_network, tab_timeline, tab_evidence, tab_lab, tab_editor,
 tab_model) = st.tabs(TAB_NAMES, key="tab", bind="query-params")

# ------------------------------------------------------------------ overview

with tab_overview, guarded("The Overview tab"):
    with st.expander("New here? How to use this app in four steps", icon=":material/school:"):
        render_quick_start()
    with st.container(border=True):
        st.markdown("#### The answer")
        for paragraph in brief:
            st.markdown(paragraph)
    s = scenario
    facts = st.columns(5)
    metric(facts[0], "Participants", len(s.participants), help="Funded businesses inside the accounting boundary.")
    metric(facts[1], "Services / routes", f"{len(s.services)} / {len(s.routes)}")
    metric(facts[2], "Obligations", len(s.obligations))
    metric(facts[3], f"Scheduled value ({cur.code})", compact(sum(o.amount for o in s.obligations), unit=False),
                    help=money(sum(o.amount for o in s.obligations)))
    metric(facts[4], "Horizon", f"{H} h")
    st.markdown(disruption_text)
    st.markdown(describe_intervention(chosen_kind))

    A, B = summaries["A"], summaries["B"]
    C = summaries[c_key] if c_key else None

    def tiles(current: dict, reference: dict, tag: str) -> None:
        cols = st.columns(4)
        diff = {k: current[k] - reference[k] for k in ("delayed_value", "peak_overdue_value", "overdue_value_hours")}
        metric(cols[0], f"On time ({tag})", f"{current['on_time_count']} / {current['obligations_count']}",
                       delta=current["on_time_count"] - reference["on_time_count"],
                       help="Settled by the end of the due hour.")
        metric(cols[1], f"Delayed value ({tag})", compact(current["delayed_value"]),
                       delta=compact(diff["delayed_value"], True), delta_color="inverse",
                       help="Value of obligations settled late or still unsettled at the horizon. Not a loss estimate.")
        metric(cols[2], f"Peak overdue value ({tag})", compact(current["peak_overdue_value"]),
                       delta=compact(diff["peak_overdue_value"], True), delta_color="inverse",
                       help="Largest value of overdue obligations at the end of any hour. Not the liquidity required.")
        metric(cols[3], f"Overdue value × hours ({tag})", compact(current["overdue_value_hours"]) + "·h",
                       delta=compact(diff["overdue_value_hours"], True) + "·h", delta_color="inverse",
                       help="Sum over hours of the overdue value: a disruption-intensity measure, not a loss.")

    st.caption("Stress run B, with change against baseline A")
    tiles(B, A, "B")
    if C is not None:
        st.caption(f"{runs[c_key].label}, with change against stress run B")
        tiles(C, B, "C")

    left, right = st.columns(2)
    with left:
        st.markdown("**Overdue obligations over time**")
        show_chart(charts.overdue_chart(abc, pal, cur, "value"), "ov_overdue")
    with right:
        st.markdown("**Settlement delay of completed payments**")
        include_zero = st.toggle("Include on-time payments (delay 0)", value=False, key="ov_zero")
        show_chart(charts.delay_histogram(abc, pal, include_zero), "ov_delay")
        st.caption("Unsettled obligations have no completed delay; they are counted separately below.")

    st.markdown("**What held payments up** (overdue value × hours by blocking reason)")
    show_chart(charts.mechanism_bars([rv for rv in abc if rv.key != "A"], summaries, pal, cur), "ov_mech")
    shares = mechanism_shares(B)
    if B["overdue_value_hours"]:
        st.caption("Run B: " + "; ".join(f"{CATEGORY_LABELS[c].split(' (')[0]} {shares[c]:.0%}" for c in CATEGORIES if shares[c]))

    st.markdown("**Baseline vs stress vs intervention**")
    comparison_keys = [rv.key for rv in abc]
    table = pd.DataFrame([{"Metric": label, **{runs[k].label: fmt(summaries[k].get(key), kind) for k in comparison_keys}}
                          for key, label, kind in HEADLINE_METRICS])
    st.dataframe(table, hide_index=True)

    with st.expander("Key assumptions (full model note on the last tab)"):
        st.markdown("\n".join(f"- {a}" for a in KEY_ASSUMPTIONS))

# ----------------------------------------------------------------- contagion

with tab_contagion, guarded("The Contagion tab"):
    st.markdown("#### How far does the disruption travel?")
    st.caption("Step 0 = payments held up directly by the outage. Step k = payments whose payer had a working route but "
               "was short of cash while waiting for delayed incoming payments, k such links away from a direct delay. "
               "Links are recorded blocking relationships, not proofs that one delay alone caused another.")
    cg_options = [k for k in runs if k != "A"]
    cg_key = st.selectbox("Run", cg_options, format_func=lambda k: runs[k].label, key="cg_run")
    cg_result = runs[cg_key].result
    cg = cascade_for(cg_key)
    cs = cg.summary()
    if not cg.nodes:
        st.success("No obligation is delayed in this run, so nothing propagates.")
    else:
        short = cg.cash_short()
        indirect = cg.indirectly_affected()
        cols = st.columns(4)
        metric(cols[0], "Held up directly", cs["direct_count"], help=f"{money(cs['direct_value'])}: the route needed a disrupted service.")
        metric(cols[1], "Held up by contagion", cs["propagated_count"],
                       help=f"{money(cs['propagated_value'])}: route worked, payer short of cash while waiting for delayed inflows.")
        metric(cols[2], "Contagion depth", f"{cs['max_generation'] or 0} step{'s' if (cs['max_generation'] or 0) != 1 else ''}",
                       help="Longest recorded chain from a direct delay to a delayed payment.")
        metric(cols[3], "Short of cash", len(short), help=f"{len(indirect)} of them never lost a payment route of their own.")
        if indirect:
            st.markdown("**Reached only through delayed incoming money** (their own routes never failed): "
                        + ", ".join(names[p] for p in sorted(indirect, key=lambda p: (cg.participants[p].first_cash_short_hour, p))) + ".")
        st.markdown("**Contagion map**")
        st.graphviz_chart(network.cascade_dot(cg, cg_result, pal))
        st.caption("Top to bottom: the disruption (box), payers whose payments it blocked directly (dashed arrows), and "
                   "every delayed payment from payer to recipient, labelled value · count (arrow width ∝ value; hover an "
                   "arrow for details). Violet arrows were held up by the outage; green arrows by the payer's cash "
                   f"shortfall. Amber outlines mark participants that ran short of cash. {STATUS_ICON['degraded']} short of "
                   f"cash · {STATUS_ICON['unavailable']} route-blocked.")
        left, right = st.columns(2)
        with left:
            st.markdown("**Delayed value by step**")
            show_chart(charts.generation_bars(cg, pal, cur), "cg_generations")
        with right:
            st.markdown("**Steps in this run**")
            st.dataframe(pd.DataFrame([{
                "Step": "unlinked" if g["generation"] is None else str(g["generation"]),
                "Kind": KIND_LABELS[g["kind"]].split(":")[0], "Payments": g["count"], "Value": money(g["value"]),
                "Value × hours": fmt(g["value_hours"], "money_hours"),
                "Payers": ", ".join(names.get(p, p) for p in g["payers"]),
            } for g in cg.by_generation]), hide_index=True)
        st.markdown("**Participants**")
        st.dataframe(pd.DataFrame([{
            "Participant": names[x.participant],
            "Short of cash": f"{STATUS_ICON['degraded']} from hour {x.first_cash_short_hour}" if x.cash_short else "–",
            "Hours short": x.cash_short_hours,
            "Largest single shortfall": money(x.largest_shortfall),
            "Own route ever blocked": "yes" if x.route_blocked else "no",
            "Reached at step": "–" if x.generation is None else str(x.generation),
            "Delayed out": f"{x.delayed_out_count} · {money(x.delayed_out_value)}",
            "Delayed in": f"{x.delayed_in_count} · {money(x.delayed_in_value)}",
            "Lowest balance": money(x.min_balance),
        } for x in sorted(cg.participants.values(), key=lambda x: (not x.cash_short, x.first_cash_short_hour or 0,
                                                                   -x.delayed_out_value, x.participant))]), hide_index=True)
        with st.expander("Every delayed obligation with its step and what it waited for"):
            st.dataframe(pd.DataFrame([{
                "Obligation": n.obligation_id, "Purpose": n.purpose,
                "Payer → recipient": f"{names.get(n.payer, n.payer)} → {names.get(n.recipient, n.recipient)}",
                "Amount": money(n.amount), "Overdue h": n.overdue_hours, "Kind": n.kind,
                "Step": "–" if n.generation is None else str(n.generation),
                "Waiting for (overdue inflows)": ", ".join(n.parents),
                "Blocked by": ", ".join(names.get(x, x) for x in n.root_services),
            } for n in sorted(cg.nodes.values(), key=lambda n: (n.generation is None, n.generation or 0, -n.value_hours))]),
                hide_index=True)

# ------------------------------------------------------------------- network

with tab_network, guarded("The Network tab"):
    controls = st.columns([2, 1, 2])
    layer = controls[0].radio("Layer", ["Operational dependencies", "Financial obligations", "Payment routing (coupling)"],
                              horizontal=True, key="net_layer")
    net_run = controls[1].selectbox("Run", [k for k in runs if k != "A"] + ["A"], format_func=lambda k: runs[k].label, key="net_run")
    default_hour = summaries["B"]["peak_overdue_hour"] if summaries["B"]["peak_overdue_hour"] is not None else start
    hour = controls[2].slider("Hour", 0, H - 1, int(default_hour), key=f"{sid}:net_hour")
    result = runs[net_run].result
    status = result.service_status[hour]
    used = result.hours[hour].service_used

    if layer == "Operational dependencies":
        disrupted = {d.service for d in result.effective_disruptions if d.phase_at(hour) is not None}
        st.graphviz_chart(network.operational_dot(result.scenario, status, used, disrupted, pal))
        st.caption("Arrow A → B: A depends on B, so a failure of B propagates to A. Solid = required; dashed = one of "
                   "several alternatives (failover). Arrow-shaped nodes are payment routes; boxes are services. "
                   f"{STATUS_ICON['available']} available · {STATUS_ICON['degraded']} degraded · {STATUS_ICON['unavailable']} unavailable · "
                   "double border = directly disrupted.")
    elif layer == "Financial obligations":
        only_delayed = st.toggle("Only relationships with delayed payments", value=True, key="net_only_delayed")
        st.graphviz_chart(network.financial_dot(result, hour, pal, only_delayed))
        st.caption("Arrows run payer → recipient; labels give the total scheduled value over the horizon. Red ✕ = "
                   "payments overdue now with an operational reason; amber ⚠ = overdue now for lack of cash. "
                   "Ellipses are funded participants (balance at the end of the hour); dashed ellipses are outside the boundary.")
    else:
        st.graphviz_chart(network.routing_dot(result, hour, pal))
        st.caption("How the networks couple: each participant's payments (as payer, or as recipient of external payments) "
                   "use routes, and each route needs every service on its path. Dashed = backup eligibility added by run C.")

    left, right = st.columns(2)
    with left:
        st.markdown(f"**Services in hour {hour}** ({runs[net_run].label})")

        def cause_text(causes) -> str:
            parts = []
            for c in causes:
                if c["type"] == "disruption":
                    parts.append(f"disruption {c['disruption']} ({c['phase']})")
                elif c["type"] == "dependency":
                    joined = " / ".join(names[m] for m in c["services"])
                    parts.append(("requires " if c["mode"] == "required" else "all alternatives down: ") + joined)
            return "; ".join(parts)

        st.dataframe(pd.DataFrame([{
            "Service": names[x], "State": f"{STATUS_ICON[status[x].state.value]} {status[x].state.value}",
            "Capacity %": status[x].pct,
            "Capacity /h": "not limited" if status[x].capacity is None else str(status[x].capacity),
            "Used": used.get(x, 0), "Binding cause": cause_text(status[x].causes),
        } for x in service_ids]), hide_index=True)
    with right:
        st.markdown(f"**Participants at the end of hour {hour}**")
        obligations = result.scenario.obligations_by_id()
        now = [b for b in result.blocks if b.hour == hour]
        st.dataframe(pd.DataFrame([{
            "Participant": p.name,
            "Balance": money(result.hours[hour].balances[p.id]),
            "Credit drawn": money(result.hours[hour].credit_drawn.get(p.id, 0)) if result.credit_lines else "–",
            "Overdue out": sum(1 for b in now if b.payer == p.id),
            "…cash-blocked": sum(1 for b in now if b.payer == p.id and b.cash),
            "…route-blocked": sum(1 for b in now if b.payer == p.id and b.operational),
            "Overdue in": sum(1 for b in now if obligations[b.obligation_id].recipient == p.id),
        } for p in result.scenario.participants]), hide_index=True)

    with st.expander("Independence analysis (static, from the dependency graph)"):
        st.dataframe(pd.DataFrame([{
            "Route": r.name,
            "Single points of failure": ", ".join(names[x] for x in op_model.single_points_of_failure(r.id)),
        } for r in scenario.routes]), hide_index=True)
        st.caption("A single point of failure is a service whose failure alone makes the route unusable. A backup "
                   "only helps against failures that are not single points of failure for the backup as well.")

# ------------------------------------------------------------------ timeline

with tab_timeline, guarded("The Timeline tab"):
    selected = st.multiselect("Runs to compare", list(runs), default=[rv.key for rv in abc],
                              format_func=lambda k: runs[k].label, key="tl_runs")
    chosen = [runs[k] for k in selected] or abc
    left, right = st.columns(2)
    with left:
        st.markdown("**Overdue value (backlog) over time**")
        show_chart(charts.overdue_chart(chosen, pal, cur, "value"), "tl_value")
    with right:
        st.markdown("**Overdue obligations (count) over time**")
        show_chart(charts.overdue_chart(chosen, pal, cur, "count"), "tl_count")
    st.caption("Backlog recovery: " + "; ".join(
        f"{runs[k].label}: " + ("no outage" if summaries[k]["outage_end_hour"] is None else
                                f"{summaries[k]['backlog_at_outage_end_count']} overdue at end of outage, "
                                + ("cleared by end of hour " + str(summaries[k]["backlog_cleared_hour"])
                                   if summaries[k]["backlog_cleared_hour"] is not None else "not cleared within the horizon"))
        for k in selected if k != "A"))

    credit_runs = [rv for rv in chosen if rv.result.credit_lines]
    if credit_runs:
        st.markdown("**Credit line outstanding** (end of hour, after cash repays draws)")
        show_chart(charts.credit_outstanding(credit_runs, pal, cur), "tl_credit")

    detail_run = st.selectbox("Run for the detailed views below", [k for k in runs if k != "A"],
                              format_func=lambda k: runs[k].label, key="tl_detail")
    detail = runs[detail_run].result
    measure = st.radio("Measure", ["value", "count"], horizontal=True, key="tl_measure",
                       format_func=lambda m: "Value" if m == "value" else "Count")
    st.markdown("**Overdue obligations by blocking reason** (mutually exclusive categories per obligation-hour)")
    show_chart(charts.blocked_by_reason(detail, pal, cur, measure), "tl_reasons")
    st.markdown("**Service capacity lost** (hover for state, capacity and usage)")
    show_chart(charts.service_heatmap(detail, pal), "tl_heat")
    limited = [x for x in service_ids if services[x].capacity_per_hour is not None]
    if limited:
        usage_service = st.selectbox("Service throughput", limited, index=limited.index("psp_alpha") if "psp_alpha" in limited else 0,
                                     format_func=names.get, key="tl_usage_service")
        show_chart(charts.capacity_usage(detail, usage_service, pal), "tl_usage")

    st.markdown("**Cash balances by participant** (end of hour)")
    short_b = summaries["B"]["participants_cash_short"]
    default_people = short_b or participant_ids[:6]
    people = st.multiselect("Participants", participant_ids, default=default_people, format_func=names.get, key=f"{sid}:tl_people")
    if people:
        show_chart(charts.balance_multiples(chosen, people, names, pal, cur), "tl_bal", table=False)
    with st.expander("Table view: hourly series"):
        st.dataframe(pd.DataFrame([{
            "Hour": h.hour, **{f"{runs[k].label} overdue": money(runs[k].result.hours[h.hour].overdue_value) for k in selected},
            **{f"{runs[k].label} overdue (n)": runs[k].result.hours[h.hour].overdue_count for k in selected},
        } for h in runs["A"].result.hours]), hide_index=True)

# ------------------------------------------------------- interventions/evidence

with tab_evidence, guarded("The Interventions & evidence tab"):
    st.markdown(f"#### {len(intervention_keys)} interventions against the same stressed run B")
    st.caption("Rankings below describe this scenario under its declared assumptions only. They are not general "
               "statements about which intervention is better.")
    money_metric = METRIC_KIND.get(rank_metric, "count").startswith("money")
    rows = [{"label": runs["B"].label, "kind": "stress", "raw": summaries["B"][rank_metric],
             "value": cur.to_major(summaries["B"][rank_metric]) if money_metric else summaries["B"][rank_metric]}]
    for k in intervention_keys:
        raw = summaries[k][rank_metric]
        rows.append({"label": runs[k].label, "kind": runs[k].kind, "raw": raw,
                     "value": cur.to_major(raw) if money_metric else raw})
    show_chart(charts.intervention_bars(rows, pal, METRIC_LABEL.get(rank_metric, rank_metric),
                                        lambda v: fmt(v, METRIC_KIND.get(rank_metric, "count"))), "ev_bars")
    ranking = comparison.ranking(rank_metric)

    def change_text(row: dict) -> str:
        if row["reduction_pct"] is None:
            return ""
        if row["reduction"] < 0:
            return f" (**{abs(row['reduction_pct']):.0f}% higher** than B)"
        return f" ({row['reduction_pct']:.0f}% lower than B)" if row["reduction"] > 0 else " (no change)"

    st.markdown("Ranking by **" + METRIC_LABEL.get(rank_metric, rank_metric).lower() + "**: " + " · ".join(
        f"{row['rank']}. {runs[row['run']].label.replace('C · ', '')}{change_text(row)}" for row in ranking))

    for chunk in range(0, len(intervention_keys), 2):
        cols = st.columns(2)
        for col, k in zip(cols, intervention_keys[chunk:chunk + 2]):
            with col:
                st.markdown(f"**{runs[k].label}**")
                for line in comparison.effects[k]:
                    st.markdown(f"- {line}")

    all_keys = ["A", "B"] + intervention_keys
    st.dataframe(pd.DataFrame([{"Metric": label, **{runs[k].label: fmt(summaries[k].get(key), kind) for k in all_keys}}
                               for key, label, kind in HEADLINE_METRICS]), hide_index=True)

    st.divider()
    st.markdown("#### How robust is the ranking?")
    st.caption("The same comparison re-run under changed assumptions, one change at a time: the other settlement rules, "
               "the outage earlier or later, shorter or longer, lower or higher opening balances, less throughput. Each "
               "cell is the share of *that variant's* stress disruption the intervention removes (★ = best in the row; "
               "blue = less disruption, red = more).")
    robust_text = robustness_paragraph(robust)
    if robust_text:
        st.markdown(robust_text)
    robust_labels = {k: INTERVENTION_LABELS[k] for k in robust["plans"]}
    show_chart(charts.robustness_heatmap(robust, robust_labels, pal, cur), "ev_robust")
    regret_kind = METRIC_KIND.get(rank_metric, "count")
    st.dataframe(pd.DataFrame([{
        "Intervention": robust_labels[k],
        "Best (or within 1%)": f"{x['practical_wins']} of {x['tested']}",
        "Largest shortfall against the best": (fmt(x["max_regret"], regret_kind)
                                               + (f" ({x['max_regret_share'] * 100:.0f}% of B)" if x["max_regret_share"] else "")),
        "In variant": next((v["label"] for v in robust["variants"] if v["key"] == x["max_regret_variant"]), "–"),
        "Reduction range": (f"{x['min_reduction_pct']:.0f}% to {x['max_reduction_pct']:.0f}%"
                            if x["min_reduction_pct"] is not None else "–"),
        "Makes it worse in": f"{x['worsens_in']} of {x['tested']}",
    } for k, x in sorted(robust["summary"].items(), key=lambda kv: (kv[1]["max_regret"], kv[0]))]), hide_index=True)
    st.caption("Sorted by largest shortfall against the best option in any variant (minimax regret: smallest first). "
               "'–' in the grid: the variant has no disruption to reduce.")

    package_keys = comparison.package_keys()
    if package_keys and rank_metric in LOWER_IS_BETTER:
        st.divider()
        st.markdown("#### Do the interventions complement each other?")
        st.caption("Each pair is run together on the same B. Benchmark: if the two acted independently, each removing its "
                   "own share of whatever disruption the other leaves, together they would remove 1 − (1 − a)(1 − b). "
                   "More than that is complementary; less is overlapping (they partly fix the same delays); within one "
                   "percentage point is roughly independent. A property of this run, not a general law.")
        prow = []
        for k in package_keys:
            x = comparison.interaction(k, rank_metric)
            if x["combined_pct"] is not None:
                prow.append({"key": k, "label": runs[k].label.replace("C · ", ""), **x})
        if prow:
            show_chart(charts.package_dumbbell(prow, pal), "ev_pairs")
            st.dataframe(pd.DataFrame([{
                "Combination": r["label"], "Removed together": f"{r['combined_pct']:.0f}%",
                "Expected if independent": f"{r['independent_pct']:.0f}%", "Difference": f"{r['excess_pp']:+.1f} pp",
                "In this run": r["verdict"], "Gain over best single": fmt(r["gain_over_best_single"], METRIC_KIND.get(rank_metric, "count")),
            } for r in prow]), hide_index=True)
        else:
            st.info("Nothing to reduce: run B has no disruption by this metric.")
        with st.expander("Mechanism statements for each combination"):
            for k in package_keys:
                st.markdown(f"**{runs[k].label}**")
                for line in comparison.effects[k]:
                    st.markdown(f"- {line}")

    st.divider()
    st.markdown("#### Exact liquidity backstop")
    st.caption("How much hour-0 pre-funding would each participant need so that every payment settles exactly as it "
               "would with unlimited liquidity? Computed from one run with a non-binding credit line, refined by search, "
               "and re-checked by simulation (docs/MODEL.md section 7.5). It shows what liquidity can do, and what it "
               "cannot: route-limited delays remain whatever the amount.")
    (st.success if backstop.verified_sufficient else st.warning)(backstop.verdict(money))
    if backstop.rows:
        left, right = st.columns([3, 2])
        with left:
            show_chart(charts.backstop_bars(backstop, names, pal, cur), "ev_backstop")
        with right:
            st.dataframe(pd.DataFrame([{
                "Participant": names[r["participant"]], "Pre-funding needed": money(r["allocation"]),
                "Credit-line peak": money(r["peak_draw"]), "First draw (hour)": str(r["first_draw_hour"]),
                "Cash-blocked hours in B": r["stress_cash_blocked_hours"],
                "One unit less changes the outcome": {True: "yes", False: "no", None: "–"}[r["tight"]],
            } for r in backstop.rows]), hide_index=True)
        st.caption(f"{backstop.runs_used} simulation runs. Pre-funding these amounts is not a recommendation; it is the "
                   "liquidity this model needs to make cash non-binding in this one scenario.")

    st.divider()
    st.markdown("#### Liquidity buffer sweep")
    st.caption("Reruns B with each tested buffer size (the same amount for every selected participant). It reports the "
               "smallest *tested* buffer that achieves the selected outcome; this is not a proven minimum. A reference "
               "run with cash made non-binding for every participant shows what liquidity cannot fix.")
    sweep_cols = st.columns([2, 2, 2])
    sweep_people = sweep_cols[0].multiselect("Participants buffered", participant_ids,
                                             default=buffer_parts or participant_ids, format_func=names.get, key=f"{sid}:sw_people")
    base_amount = buffer_amount or cur.to_major(max((o.amount for o in scenario.obligations), default=0))
    default_grid = sorted({round(base_amount * f) for f in (0, 0.25, 0.5, 1, 1.5, 2, 3)})
    grid_text = sweep_cols[1].text_input(f"Buffer sizes per participant ({cur.code}, comma-separated)",
                                         ", ".join(f"{g:g}" for g in default_grid), key=f"{sid}:sw_grid")
    outcome = sweep_cols[2].selectbox("Outcome", list(OUTCOMES), format_func=OUTCOMES.get, key="sw_outcome")
    try:
        grid = [cur.to_minor(float(x)) for x in grid_text.split(",") if x.strip()]
    except ValueError:
        grid = []
        st.error("Buffer sizes must be numbers separated by commas.")
    if sweep_people and grid:
        sweep = buffer_sweep(comparison.runs["B"].scenario, sweep_people, grid, outcome=outcome, funding_hour=funding_hour)
        (st.success if sweep.smallest_achieving else st.info)(sweep.verdict(money))
        left, right = st.columns([3, 2])
        with left:
            show_chart(charts.sweep_chart(sweep, pal, cur), "ev_sweep")
            st.caption("Filled marker = outcome achieved at that buffer size; hollow = not achieved.")
        with right:
            st.dataframe(pd.DataFrame([{
                "Buffer each": money(p.amount_each), "Total supplied": money(p.total_supplied),
                "Outcome": "achieved" if p.achieved else "not achieved",
                "Delayed": p.summary["delayed_count"], "Cash-blocked": p.summary["blocked_cash_count"],
                "Value × hours": fmt(p.summary["overdue_value_hours"], "money_hours"),
            } for p in sweep.points]), hide_index=True)
        if sweep.route_limited:
            st.markdown(f"**Not solvable by liquidity:** {len(sweep.route_limited)} obligation(s) stay delayed even with "
                        "cash non-binding for every participant, because their routes are unavailable or saturated.")
            st.dataframe(pd.DataFrame([{
                "Obligation": x["obligation_id"], "Payer": names[x["payer"]], "Recipient": names[x["recipient"]],
                "Amount": money(x["amount"]), "Due hour": x["due_hour"], "Status": x["status"],
                "Delay (h)": x["delay_hours"], "Operational reasons": ", ".join(x["operational_reasons"]),
            } for x in sweep.route_limited]), hide_index=True)

    st.divider()
    st.markdown("#### Why was this payment delayed?")
    st.caption("Deterministic trace assembled from recorded events; every step cites its event IDs (see events.csv in the export).")
    inspect_run = st.selectbox("Run", [k for k in runs if k != "A"], format_func=lambda k: runs[k].label, key="ev_run")
    r = runs[inspect_run].result
    delayed = sorted((o for o in r.outcomes.values() if o.delayed), key=lambda o: (-o.overdue_hours, o.obligation_id))
    if not delayed:
        st.info("No obligation was delayed in this run.")
    else:
        preferred = next((o.obligation_id for o in delayed if o.mechanism.value == "financial_propagation"
                          and o.purpose == "payroll"), delayed[0].obligation_id)
        ids = [o.obligation_id for o in delayed]

        def option_label(oid: str) -> str:
            o = r.outcomes[oid]
            when = f"{o.delay_hours} h late" if o.status is Status.SETTLED_LATE else "unsettled at horizon"
            return (f"{oid} · {o.purpose} · {names[o.payer]} → {names[o.recipient]} · {money(o.amount)} · {when} · "
                    f"{o.mechanism.value.replace('_', ' ')}")

        oid = st.selectbox("Delayed obligation", ids, index=ids.index(preferred), format_func=option_label, key=f"{inspect_run}:ev_oid")
        explanation = Explainer(r).explain(oid)
        st.markdown(f"**{explanation.headline}**")
        st.markdown(" → ".join(f"`{step.short}`" for step in explanation.chain))
        for step in explanation.chain:
            refs = ", ".join(f"E{e}" for e in step.event_ids if e)
            st.markdown(f"- {step.text}" + (f" :gray[(events {refs})]" if refs else ""))
        base_outcome = runs["A"].result.outcomes[oid]
        st.caption(f"In baseline A this obligation settled in hour {base_outcome.settled_hour} "
                   f"({base_outcome.status.value.replace('_', ' ')}). {explanation.caveat}")
        left, right = st.columns(2)
        with left:
            st.markdown("Blocked hours")
            st.dataframe(pd.DataFrame([{
                "Hours": f"{seg['from_hour']}–{seg['to_hour']}" if seg["from_hour"] != seg["to_hour"] else str(seg["from_hour"]),
                "Reasons": ", ".join(seg["reasons"]),
                "Routes": "; ".join(f"{c['route']}: {c['status']}" for c in seg["route_checks"]),
                "Available cash": money(seg["available_cash"]) if seg["available_cash"] is not None else "–",
                "Events": ", ".join(f"E{e}" for e in seg["event_ids"][:4]) + (" …" if len(seg["event_ids"]) > 4 else ""),
            } for seg in explanation.segments]), hide_index=True)
        with right:
            st.markdown("Payer's overdue incoming payments at the first cash block")
            if explanation.linked_inflows:
                st.dataframe(pd.DataFrame([{
                    "Obligation": x["obligation_id"], "From": names[x["payer"]], "Amount": money(x["amount"]),
                    "Status": x["status"].replace("_", " "), "Settled hour": x["settled_hour"],
                    "Mechanism": x["mechanism"].replace("_", " "),
                } for x in explanation.linked_inflows]), hide_index=True)
            else:
                st.caption("No cash block recorded for this obligation.")

    unsettled = [o for o in r.outcomes.values() if o.status is Status.UNSETTLED_AT_HORIZON]
    st.markdown(f"**Unsettled at the horizon in {runs[inspect_run].label}:** {len(unsettled)} obligation(s), "
                f"{money(sum(o.amount for o in unsettled))}. These are reported separately from late payments.")
    if unsettled:
        st.dataframe(pd.DataFrame([{
            "Obligation": o.obligation_id, "Purpose": o.purpose, "Payer": names[o.payer], "Recipient": names[o.recipient],
            "Amount": money(o.amount), "Due hour": o.due_hour, "Last reasons": ", ".join(o.final_reasons),
        } for o in unsettled]), hide_index=True)
    with st.expander("All obligations in this run (table view)"):
        st.dataframe(pd.DataFrame(obligation_rows(r)), hide_index=True)

# ---------------------------------------------------------------- stress lab

PLAN_LABELS = {"none": "No intervention", **{k: INTERVENTION_LABELS[k] for k in KINDS}}
PLAN_KINDS = {"none": "stress", **{k: k for k in KINDS}}
LAB_METRICS = ["overdue_value_hours", "delayed_count", "delayed_value", "unsettled_value", "peak_overdue_value"]

with tab_lab, guarded("The Stress lab"):
    if tab_lab.open is False:
        st.caption("Open this tab to run the systematic analyses.")
    else:
        st.caption("Every number on this tab comes from ordinary simulation runs of the same payment schedule: one run per "
                   "service, duration or start hour tested. Nothing is sampled, and nothing is extrapolated beyond the "
                   "tested values. Interventions use your sidebar settings (the backup's activation delay follows each outage).")

        st.markdown("#### 1 · Which service matters most?")
        c = st.columns(4)
        lab_start = c[0].slider("Outage start (hour)", 0, H - 1, key=f"lab:{sid}:start", **lab_value(f"lab:{sid}:start", start))
        lab_duration = c[1].slider("Duration (h)", 1, H, key=f"lab:{sid}:duration", **lab_value(f"lab:{sid}:duration", duration))
        lab_severity = c[2].slider("Capacity lost (%)", 10, 100, step=5, key=f"lab:{sid}:severity", **lab_value(f"lab:{sid}:severity", severity))
        lab_metric = c[3].selectbox("Rank by", LAB_METRICS, format_func=lambda m: METRIC_LABEL.get(m, m), key="lab:metric",
                                    **first("lab:metric", index=0))
        scan = lab_scan(base_json, lab_start, lab_duration, lab_severity, 0, activation_delay, specs_json, lab_metric)
        worst = scan[0]
        st.markdown(f"With the same {lab_duration} h outage from hour {lab_start}, **{names[worst['service']]}** causes the "
                    f"most disruption ({fmt(worst[lab_metric], METRIC_KIND.get(lab_metric, 'count'))} by "
                    f"{RANKING_LABELS.get(lab_metric, lab_metric)}). Ranks can change with the start hour: see section 3.")
        left, right = st.columns([2, 3])
        with left:
            show_chart(charts.criticality_bars(scan, names, pal, cur, selected=service), "lab_scan_chart")
        with right:
            st.dataframe(pd.DataFrame([{
                "Service": names[r_["service"]], "Routes down": len(r_["routes_unavailable"]),
                "Delayed": r_["delayed_count"], "Value × hours": fmt(r_["overdue_value_hours"], "money_hours"),
                "Short of cash": r_["participants_cash_short_count"],
                "Contagion steps": "–" if r_["max_generation"] is None else str(r_["max_generation"]),
                "Best intervention": (INTERVENTION_LABELS.get(r_.get("best_intervention"), "–") if r_.get("best_intervention")
                                      else ("none helps" if r_["overdue_value_hours"] else "–")),
                "Best cut": f"{r_['best_reduction_pct']:.0f}%" if r_.get("best_intervention") else "–",
                "Independent backup exists": {True: "yes", False: "no", None: "–"}[r_.get("backup_independent")],
            } for r_ in scan]), hide_index=True)
        st.caption("Best intervention per service tries a backup suggested for that service's own footprint (same "
                   "activation delay as the sidebar) plus your buffer, credit line and faster-recovery settings.")

        st.markdown("#### 2 · How long can the outage last?")
        c = st.columns([2, 3, 2])
        prof_service = c[0].selectbox("Service", service_ids, format_func=service_label, key=f"lab:{sid}:prof_service",
                                      **first(f"lab:{sid}:prof_service", index=service_ids.index(service)))
        lab_outcomes = [o for o in OUTCOMES if o != "selected_payers_on_time"]
        prof_outcome = c[1].selectbox("Outcome that must hold", lab_outcomes, format_func=OUTCOMES.get, key="lab:outcome",
                                      **first("lab:outcome", index=lab_outcomes.index("priority_on_time")))
        durations_text = c[2].text_input("Durations tested (h)", key=f"lab:{sid}:durations",
                                         **first(f"lab:{sid}:durations", value="1, 2, 3, 4, 6, 8, 10, 12, 16, 20, 24, 30, 36"))
        try:
            prof_durations = tuple(sorted({int(x) for x in durations_text.split(",") if x.strip() and 0 < int(x) <= H}))
        except ValueError:
            prof_durations = ()
            st.error("Durations must be whole hours separated by commas.")
        prof_specs = specs_json if prof_service == service else json.dumps(
            {k: v for k, v in plan_specs.items() if k != "backup_route"}, sort_keys=True)
        if prof_durations:
            prof = lab_profile(base_json, prof_service, start, prof_durations, severity, recovery_hours, prof_specs,
                               prof_outcome, json.dumps([d.model_dump(mode="json") for d in extra_disruptions]))
            show_chart(charts.profile_lines(prof["rows"], "duration_h", "Outage duration (hours)", PLAN_LABELS, PLAN_KINDS,
                                            pal, cur, marker_x=duration if prof_service == service else None,
                                            marker_label="B"), "lab_profile_chart")
            tol_rows = []
            for plan, tol in prof["tolerance"].items():
                tol_rows.append({
                    "Plan": PLAN_LABELS.get(plan, plan),
                    "Longest outage with the outcome intact": "none tested" if tol["longest_tolerated_h"] is None else f"{tol['longest_tolerated_h']} h",
                    "First breach": "none up to " + f"{max(tol['tested'])} h" if tol["first_breach_h"] is None else f"{tol['first_breach_h']} h",
                    "Note": "not monotone: holds again at some longer outages" if tol["non_monotone"] else "",
                })
            st.dataframe(pd.DataFrame(tol_rows), hide_index=True)
            none_tol = prof["tolerance"]["none"]
            st.caption(f"Outcome: {OUTCOMES[prof_outcome].lower()}. Without an intervention it holds for outages up to "
                       + ("no tested duration" if none_tol["longest_tolerated_h"] is None else f"{none_tol['longest_tolerated_h']} h")
                       + f" from hour {start}. Tested durations only; the true threshold lies between the last success and "
                       "the first breach. Outages running past the horizon leave payments unsettled (right-censoring)."
                       + (" Concurrent disruptions from the sidebar are included." if extra_disruptions else "")
                       + ("" if prof_service == service else " The backup is omitted here because it was configured for B's service."))

        st.markdown("#### 3 · When is the worst time for it to start?")
        c = st.columns([2, 2, 3])
        start_duration = c[0].slider("Duration (h)", 1, H, key=f"lab:{sid}:start_duration", **lab_value(f"lab:{sid}:start_duration", duration))
        compare_key = f"lab:{sid}:{prof_service}:compare_plan"
        compare_plan = c[1].selectbox("Compare with", ["none"] + [k for k in KINDS if k in plan_specs and (k != "backup_route" or prof_service == service)],
                                      format_func=lambda k: "Nothing (stress only)" if k == "none" else PLAN_LABELS[k],
                                      key=compare_key, **first(compare_key, index=0))
        last_start = max(0, H - start_duration)
        starts = tuple(range(0, last_start + 1))
        start_specs = json.dumps({k: v for k, v in plan_specs.items() if k == compare_plan}, sort_keys=True)
        srows = lab_starts(base_json, prof_service, start_duration, starts, severity, recovery_hours, start_specs)
        show_chart(charts.profile_lines(srows, "start_hour", "Outage start hour", PLAN_LABELS, PLAN_KINDS, pal, cur,
                                        marker_x=start if prof_service == service else None, marker_label="B"),
                   "lab_start_chart")
        none_rows = [x for x in srows if x["plan"] == "none"]
        if none_rows:
            hi = max(none_rows, key=lambda x: (x["overdue_value_hours"], -x["start_hour"]))
            lo = min(none_rows, key=lambda x: (x["overdue_value_hours"], x["start_hour"]))
            harmless = [x["start_hour"] for x in none_rows if x["overdue_value_hours"] == 0]
            st.caption(f"{names[prof_service]}, {start_duration} h outage: the worst tested start is hour {hi['start_hour']} "
                       f"({fmt(hi['overdue_value_hours'], 'money_hours')}, {hi['delayed_count']} delayed); the mildest is hour "
                       f"{lo['start_hour']} ({fmt(lo['overdue_value_hours'], 'money_hours')})."
                       + (f" {len(harmless)} of {len(none_rows)} start hours delay nothing at all." if harmless else "")
                       + f" Starts after hour {last_start} are not tested, so that the outage ends within the horizon.")
        if st.toggle("Show the full start × duration grid (one run per cell)", key=f"lab:{sid}:surface",
                     **first(f"lab:{sid}:surface", value=False)):
            grid_starts = tuple(range(0, H, 2))
            grid_durations = tuple(d for d in (1, 2, 4, 6, 8, 12, 16, 24, 36) if d <= H)
            surf = lab_surface(base_json, prof_service, grid_starts, grid_durations, severity)
            show_chart(charts.surface_heatmap(surf, pal, cur), "lab_surface_chart")
            st.caption(f"Overdue value × hours with no intervention, {len(grid_starts) * len(grid_durations)} runs. "
                       "Darker = more disruption. Long outages that start late run past the horizon.")

# ------------------------------------------------------------ scenario editor

with tab_editor, guarded("The scenario editor"):
    st.markdown("#### Edit the scenario")
    st.caption("Change balances, capacities and the payment schedule, then validate. Edits live only in this browser "
               "session (nothing is written to disk) and every edit is re-validated; download the JSON to keep it. "
               "All data stay synthetic: do not enter real or confidential information.")
    parties = participant_ids + [e.id for e in scenario.external_parties]
    with st.form("scenario_editor"):
        g = st.columns(3)
        bal_pct = g[0].number_input("Scale opening balances (%)", 0, 1000, 100, step=10, key="ed:bal_pct")
        amt_pct = g[1].number_input("Scale obligation amounts (%)", 1, 1000, 100, step=10, key="ed:amt_pct")
        shift = g[2].number_input("Shift every due hour by (h)", -H, H, 0, key="ed:shift",
                                  help="Positive = later. Payments shifted outside the horizon are rejected by validation.")
        st.markdown("**Participants**")
        parts_df = st.data_editor(pd.DataFrame([{
            "id": p.id, "name": p.name, "sector": p.sector, f"opening balance ({cur.code})": cur.to_major(p.initial_balance),
        } for p in scenario.participants]), disabled=["id"], hide_index=True, key="ed:parts")
        st.markdown("**Services**")
        svc_df = st.data_editor(pd.DataFrame([{
            "id": x.id, "name": x.name, "kind": x.kind.value,
            "capacity per hour": x.capacity_per_hour if x.capacity_per_hour is not None else None,
        } for x in scenario.services]), disabled=["id", "kind"], hide_index=True, key="ed:svc",
            column_config={"capacity per hour": st.column_config.NumberColumn(min_value=1, step=1,
                                                                              help="Empty = not throughput-limited")})
        st.markdown("**Obligations** (add or delete rows; routes separated by `;`)")
        obl_df = st.data_editor(pd.DataFrame([{
            "id": o.id, "payer": o.payer, "recipient": o.recipient, f"amount ({cur.code})": cur.to_major(o.amount),
            "due hour": o.due_hour, "priority": o.priority, "routes": ";".join(o.routes), "purpose": o.purpose,
        } for o in scenario.obligations]), num_rows="dynamic", hide_index=True, key="ed:obl",
            column_config={
                "payer": st.column_config.SelectboxColumn(options=parties, required=True),
                "recipient": st.column_config.SelectboxColumn(options=parties, required=True),
                "due hour": st.column_config.NumberColumn(min_value=0, max_value=H - 1, step=1),
                "priority": st.column_config.NumberColumn(min_value=1, max_value=9, step=1, help="1 = most urgent"),
            })
        submitted = st.form_submit_button("Validate and apply", type=MAIN_BUTTON)
    if submitted:
        def _num(value, default=None):
            if value is None or (isinstance(value, float) and math.isnan(value)):
                return default
            return value

        data = scenario.model_dump(mode="json")
        try:
            data["participants"] = [{**p, "name": str(row["name"]), "sector": str(row["sector"] or ""),
                                     "initial_balance": round(cur.to_minor(float(_num(row[f"opening balance ({cur.code})"], 0)))
                                                              * bal_pct / 100)}
                                    for p, (_, row) in zip(data["participants"], parts_df.iterrows())]
            data["services"] = [{**x, "name": str(row["name"]),
                                 "capacity_per_hour": None if _num(row["capacity per hour"]) is None else int(row["capacity per hour"])}
                                for x, (_, row) in zip(data["services"], svc_df.iterrows())]
            obligations_out = []
            for index, (_, row) in enumerate(obl_df.iterrows()):
                amount = _num(row[f"amount ({cur.code})"])
                due = _num(row["due hour"])
                obligations_out.append({
                    "id": str(row["id"]).strip() if _num(row["id"]) not in (None, "") else f"new_{index + 1}",
                    "payer": row["payer"], "recipient": row["recipient"],
                    "amount": round(cur.to_minor(float(amount)) * amt_pct / 100) if amount is not None else 0,
                    "due_hour": int(due) + int(shift) if due is not None else -1,
                    "priority": int(_num(row["priority"], 3)),
                    "routes": [x.strip() for x in str(_num(row["routes"], "")).replace(",", ";").split(";") if x.strip()],
                    "purpose": str(_num(row["purpose"], "")),
                })
            data["obligations"] = obligations_out
            edited = Scenario.model_validate(data)
        except ValidationError as exc:
            st.error("Not applied: the edited scenario failed validation. Fix these and apply again.", icon=":material/block:")
            st.markdown("\n".join(f"- {plain(line)}" for line in friendly_errors(exc)))
        except (TypeError, ValueError, KeyError) as exc:
            st.error(f"Not applied: {plain(str(exc))}", icon=":material/block:")
        else:
            st.session_state[edit_key] = scenario_json(edited)
            for k in ("ed:parts", "ed:svc", "ed:obl", "ed:bal_pct", "ed:amt_pct", "ed:shift"):
                st.session_state.pop(k, None)
            st.rerun()
    if edit_key in st.session_state:
        st.download_button("Download the edited scenario (.json)", data=st.session_state[edit_key],
                           file_name=f"{sid}_edited.json", mime="application/json", key="ed:download")
        st.caption("Run it later with `cyberliquidity run <file>` or upload it in the sidebar.")

# ---------------------------------------------------------------------- model

with tab_model, guarded("The model note"):
    with st.expander("Glossary of terms", icon=":material/menu_book:", expanded=True):
        render_glossary()
    if MODEL_NOTE.exists():
        st.markdown(MODEL_NOTE.read_text(encoding="utf-8"))
    else:
        st.info("docs/MODEL.md not found.")
    with st.expander("Scenario JSON for run B (exactly what was simulated)"):
        st.code(json.dumps(comparison.runs["B"].scenario.model_dump(mode="json"), indent=2)[:20000], language="json")

# ---------------------------------------------------------------------- footer

st.divider()
audits = {key: audit(rv.result) for key, rv in runs.items()}
failed = {key: report for key, report in audits.items() if not report.ok}
if failed:
    st.error("**Integrity audit failed** for " + ", ".join(runs[k].label for k in failed) + ". Do not rely on these "
             "numbers; please report this as a bug.\n\n"
             + "\n".join(f"- {plain(line)}" for report in failed.values() for line in report.failures[:3]),
             icon=":material/gpp_bad:")
else:
    movements = sum(report.entries for report in audits.values())
    st.caption(f":material/verified_user: **Integrity audit passed** for all {len(audits)} runs on this page: "
               f"{movements:,} cash movements replay exactly to every recorded balance.  \n"
               f"CyberLiquidity {__version__} · scenario digest `{scenario_digest(scenario)[:16]}` · {DISCLAIMER}")
