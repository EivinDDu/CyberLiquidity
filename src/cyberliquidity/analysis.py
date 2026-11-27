"""Systematic stress analysis (the app's "Stress lab"). Every number is an ordinary simulation run.

* ``criticality_scan``: the same outage applied to each service in turn - which service
  matters most for this payment schedule, at this time and for this long?
* ``duration_profile``: one service, growing outage durations, with and without
  interventions - how long can the outage last before a chosen outcome breaks?
* ``start_profile``: one service and duration, moving start hour - how much does the
  timing relative to the payment calendar matter?
* ``impact_surface``: start hour x duration grid of one metric.

Interventions are passed as *plans*: functions that build the intervention for a given
disruption, so that e.g. a backup's activation can follow a moving outage start. Nothing
here samples, estimates probabilities or extrapolates beyond the tested grid.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any

from .cascade import analyse_cascade
from .engine import simulate
from .metrics import summarize
from .models import Disruption, Intervention, Scenario
from .operational import OperationalModel
from .sweep import OUTCOMES, outcome_achieved
from .variants import make_disruption, replace, suggest_backup, with_intervention

Plan = Callable[[Scenario, Disruption], "Intervention | Sequence[Intervention] | None"]

DISRUPTION_ID = "D1"


def _stressed(base: Scenario, disruption: Disruption, extra: Sequence[Disruption] = ()) -> Scenario:
    return replace(base, disruptions=(disruption, *extra), interventions=())


def _apply(stressed: Scenario, disruption: Disruption, plan: Plan | None) -> Scenario | None:
    if plan is None:
        return stressed
    built = plan(stressed, disruption)
    if built is None:
        return None
    items = list(built) if isinstance(built, (list, tuple)) else [built]
    return with_intervention(stressed, *items) if items else stressed


def _row(result, outcome: str | None) -> dict[str, Any]:
    m = summarize(result)
    row = {
        "overdue_value_hours": m["overdue_value_hours"],
        "delayed_count": m["delayed_count"],
        "delayed_value": m["delayed_value"],
        "unsettled_count": m["unsettled_count"],
        "unsettled_value": m["unsettled_value"],
        "peak_overdue_value": m["peak_overdue_value"],
        "participants_cash_short_count": m["participants_cash_short_count"],
        "blocked_cash_count": m["blocked_cash_count"],
        "results_digest": result.digest(),
    }
    if outcome is not None:
        row["outcome_achieved"] = outcome_achieved(result, outcome, ())
    return row


def criticality_scan(base: Scenario, start_hour: int, duration_hours: int, severity_pct: int = 100,
                     recovery_hours: int = 0, services: Iterable[str] | None = None,
                     plans: Mapping[str, Plan] | None = None, backup_delay_hours: int | None = None,
                     metric: str = "overdue_value_hours") -> list[dict[str, Any]]:
    """Outage of each service in turn (same timing), worst first by ``metric``.

    With ``backup_delay_hours`` set, each service also gets the backup suggested for *its*
    footprint; ``plans`` adds further interventions (e.g. a buffer). The best of these per
    service is reported as ``best_intervention``.
    """
    op = OperationalModel(base)
    names = base.names()
    ids = list(services) if services is not None else [s.id for s in base.services]
    rows = []
    for sid in ids:
        d = make_disruption(sid, start_hour, duration_hours, severity_pct, recovery_hours, id=DISRUPTION_ID)
        stressed = _stressed(base, d)
        result = simulate(stressed)
        cascade = analyse_cascade(result).summary()
        footprint = op.failure_footprint(sid, severity_pct)
        row = {
            "service": sid,
            "service_name": names[sid],
            "kind": base.services_by_id()[sid].kind.value,
            "routes_unavailable": footprint["unavailable_routes"],
            "routes_degraded": footprint["degraded_routes"],
            "services_affected": len(footprint["unavailable_services"]) + len(footprint["degraded_services"]),
            **_row(result, None),
            "direct_count": cascade["direct_count"],
            "propagated_count": cascade["propagated_count"],
            "max_generation": cascade["max_generation"],
            "indirectly_affected": cascade["indirectly_affected_participants"],
            "propagated_value_hours_share": cascade["propagated_value_hours_share"],
        }
        options: dict[str, Plan] = {}
        if backup_delay_hours is not None:
            suggestion = suggest_backup(stressed, d)
            if suggestion.primary_routes and suggestion.backup_route:
                from .variants import make_backup

                active = min(start_hour + backup_delay_hours, base.horizon_hours - 1)
                options["backup_route"] = (lambda s, dd, sg=suggestion, a=active:
                                           make_backup(sg.primary_routes, sg.backup_route, a, id="backup"))
                row["suggested_backup"] = suggestion.backup_route
                row["backup_independent"] = suggestion.independent
        options.update(plans or {})
        effects = {}
        for key, plan in options.items():
            scenario = _apply(stressed, d, plan)
            if scenario is None:
                continue
            effects[key] = summarize(simulate(scenario))[metric]
        if effects and row[metric]:
            best = min(effects, key=lambda k: (effects[k], k))
            row["intervention_values"] = effects
            if effects[best] < row[metric]:
                row["best_intervention"] = best
                row["best_reduction_pct"] = (row[metric] - effects[best]) / row[metric] * 100
            else:
                row["best_intervention"] = None  # none of the tested interventions reduces the metric
                row["best_reduction_pct"] = 0.0
        rows.append(row)
    rows.sort(key=lambda r: (-r[metric], r["service"]))
    return rows


def duration_profile(base: Scenario, service: str, start_hour: int, durations: Sequence[int],
                     severity_pct: int = 100, recovery_hours: int = 0,
                     plans: Mapping[str, Plan] | None = None, outcome: str | None = "priority_on_time",
                     extra_disruptions: Sequence[Disruption] = ()) -> dict[str, Any]:
    """Outcome and disruption metrics for each tested duration, for ``none`` and each plan."""
    if outcome is not None and outcome not in OUTCOMES:
        raise ValueError(f"unknown outcome '{outcome}'")
    keys = ["none", *(plans or {})]
    rows = []
    for duration in sorted(set(int(x) for x in durations if int(x) >= 1)):
        d = make_disruption(service, start_hour, duration, severity_pct, recovery_hours, id=DISRUPTION_ID)
        stressed = _stressed(base, d, extra_disruptions)
        for key in keys:
            scenario = _apply(stressed, d, None if key == "none" else plans[key])
            if scenario is None:
                continue
            rows.append({"duration_h": duration, "plan": key, **_row(simulate(scenario), outcome)})
    tolerance = {key: _tolerance([r for r in rows if r["plan"] == key]) for key in keys} if outcome else {}
    return {"service": service, "start_hour": start_hour, "outcome": outcome, "rows": rows, "tolerance": tolerance}


def _tolerance(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Longest tested duration up to which the outcome holds at *every* tested duration."""
    ordered = sorted(rows, key=lambda r: r["duration_h"])
    longest, first_breach = None, None
    for r in ordered:
        if r["outcome_achieved"]:
            if first_breach is None:
                longest = r["duration_h"]
        elif first_breach is None:
            first_breach = r["duration_h"]
    achieved = [r["duration_h"] for r in ordered if r["outcome_achieved"]]
    return {
        "longest_tolerated_h": longest,
        "first_breach_h": first_breach,
        "achieved_at": achieved,
        "non_monotone": first_breach is not None and any(h > first_breach for h in achieved),
        "tested": [r["duration_h"] for r in ordered],
    }


def start_profile(base: Scenario, service: str, duration_hours: int, starts: Sequence[int],
                  severity_pct: int = 100, recovery_hours: int = 0,
                  plans: Mapping[str, Plan] | None = None, outcome: str | None = None) -> list[dict[str, Any]]:
    """Metrics for each tested outage start hour (same duration), for ``none`` and each plan."""
    keys = ["none", *(plans or {})]
    rows = []
    for start in sorted(set(int(s) for s in starts if 0 <= int(s) < base.horizon_hours)):
        d = make_disruption(service, start, duration_hours, severity_pct, recovery_hours, id=DISRUPTION_ID)
        stressed = _stressed(base, d)
        for key in keys:
            scenario = _apply(stressed, d, None if key == "none" else plans[key])
            if scenario is None:
                continue
            rows.append({"start_hour": start, "plan": key, **_row(simulate(scenario), outcome)})
    return rows


def impact_surface(base: Scenario, service: str, starts: Sequence[int], durations: Sequence[int],
                   severity_pct: int = 100, metric: str = "overdue_value_hours") -> dict[str, Any]:
    """``metric`` of the stress run (no intervention) on a start-hour x duration grid."""
    starts = sorted(set(int(s) for s in starts if 0 <= int(s) < base.horizon_hours))
    durations = sorted(set(int(x) for x in durations if int(x) >= 1))
    z = []
    for duration in durations:
        row = []
        for start in starts:
            d = make_disruption(service, start, duration, severity_pct, id=DISRUPTION_ID)
            row.append(summarize(simulate(_stressed(base, d)))[metric])
        z.append(row)
    return {"service": service, "metric": metric, "starts": starts, "durations": durations, "z": z}


# --------------------------------------------------------------- robustness

ROBUSTNESS_MATERIALITY = 0.01  # a shortfall up to 1% of the variant's stress value counts as "practically tied"


def _shift(d: Disruption, hours: int, horizon: int) -> Disruption:
    return d.model_copy(update={"start_hour": min(max(d.start_hour + hours, 0), horizon - 1)})


def _scale_duration(d: Disruption, factor: float) -> Disruption:
    return d.model_copy(update={"duration_hours": max(1, round(d.duration_hours * factor)) if d.duration_hours else 0})


def assumption_variants(base: Scenario, disruptions: Sequence[Disruption], shift_hours: int = 3,
                        duration_factor: float = 0.5, balance_pct: int = 25,
                        capacity_pct: int = 25) -> list[tuple[dict[str, str], Scenario, tuple[Disruption, ...]]]:
    """The configured case and one-at-a-time changes to the assumptions most likely to move a ranking.

    Each entry is ``(description, base scenario, disruptions)``. Changes: the other liquidity-recycling
    rule; the other queue order; every outage ``shift_hours`` earlier and later; every outage
    ``duration_factor`` shorter and longer; every opening balance ``balance_pct`` % lower and higher;
    every throughput-limited service ``capacity_pct`` % slower. Variants that coincide with the
    configured case (for example a shift that hits the start of the horizon) are left out.
    """
    horizon = base.horizon_hours
    ds = tuple(disruptions)
    settings = base.settings
    other_recycling = "next_step" if settings.liquidity_recycling == "within_step" else "within_step"
    other_order = "due_id" if settings.ordering == "priority_due_id" else "priority_due_id"
    order_text = {"due_id": "due hour first, ignoring priority", "priority_due_id": "priority first"}
    recycling_text = {"next_step": "received cash usable only from the next hour",
                      "within_step": "received cash usable within the same hour"}

    def scaled_balances(pct: int) -> Scenario:
        parts = tuple(p.model_copy(update={"initial_balance": p.initial_balance * (100 + pct) // 100})
                      for p in base.participants)
        return replace(base, participants=parts)

    def slower(pct: int) -> Scenario:
        services = tuple(s.model_copy(update={"capacity_per_hour": max(1, s.capacity_per_hour * (100 - pct) // 100)})
                         if s.capacity_per_hour else s for s in base.services)
        return replace(base, services=services)

    out: list[tuple[dict[str, str], Scenario, tuple[Disruption, ...]]] = [
        ({"key": "configured", "group": "as configured", "label": "As configured"}, base, ds),
        ({"key": "recycling", "group": "settlement rules", "label": f"Recycling: {recycling_text[other_recycling]}"},
         replace(base, settings=settings.model_copy(update={"liquidity_recycling": other_recycling})), ds),
        ({"key": "ordering", "group": "settlement rules", "label": f"Queue order: {order_text[other_order]}"},
         replace(base, settings=settings.model_copy(update={"ordering": other_order})), ds),
    ]
    seen = {tuple((d.start_hour, d.duration_hours) for d in ds)}
    for sign, word in ((-1, "earlier"), (1, "later")):
        moved = tuple(_shift(d, sign * shift_hours, horizon) for d in ds)
        signature = tuple((d.start_hour, d.duration_hours) for d in moved)
        if signature not in seen:
            seen.add(signature)
            out.append(({"key": f"start_{word}", "group": "timing", "label": f"Outage starts {shift_hours} h {word}"},
                        base, moved))
    for factor, word in ((1 - duration_factor, "shorter"), (1 + duration_factor, "longer")):
        moved = tuple(_scale_duration(d, factor) for d in ds)
        signature = tuple((d.start_hour, d.duration_hours) for d in moved)
        if signature not in seen:
            seen.add(signature)
            out.append(({"key": f"duration_{word}", "group": "duration",
                         "label": f"Outage {round(duration_factor * 100)}% {word}"}, base, moved))
    out.append(({"key": "balances_lower", "group": "liquidity", "label": f"Opening balances {balance_pct}% lower"},
                scaled_balances(-balance_pct), ds))
    out.append(({"key": "balances_higher", "group": "liquidity", "label": f"Opening balances {balance_pct}% higher"},
                scaled_balances(balance_pct), ds))
    if any(s.capacity_per_hour for s in base.services):
        out.append(({"key": "capacity_lower", "group": "capacity",
                     "label": f"Service throughput {capacity_pct}% lower"}, slower(capacity_pct), ds))
    return out


def robustness(base: Scenario, disruptions: Sequence[Disruption], plans: Mapping[str, Plan],
               metric: str = "overdue_value_hours", materiality: float = ROBUSTNESS_MATERIALITY,
               labels: Mapping[str, str] | None = None, **variant_options: Any) -> dict[str, Any]:
    """Does the ranking of interventions survive changes to the assumptions? (No probabilities involved.)

    Every variant from ``assumption_variants`` is simulated without an intervention and with
    each plan (plans follow the moved outage, e.g. a backup's activation delay). For each
    variant and plan the *regret* is how much worse the plan is than the best plan in that
    variant (in the metric's own units; ``regret_share`` expresses it as a share of the
    configured case's stress value so that variants are comparable). Reported per plan: in
    how many variants it is best (exact ties count for every tied plan), best or practically
    tied (regret <= ``materiality`` x the variant's stress value), its largest regret, and the
    range of its reduction. The *minimax-regret* plan is the one whose largest regret is
    smallest. The variants are not weighted: none is treated as more likely than another.
    """
    if not disruptions:
        raise ValueError("robustness needs at least one disruption")
    keys = list(plans)
    rows: list[dict[str, Any]] = []
    for info, variant_base, ds in assumption_variants(base, disruptions, **variant_options):
        stressed = replace(variant_base, disruptions=ds, interventions=())
        stress_value = summarize(simulate(stressed))[metric]
        values: dict[str, int] = {}
        for key in keys:
            scenario = _apply(stressed, ds[0], plans[key])
            if scenario is not None:
                values[key] = summarize(simulate(scenario))[metric]
        best_value = min(values.values()) if values else None
        tolerance = materiality * stress_value
        rows.append({
            **info,
            "disruptions": [d.model_dump(mode="json") for d in ds],
            "stress_value": stress_value,
            "values": values,
            "reduction_pct": {k: ((stress_value - v) / stress_value * 100 if stress_value else None)
                              for k, v in values.items()},
            "best": sorted(k for k, v in values.items() if v == best_value),
            "practically_best": sorted(k for k, v in values.items() if v - best_value <= tolerance),
            "regret": {k: v - best_value for k, v in values.items()},
        })
    reference = rows[0]["stress_value"]
    for r in rows:
        r["regret_share"] = {k: (x / reference if reference else None) for k, x in r["regret"].items()}
    summary: dict[str, dict[str, Any]] = {}
    for key in keys:
        tested = [r for r in rows if key in r["values"]]
        if not tested:
            continue
        worst = max(tested, key=lambda r: r["regret"][key])
        reductions = [r["reduction_pct"][key] for r in tested if r["reduction_pct"][key] is not None]
        summary[key] = {
            "tested": len(tested),
            "wins": sum(1 for r in tested if key in r["best"]),
            "practical_wins": sum(1 for r in tested if key in r["practically_best"]),
            "max_regret": worst["regret"][key],
            "max_regret_share": worst["regret_share"][key],
            "max_regret_variant": worst["key"] if worst["regret"][key] > 0 else None,
            "min_reduction_pct": min(reductions) if reductions else None,
            "max_reduction_pct": max(reductions) if reductions else None,
            "worsens_in": sum(1 for r in tested if r["values"][key] > r["stress_value"]),
        }
    configured = rows[0]
    minimax = min(summary, key=lambda k: (summary[k]["max_regret"], -summary[k]["practical_wins"], k)) if summary else None
    lost = {}
    for r in rows[1:]:
        for key in configured["best"]:
            if key in r["values"] and key not in r["practically_best"]:
                lost.setdefault(key, []).append({"variant": r["key"], "label": r["label"], "winners": r["best"],
                                                 "regret": r["regret"][key], "regret_share": r["regret_share"][key]})
    return {
        "metric": metric,
        "materiality": materiality,
        "reference_stress_value": reference,
        "plans": keys,
        "labels": {k: (labels or {}).get(k, k) for k in keys},
        "variants": rows,
        "summary": summary,
        "configured_best": configured["best"],
        "minimax_regret": minimax,
        "configured_best_beaten_in": lost,
        "robust": bool(configured["best"]) and all(not lost.get(k) for k in configured["best"]),
    }


def plans_from_candidates(scenario: Scenario) -> dict[str, Plan]:
    """Turn a scenario's candidate interventions into plans that follow a moved outage.

    A backup keeps its activation delay relative to the first disruption's start, and a
    faster recovery targets the first disruption; the other types are used as they are.
    """
    first = scenario.disruptions[0] if scenario.disruptions else None
    plans: dict[str, Plan] = {}
    for iv in scenario.candidate_interventions:
        if iv.type == "backup_route" and first is not None:
            delay = max(0, iv.active_from_hour - first.start_hour)
            plans[iv.id] = (lambda s, d, iv=iv, delay=delay:
                            iv.model_copy(update={"active_from_hour": min(d.start_hour + delay, s.horizon_hours - 1)}))
        elif iv.type == "faster_recovery":
            plans[iv.id] = lambda s, d, iv=iv: iv.model_copy(update={"disruption": d.id})
        else:
            plans[iv.id] = lambda s, d, iv=iv: iv
    return plans
