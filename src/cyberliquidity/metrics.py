"""Metrics and tables derived from a ``SimulationResult`` (definitions: docs/MODEL.md section 6).

Deliberately *not* computed: economic loss, insolvency, probabilities, expected
losses or "liquidity required". Overdue value measures how much payment value is
late, not what anyone loses.
"""

from __future__ import annotations

import json
import math
from typing import Any

from .engine import CASH_REASON, CATEGORIES, Mechanism, Reason, SimulationResult, Status

# Metrics where a lower value means less payment disruption (usable for rankings).
LOWER_IS_BETTER = (
    "overdue_value_hours",
    "delayed_count",
    "delayed_value",
    "peak_overdue_value",
    "unsettled_value",
    "unsettled_count",
    "late_count",
    "overdue_obligation_hours",
    "urgent_delayed_count",
    "urgent_overdue_value_hours",
)

# Short lowercase names of the ranking metrics, for sentences.
RANKING_LABELS = {
    "overdue_value_hours": "overdue value × hours",
    "delayed_count": "number of delayed obligations",
    "delayed_value": "delayed value",
    "peak_overdue_value": "peak overdue value",
    "unsettled_value": "value unsettled at the horizon",
    "unsettled_count": "obligations unsettled at the horizon",
    "late_count": "obligations settled late",
    "overdue_obligation_hours": "overdue obligation-hours",
    "urgent_delayed_count": "delayed priority-1 payments (e.g. payroll, tax)",
    "urgent_overdue_value_hours": "overdue value × hours of priority-1 payments",
}

HEADLINE_METRICS: tuple[tuple[str, str, str], ...] = (
    ("on_time_count", "Settled on time (count)", "count"),
    ("on_time_value", "Settled on time (value)", "money"),
    ("late_count", "Settled late (count)", "count"),
    ("late_value", "Settled late (value)", "money"),
    ("unsettled_count", "Unsettled at horizon (count)", "count"),
    ("unsettled_value", "Unsettled at horizon (value)", "money"),
    ("peak_overdue_value", "Peak outstanding overdue value", "money"),
    ("peak_overdue_hour", "Hour of peak overdue value", "hour"),
    ("overdue_value_hours", "Overdue value x hours (disruption intensity)", "money_hours"),
    ("mean_delay_settled_h", "Mean delay of completed payments (h)", "hours"),
    ("p90_delay_settled_h", "90th percentile delay of completed payments (h)", "hours"),
    ("max_delay_settled_h", "Longest delay of a completed payment (h)", "hours"),
    ("blocked_service_unavailable_count", "Obligations ever blocked by an unavailable service", "count"),
    ("blocked_capacity_count", "Obligations ever blocked by exhausted capacity", "count"),
    ("blocked_cash_count", "Obligations ever blocked by insufficient cash", "count"),
    ("participants_cash_short_count", "Participants with a cash-blocked payment", "count"),
    ("backlog_hours_to_clear", "Hours to clear the overdue backlog after the outage", "hours"),
    ("settled_via_backup_count", "Payments settled via a backup route", "count"),
    ("intervention_funding", "Pre-funded liquidity supplied (buffers)", "money"),
    ("credit_peak_total", "Credit line usage (sum of participants' peak draws)", "money"),
    ("urgent_delayed_count", "Priority-1 payments delayed (e.g. payroll, tax)", "count"),
    ("settled_via_offsetting_count", "Payments settled by multilateral offsetting", "count"),
    ("offsetting_liquidity_saved", "Liquidity saved by offsetting (gross settled − net cash moved)", "money"),
    ("reserve_held_count", "Payments held back by a reserve rule although they could be funded", "count"),
)


def percentile(sorted_values: list[int], p: float) -> int | None:
    """Nearest-rank percentile of an ascending list (None if empty)."""
    if not sorted_values:
        return None
    rank = max(1, math.ceil(p / 100 * len(sorted_values)))
    return sorted_values[rank - 1]


def backlog_recovery(r: SimulationResult) -> dict[str, Any]:
    """Backlog left at the end of the last outage phase and how long it took to clear."""
    ends = [d.end_hour for d in r.effective_disruptions if d.duration_hours > 0]
    empty = {
        "outage_end_hour": None,
        "backlog_at_outage_end_count": None,
        "backlog_at_outage_end_value": None,
        "backlog_cleared_hour": None,
        "backlog_hours_to_clear": None,
    }
    if not ends or not r.hours:
        return empty
    end = max(ends)
    last_outage_hour = min(end, len(r.hours)) - 1
    at_end = r.hours[last_outage_hour]
    if at_end.overdue_count == 0:
        cleared, needed = last_outage_hour, 0
    else:
        cleared = next((h.hour for h in r.hours[end:] if h.overdue_count == 0), None)
        needed = (cleared - end + 1) if cleared is not None else None
    return {
        "outage_end_hour": end,
        "backlog_at_outage_end_count": at_end.overdue_count,
        "backlog_at_outage_end_value": at_end.overdue_value,
        "backlog_cleared_hour": cleared,
        "backlog_hours_to_clear": needed,
    }


def summarize(r: SimulationResult) -> dict[str, Any]:
    outcomes = list(r.outcomes.values())
    on_time = [o for o in outcomes if o.status is Status.SETTLED_ON_TIME]
    late = [o for o in outcomes if o.status is Status.SETTLED_LATE]
    unsettled = [o for o in outcomes if o.status is Status.UNSETTLED_AT_HORIZON]
    settled_delays = sorted(o.delay_hours for o in on_time + late)
    late_delays = sorted(o.delay_hours for o in late)
    total_value = sum(o.amount for o in outcomes)

    peak = max(r.hours, key=lambda h: (h.overdue_value, -h.hour)) if r.hours else None

    def ever(reason: str) -> list:
        return [o for o in outcomes if o.reason_hours.get(reason, 0) > 0]

    blocked_unavailable = ever(Reason.SERVICE_UNAVAILABLE.value)
    blocked_capacity = ever(Reason.CAPACITY_EXHAUSTED.value)
    blocked_not_active = ever(Reason.ROUTE_NOT_YET_ACTIVE.value)
    blocked_cash = ever(CASH_REASON)

    mechanisms = {}
    for m in Mechanism:
        if m is Mechanism.NONE:
            continue
        group = [o for o in outcomes if o.mechanism is m]
        mechanisms[m.value] = {"count": len(group), "value": sum(o.amount for o in group)}

    category_value_hours = dict.fromkeys(CATEGORIES, 0)
    category_obligation_hours = dict.fromkeys(CATEGORIES, 0)
    for record in r.blocks:
        category_value_hours[record.category] += record.amount
        category_obligation_hours[record.category] += 1

    via_backup = [o for o in outcomes if o.via_backup]
    via_offsetting = [o for o in outcomes if o.via_offsetting]
    urgent = [o for o in outcomes if o.priority == 1]
    held = [b for b in r.blocks if b.held_by_reserve]
    held_ids = {b.obligation_id for b in held}
    totals = r.totals
    conservation_ok = totals["final_internal_cash"] == (
        totals["initial_internal_cash"] + totals["external_inflow"] - totals["external_outflow"]
        + totals["intervention_funding"] + totals.get("credit_drawn", 0) - totals.get("credit_repaid", 0)
    )
    min_balance = {
        pid: min([r.initial_balances[pid]] + [h.balances[pid] for h in r.hours]) for pid in r.initial_balances
    }

    summary: dict[str, Any] = {
        "obligations_count": len(outcomes),
        "obligations_value": total_value,
        "on_time_count": len(on_time),
        "on_time_value": sum(o.amount for o in on_time),
        "late_count": len(late),
        "late_value": sum(o.amount for o in late),
        "unsettled_count": len(unsettled),
        "unsettled_value": sum(o.amount for o in unsettled),
        "delayed_count": len(late) + len(unsettled),
        "delayed_value": sum(o.amount for o in late + unsettled),
        "on_time_share_count": len(on_time) / len(outcomes) if outcomes else None,
        "on_time_share_value": sum(o.amount for o in on_time) / total_value if total_value else None,
        "peak_overdue_value": peak.overdue_value if peak else 0,
        "peak_overdue_hour": peak.hour if peak and peak.overdue_value > 0 else None,
        "peak_overdue_count": max((h.overdue_count for h in r.hours), default=0),
        "overdue_value_hours": sum(h.overdue_value for h in r.hours),
        "overdue_obligation_hours": sum(h.overdue_count for h in r.hours),
        "mean_delay_settled_h": (sum(settled_delays) / len(settled_delays)) if settled_delays else None,
        "median_delay_settled_h": percentile(settled_delays, 50),
        "p90_delay_settled_h": percentile(settled_delays, 90),
        "max_delay_settled_h": settled_delays[-1] if settled_delays else None,
        "mean_delay_late_h": (sum(late_delays) / len(late_delays)) if late_delays else None,
        "blocked_service_unavailable_count": len(blocked_unavailable),
        "blocked_service_unavailable_value": sum(o.amount for o in blocked_unavailable),
        "blocked_capacity_count": len(blocked_capacity),
        "blocked_capacity_value": sum(o.amount for o in blocked_capacity),
        "blocked_route_not_active_count": len(blocked_not_active),
        "blocked_cash_count": len(blocked_cash),
        "blocked_cash_value": sum(o.amount for o in blocked_cash),
        "category_value_hours": category_value_hours,
        "category_obligation_hours": category_obligation_hours,
        "mechanisms": mechanisms,
        "participants_cash_short": sorted({b.payer for b in r.blocks if b.cash}),
        "settled_via_backup_count": len(via_backup),
        "settled_via_backup_value": sum(o.amount for o in via_backup),
        "settled_via_offsetting_count": len(via_offsetting),
        "settled_via_offsetting_value": sum(o.amount for o in via_offsetting),
        "offsetting_batches": totals.get("offsetting_batches", 0),
        "offsetting_gross_value": totals.get("offsetting_gross_value", 0),
        "offsetting_net_value": totals.get("offsetting_net_value", 0),
        "offsetting_liquidity_saved": totals.get("offsetting_gross_value", 0) - totals.get("offsetting_net_value", 0),
        "urgent_count": len(urgent),
        "urgent_delayed_count": sum(1 for o in urgent if o.delayed),
        "urgent_delayed_value": sum(o.amount for o in urgent if o.delayed),
        "urgent_overdue_value_hours": sum(o.amount * o.overdue_hours for o in urgent),
        "reserve_held_count": len(held_ids),
        "reserve_held_value": sum(o.amount for o in outcomes if o.obligation_id in held_ids),
        "reserve_held_value_hours": sum(b.amount for b in held),
        "min_balance": min_balance,
        "initial_internal_cash": totals["initial_internal_cash"],
        "final_internal_cash": totals["final_internal_cash"],
        "external_inflow": totals["external_inflow"],
        "external_outflow": totals["external_outflow"],
        "intervention_funding": totals["intervention_funding"],
        "credit_drawn_total": totals.get("credit_drawn", 0),
        "credit_repaid_total": totals.get("credit_repaid", 0),
        "credit_outstanding_at_horizon": totals.get("credit_outstanding", 0),
        "credit_peak": dict(r.credit_peak),
        "credit_peak_total": sum(r.credit_peak.values()),
        "credit_outstanding_value_hours": sum(sum(h.credit_drawn.values()) for h in r.hours),
        "participants_drawing_credit": sorted(p for p, v in r.credit_peak.items() if v > 0),
        "conservation_ok": conservation_ok,
    }
    summary["participants_cash_short_count"] = len(summary["participants_cash_short"])
    summary.update(backlog_recovery(r))
    return summary


def mechanism_shares(summary: dict[str, Any]) -> dict[str, float | None]:
    """Shares of overdue value-hours by blocking category (sums to 1 when anything is overdue)."""
    values = summary["category_value_hours"]
    total = sum(values.values())
    return {c: (values[c] / total if total else None) for c in CATEGORIES}


def delay_distribution(r: SimulationResult) -> list[dict[str, int]]:
    """Completed payments by settlement delay in hours (0 = on time). Unsettled ones are excluded."""
    buckets: dict[int, list[int]] = {}
    for o in r.outcomes.values():
        if o.delay_hours is not None:
            buckets.setdefault(o.delay_hours, []).append(o.amount)
    return [{"delay_hours": d, "count": len(v), "value": sum(v)} for d, v in sorted(buckets.items())]


# --------------------------------------------------------------------- tables


def _js(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def obligation_rows(r: SimulationResult) -> list[dict[str, Any]]:
    names = r.scenario.names()
    obligations = r.scenario.obligations_by_id()
    rows = []
    for o in r.outcomes.values():
        ob = obligations[o.obligation_id]
        rows.append(
            {
                "obligation_id": o.obligation_id,
                "payer": o.payer,
                "payer_name": names.get(o.payer, o.payer),
                "recipient": o.recipient,
                "recipient_name": names.get(o.recipient, o.recipient),
                "amount_minor": o.amount,
                "due_hour": o.due_hour,
                "priority": o.priority,
                "purpose": o.purpose,
                "eligible_routes": ";".join(rid for rid, _ in r.eligible_routes[o.obligation_id]),
                "primary_routes": ";".join(ob.routes),
                "status": o.status.value,
                "settled_hour": o.settled_hour,
                "delay_hours": o.delay_hours,
                "overdue_hours": o.overdue_hours,
                "route_used": o.route_used,
                "via_backup": o.via_backup,
                "via_offsetting": o.via_offsetting,
                "blocked_hours": o.blocked_hours,
                "hours_service_unavailable": o.reason_hours.get(Reason.SERVICE_UNAVAILABLE.value, 0),
                "hours_capacity_exhausted": o.reason_hours.get(Reason.CAPACITY_EXHAUSTED.value, 0),
                "hours_route_not_yet_active": o.reason_hours.get(Reason.ROUTE_NOT_YET_ACTIVE.value, 0),
                "hours_insufficient_cash": o.reason_hours.get(CASH_REASON, 0),
                **{f"hours_{c}": o.category_hours[c] for c in CATEGORIES},
                "mechanism": o.mechanism.value,
                "first_block_hour": o.first_block_hour,
                "final_reasons": ";".join(o.final_reasons),
                "settlement_event_id": o.settlement_event_id,
            }
        )
    return rows


def hourly_rows(r: SimulationResult) -> list[dict[str, Any]]:
    rows = []
    for h in r.hours:
        row: dict[str, Any] = {
            "hour": h.hour,
            "overdue_count": h.overdue_count,
            "overdue_value_minor": h.overdue_value,
            "newly_due_count": h.newly_due_count,
            "newly_due_value_minor": h.newly_due_value,
            "settled_count": h.settled_count,
            "settled_value_minor": h.settled_value,
            "settled_late_count": h.settled_late_count,
            "settled_late_value_minor": h.settled_late_value,
        }
        for c in CATEGORIES:
            row[f"blocked_{c}_count"] = h.category_counts[c]
            row[f"blocked_{c}_value_minor"] = h.category_values[c]
        for reason in Reason:
            row[f"reason_{reason.value}_count"] = h.reason_counts.get(reason.value, 0)
            row[f"reason_{reason.value}_value_minor"] = h.reason_values.get(reason.value, 0)
        row["total_internal_cash_minor"] = sum(h.balances.values())
        row["cumulative_external_inflow_minor"] = h.cumulative_external_inflow
        row["cumulative_external_outflow_minor"] = h.cumulative_external_outflow
        row["cumulative_intervention_funding_minor"] = h.cumulative_intervention_funding
        row["credit_outstanding_minor"] = sum(h.credit_drawn.values())
        row["cumulative_credit_drawn_minor"] = h.cumulative_credit_drawn
        row["cumulative_credit_repaid_minor"] = h.cumulative_credit_repaid
        rows.append(row)
    return rows


def balance_rows(r: SimulationResult) -> list[dict[str, Any]]:
    names = r.scenario.names()
    return [
        {"hour": h.hour, "participant": pid, "participant_name": names[pid], "balance_minor": bal,
         "credit_outstanding_minor": h.credit_drawn.get(pid, 0)}
        for h in r.hours
        for pid, bal in h.balances.items()
    ]


def service_rows(r: SimulationResult) -> list[dict[str, Any]]:
    services = r.scenario.services_by_id()
    rows = []
    for hour, status in enumerate(r.service_status):
        used = r.hours[hour].service_used
        for sid, st in status.items():
            rows.append(
                {
                    "hour": hour,
                    "service": sid,
                    "service_name": services[sid].name,
                    "kind": services[sid].kind.value,
                    "state": st.state.value,
                    "capacity_pct": st.pct,
                    "own_capacity_pct": st.own_pct,
                    "capacity": st.capacity,
                    "nominal_capacity": st.nominal_capacity,
                    "used": used.get(sid, 0),
                    "causes": _js(list(st.causes)),
                }
            )
    return rows


def block_rows(r: SimulationResult) -> list[dict[str, Any]]:
    return [
        {
            "hour": b.hour,
            "obligation_id": b.obligation_id,
            "payer": b.payer,
            "amount_minor": b.amount,
            "reasons": ";".join(b.reasons),
            "category": b.category,
            "route_checks": _js(list(b.route_checks)),
            "payer_balance_minor": b.payer_balance,
            "payer_available_cash_minor": b.available_cash,
            "payer_available_credit_minor": b.available_credit,
            "payer_reserved_for_priority_minor": b.reserved,
            "held_by_reserve": b.held_by_reserve,
            "shortfall_minor": b.shortfall,
            "payer_overdue_inflow_count": len(b.overdue_inflows),
            "payer_overdue_inflow_value_minor": b.overdue_inflow_value,
            "payer_overdue_inflows": _js([list(x) for x in b.overdue_inflows]),
            "event_id": b.event_id,
        }
        for b in r.blocks
    ]


def event_rows(r: SimulationResult) -> list[dict[str, Any]]:
    return [
        {
            "event_id": e.event_id,
            "hour": e.hour,
            "type": e.type,
            "obligation_id": e.obligation_id,
            "service_id": e.service_id,
            "participant_id": e.participant_id,
            "amount_minor": e.amount,
            "data": _js(e.data),
        }
        for e in r.events
    ]


def ledger_rows(r: SimulationResult) -> list[dict[str, Any]]:
    return [
        {
            "entry_id": entry.entry_id,
            "hour": entry.hour,
            "event_id": entry.event_id,
            "kind": entry.kind,
            "reference": entry.reference,
            "debit_account": entry.debit_account,
            "credit_account": entry.credit_account,
            "amount_minor": entry.amount,
            "debit_balance_after_minor": entry.debit_balance_after,
            "credit_balance_after_minor": entry.credit_balance_after,
        }
        for entry in r.ledger
    ]
