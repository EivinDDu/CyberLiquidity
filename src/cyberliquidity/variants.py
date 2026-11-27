"""Scenario variants: baseline, stress, interventions, and the liquidity-buffer sweep.

Every helper returns a *new, re-validated* ``Scenario``; nothing is mutated.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .models import (
    BackupRouteIntervention,
    BufferAllocation,
    CreditFacility,
    CreditLineIntervention,
    Disruption,
    FasterRecoveryIntervention,
    Intervention,
    LiquidityBufferIntervention,
    OffsettingIntervention,
    PriorityReserveIntervention,
    Scenario,
)
from .operational import OperationalModel


def replace(scenario: Scenario, **updates: Any) -> Scenario:
    """Copy a scenario with some fields replaced, running full validation again."""
    fields = {name: getattr(scenario, name) for name in Scenario.model_fields}
    fields.update(updates)
    return Scenario.model_validate(fields)


def baseline(scenario: Scenario) -> Scenario:
    """A: no disruption and no intervention."""
    return replace(scenario, disruptions=(), interventions=())


def stress(scenario: Scenario, disruption: Disruption | None = None) -> Scenario:
    """B: the scenario's disruptions (or the given one) with no intervention."""
    disruptions = (disruption,) if disruption is not None else scenario.disruptions
    return replace(scenario, disruptions=disruptions, interventions=())


def with_intervention(scenario: Scenario, *interventions: Intervention) -> Scenario:
    """C: add interventions to whatever the scenario already applies."""
    return replace(scenario, interventions=tuple(scenario.interventions) + tuple(interventions))


def with_settings(scenario: Scenario, **settings: Any) -> Scenario:
    return replace(scenario, settings=scenario.settings.model_copy(update=settings))


def make_disruption(service: str, start_hour: int, duration_hours: int, severity_pct: int = 100,
                    recovery_hours: int = 0, recovery_capacity_pct: int = 50, id: str = "D1",
                    description: str = "") -> Disruption:
    return Disruption(id=id, service=service, start_hour=start_hour, duration_hours=duration_hours,
                      severity_pct=severity_pct, recovery_hours=recovery_hours,
                      recovery_capacity_pct=recovery_capacity_pct, description=description)


def make_backup(primary_routes: list[str] | tuple[str, ...], backup_route: str, active_from_hour: int,
                payers: list[str] | None = None, id: str = "backup") -> BackupRouteIntervention:
    return BackupRouteIntervention(id=id, primary_routes=tuple(primary_routes), backup_route=backup_route,
                                   active_from_hour=active_from_hour,
                                   payers=tuple(payers) if payers is not None else None)


def make_buffer(participants: list[str] | tuple[str, ...], amount_each: int, funding_hour: int = 0,
                id: str = "buffer") -> LiquidityBufferIntervention:
    return LiquidityBufferIntervention(
        id=id,
        allocations=tuple(BufferAllocation(participant=p, amount=amount_each) for p in participants),
        funding_hour=funding_hour,
    )


def make_faster_recovery(disruption_id: str, reduction_hours: int, id: str = "faster_recovery") -> FasterRecoveryIntervention:
    return FasterRecoveryIntervention(id=id, disruption=disruption_id, duration_reduction_hours=reduction_hours)


def make_credit_line(participants: list[str] | tuple[str, ...] | dict[str, int], limit_each: int | None = None,
                     available_from_hour: int = 0, id: str = "credit_line", provider: str | None = None,
                     description: str = "") -> CreditLineIntervention:
    """Committed credit line: equal limits for a list of participants, or explicit ``{participant: limit}``."""
    if isinstance(participants, dict):
        limits = dict(participants)
    else:
        if limit_each is None:
            raise ValueError("limit_each is required when participants is a list")
        limits = dict.fromkeys(participants, limit_each)
    extra = {"provider": provider} if provider else {}
    return CreditLineIntervention(
        id=id,
        facilities=tuple(CreditFacility(participant=p, limit=v) for p, v in limits.items() if v > 0),
        available_from_hour=available_from_hour,
        description=description,
        **extra,
    )


def make_offsetting(active_from_hour: int = 0, id: str = "offsetting", description: str = "") -> OffsettingIntervention:
    """Gridlock resolution: hourly multilateral offsetting of queued payments from ``active_from_hour``."""
    return OffsettingIntervention(id=id, active_from_hour=active_from_hour, description=description)


def make_reserve(participants: list[str] | tuple[str, ...] | None = None, protected_priority: int = 1,
                 lookahead_hours: int = 24, active_from_hour: int = 0, id: str = "reserve",
                 description: str = "") -> PriorityReserveIntervention:
    """Treasury rule: hold back routine payments to keep cash for protected ones (``None`` = every participant)."""
    return PriorityReserveIntervention(
        id=id, participants=tuple(participants) if participants is not None else None,
        protected_priority=protected_priority, lookahead_hours=lookahead_hours, active_from_hour=active_from_hour,
        description=description,
    )


def outgoing_totals(scenario: Scenario) -> dict[str, int]:
    """Total value of each participant's outgoing obligations (an upper bound on any cash it can need)."""
    internal = scenario.internal_ids()
    totals: dict[str, int] = {}
    for ob in scenario.obligations:
        if ob.payer in internal:
            totals[ob.payer] = totals.get(ob.payer, 0) + ob.amount
    return dict(sorted(totals.items()))


def cash_unconstrained(scenario: Scenario, id: str = "cash_reference") -> Scenario:
    """Reference variant in which cash can never bind.

    Every participant with outgoing obligations is pre-funded at hour 0 with the total
    value of those obligations (explicitly recorded intervention funding). Whatever is
    still delayed in this variant cannot be fixed by liquidity: it is route-limited.
    """
    outgoing = outgoing_totals(scenario)
    if not outgoing:
        return scenario
    buffer = LiquidityBufferIntervention(
        id=id,
        allocations=tuple(BufferAllocation(participant=p, amount=v) for p, v in sorted(outgoing.items())),
        funding_hour=0,
        funder="Reference funding (diagnostic: makes cash non-binding)",
        description="Diagnostic only: every payer pre-funded with all of its outgoing obligations.",
    )
    return with_intervention(scenario, buffer)


@dataclass(frozen=True)
class BackupSuggestion:
    primary_routes: tuple[str, ...]
    backup_route: str | None
    independent: bool
    common_mode_services: tuple[str, ...]
    note: str


def suggest_backup(scenario: Scenario, disruption: Disruption) -> BackupSuggestion:
    """Pick default backup settings for a disruption from the static dependency graph.

    Primary routes = routes used by obligations that the disrupted service makes
    unusable. Backup = an unaffected route with the largest nominal throughput; if
    every route is affected, an affected route is returned and flagged as not
    independent (this is a modelling default, not a recommendation).
    """
    op = OperationalModel(scenario)
    footprint = op.failure_footprint(disruption.service, disruption.severity_pct)
    used = {rid for ob in scenario.obligations for rid in ob.routes}
    affected = [rid for rid in footprint["unavailable_routes"] if rid in used]
    if not affected:
        affected = [rid for rid in footprint["degraded_routes"] if rid in used]
    others = [r.id for r in scenario.routes if r.id not in affected]
    if not affected:
        return BackupSuggestion((), None, True, (), "The disrupted service does not affect any payment route.")
    unaffected = [rid for rid in others if rid not in footprint["unavailable_routes"]]

    def throughput(rid: str) -> int:
        cap = op.route_nominal_capacity(rid)
        return cap if cap is not None else 10**9

    if unaffected:
        backup = sorted(unaffected, key=lambda rid: (-throughput(rid), rid))[0]
        common = tuple(sorted({s for p in affected for s in op.common_mode_failures(p, backup)}))
        return BackupSuggestion(tuple(affected), backup, disruption.service not in common, common,
                                "Backup route is not disabled by the disrupted service.")
    candidates = [rid for rid in (others or [r.id for r in scenario.routes]) if rid not in affected] or affected[1:]
    backup = candidates[0] if candidates else None
    common = tuple(sorted({s for p in affected for s in (op.common_mode_failures(p, backup) if backup else [])}))
    return BackupSuggestion(tuple(affected[:1] if backup in affected else affected), backup, False, common,
                            "Every route depends on the disrupted service: no independent backup exists in this network.")
