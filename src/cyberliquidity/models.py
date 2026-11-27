"""Validated scenario data model for CyberLiquidity (Pydantic v2).

Conventions used throughout the package
---------------------------------------
* Money is integer *minor* currency units (e.g. cents). There are no floats and no FX.
* Time is integer hours. Hour ``h`` is the interval ``[h, h+1)`` after the scenario
  start; hour 0 is the first hour and ``horizon_hours - 1`` the last one simulated.
* Capacity factors are integer percentages (0-100), so capacity arithmetic is exact.
* Services, routes, participants and external parties share one identifier
  namespace. Obligations, disruptions and interventions each have their own.

Two networks are described here and kept separate:

* the *operational* network: ``services`` (with dependency edges) and ``routes``
  (ordered paths of services that process a payment);
* the *financial* network: funded ``participants`` (inside the accounting boundary),
  ``external_parties`` (outside it) and ``obligations`` between them.

They are coupled only through ``Obligation.routes``.
"""

from __future__ import annotations

from collections import Counter
from enum import Enum
from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .graphs import find_cycle

SCHEMA_VERSION = "1.0"
DISCLAIMER = (
    "Synthetic scenario. Results are conditional on model assumptions and are not "
    "forecasts of real incidents."
)
_MAX_REPORTED_ERRORS = 40

Id = Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.:\-]+$")]


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# --------------------------------------------------------------------------- money


class Currency(_Frozen):
    """Single configurable currency. The code is a label only; there is no FX model."""

    code: str = Field(pattern=r"^[A-Z]{3}$")
    minor_units: int = Field(ge=0, le=4, description="Decimal places of the minor unit (2 for cents).")

    def format(self, minor: int, with_code: bool = True) -> str:
        return format_money(minor, self, with_code=with_code)

    def to_major(self, minor: int) -> float:
        """Convert to major units for display/plotting only (never used in the engine)."""
        return minor / (10**self.minor_units)

    def to_minor(self, major: float | int) -> int:
        """Convert a user-entered major amount to integer minor units (rounded half up)."""
        scaled = round(float(major) * (10**self.minor_units))
        return int(scaled)


def format_money(minor: int, currency: Currency, with_code: bool = True) -> str:
    sign = "-" if minor < 0 else ""
    whole, frac = divmod(abs(int(minor)), 10**currency.minor_units)
    text = f"{whole:,}"
    if currency.minor_units:
        text += f".{frac:0{currency.minor_units}d}"
    return f"{sign}{text} {currency.code}" if with_code else f"{sign}{text}"


# ------------------------------------------------------------- operational network


class ServiceKind(str, Enum):
    CLOUD = "cloud"
    IDENTITY = "identity"
    PAYMENT_PROVIDER = "payment_provider"
    SETTLEMENT = "settlement"
    CHANNEL = "channel"
    OTHER = "other"


class Service(_Frozen):
    """An operational service.

    Dependency edges point from this service to the services it depends on
    (``this -> dependency``). Meaning:

    * ``requires_all``: every listed service must be operational (logical AND);
    * ``requires_any``: each group is a set of interchangeable alternatives, at
      least one of which must be operational (failover, logical OR).

    A failure therefore propagates *against* the edge direction, from a dependency
    to its dependents.
    """

    id: Id
    name: str
    kind: ServiceKind
    capacity_per_hour: int | None = Field(
        default=None,
        ge=1,
        description="Payments the service can process per hour, shared by all users; null = not throughput-limited.",
    )
    requires_all: tuple[Id, ...] = ()
    requires_any: tuple[tuple[Id, ...], ...] = ()
    description: str = ""

    def dependency_groups(self) -> tuple[tuple[str, ...], ...]:
        """Dependencies in conjunctive form: every group is required, any member of a group suffices."""
        return tuple((dep,) for dep in self.requires_all) + tuple(self.requires_any)

    def dependency_ids(self) -> tuple[str, ...]:
        seen: dict[str, None] = {}
        for group in self.dependency_groups():
            for dep in group:
                seen.setdefault(dep, None)
        return tuple(seen)


class Route(_Frozen):
    """An ordered path of services that must all process a payment.

    Settling one payment through a route consumes one unit of hourly capacity at
    every capacity-limited service on the path.
    """

    id: Id
    name: str
    path: tuple[Id, ...] = Field(min_length=1)
    description: str = ""


# --------------------------------------------------------------- financial network


class Participant(_Frozen):
    """A funded participant inside the accounting boundary (has a cash account)."""

    id: Id
    name: str
    sector: str = ""
    initial_balance: int = Field(ge=0, description="Opening cash in minor units.")
    description: str = ""


class ExternalParty(_Frozen):
    """A counterparty outside the accounting boundary (no modelled cash constraint)."""

    id: Id
    name: str
    description: str = ""


class Obligation(_Frozen):
    """A payment obligation. All obligations are pending at the start of a run.

    ``routes`` lists the eligible payment routes in preference order. The settlement
    status is an *output* of the simulation (see ``engine.ObligationOutcome``).
    """

    id: Id
    payer: Id
    recipient: Id
    amount: int = Field(gt=0, description="Minor units.")
    due_hour: int = Field(ge=0, description="On time if settled by the end of this hour.")
    priority: int = Field(default=3, ge=1, le=9, description="1 = most urgent.")
    routes: tuple[Id, ...] = Field(min_length=1)
    purpose: str = ""


# ------------------------------------------------------------ disruptions and fixes


class Disruption(_Frozen):
    """A service disruption.

    Outage phase: hours ``[start_hour, start_hour + duration_hours)`` at
    ``100 - severity_pct`` percent capacity (0 percent, i.e. unavailable, when
    ``severity_pct`` is 100). Optional recovery phase: the following
    ``recovery_hours`` hours at ``recovery_capacity_pct`` percent. Afterwards the
    service is fully available again.
    """

    id: Id
    service: Id
    start_hour: int = Field(ge=0)
    duration_hours: int = Field(ge=0)
    severity_pct: int = Field(default=100, ge=1, le=100, description="Share of capacity lost during the outage phase.")
    recovery_hours: int = Field(default=0, ge=0)
    recovery_capacity_pct: int = Field(default=50, ge=1, le=99)
    description: str = ""

    @property
    def end_hour(self) -> int:
        """First hour after the outage phase."""
        return self.start_hour + self.duration_hours

    @property
    def restored_hour(self) -> int:
        """First hour at full capacity again."""
        return self.end_hour + self.recovery_hours

    def phase_at(self, hour: int) -> tuple[int, str] | None:
        """``(capacity_pct, phase)`` if the disruption affects ``hour``, else ``None``."""
        if self.start_hour <= hour < self.end_hour:
            return 100 - self.severity_pct, ("outage" if self.severity_pct == 100 else "degradation")
        if self.recovery_hours and self.end_hour <= hour < self.restored_hour:
            return self.recovery_capacity_pct, "recovery"
        return None


class BackupRouteIntervention(_Frozen):
    """Independent backup payment provider: an extra eligible route after an activation delay.

    From ``active_from_hour`` every obligation that lists one of ``primary_routes``
    (optionally only for ``payers``) may also settle through ``backup_route``. The
    primary route stays preferred whenever it is usable. The backup has its own
    dependencies and capacity, shared with its existing users; it is *not* assumed
    independent of the failure - that follows from the dependency graph.
    """

    type: Literal["backup_route"] = "backup_route"
    id: Id
    primary_routes: tuple[Id, ...] = Field(min_length=1)
    backup_route: Id
    active_from_hour: int = Field(ge=0)
    payers: tuple[Id, ...] | None = None
    description: str = ""


class BufferAllocation(_Frozen):
    participant: Id
    amount: int = Field(gt=0)


class LiquidityBufferIntervention(_Frozen):
    """Finite liquidity buffer: explicit funding transfers into selected participants.

    Each allocation is recorded as an ``intervention_funding`` ledger entry from an
    account outside the accounting boundary at ``funding_hour``. It creates no
    unexplained cash and does not touch the operational network.
    """

    type: Literal["liquidity_buffer"] = "liquidity_buffer"
    id: Id
    allocations: tuple[BufferAllocation, ...] = Field(min_length=1)
    funding_hour: int = Field(default=0, ge=0)
    funder: str = "External liquidity provider (outside the accounting boundary)"
    description: str = ""

    @property
    def total(self) -> int:
        return sum(a.amount for a in self.allocations)


class FasterRecoveryIntervention(_Frozen):
    """Faster service recovery: shorten one disruption's outage phase, nothing else."""

    type: Literal["faster_recovery"] = "faster_recovery"
    id: Id
    disruption: Id
    duration_reduction_hours: int = Field(gt=0)
    description: str = ""


class CreditFacility(_Frozen):
    participant: Id
    limit: int = Field(gt=0, description="Maximum outstanding drawn amount in minor units.")


class CreditLineIntervention(_Frozen):
    """Committed intraday credit line, drawn only when a payment would otherwise fail for lack of cash.

    From ``available_from_hour`` a payment whose route is usable but whose payer is short
    of cash draws exactly the shortfall (if the undrawn limit covers it). At the end of
    every hour, the participant's cash repays outstanding draws first. Every draw and
    repayment is an explicit ledger entry with an account outside the accounting
    boundary. Draws and repayments are book entries and need no payment route.
    """

    type: Literal["credit_line"] = "credit_line"
    id: Id
    facilities: tuple[CreditFacility, ...] = Field(min_length=1)
    available_from_hour: int = Field(default=0, ge=0)
    provider: str = "Credit provider (outside the accounting boundary)"
    description: str = ""

    @property
    def total_limit(self) -> int:
        return sum(f.limit for f in self.facilities)


class OffsettingIntervention(_Frozen):
    """Gridlock resolution: hourly multilateral offsetting of queued payments (a liquidity-saving mechanism).

    From ``active_from_hour``, after the normal settlement loop of each hour, the payment
    system looks for a set of queued payments that can settle *simultaneously*. Every
    payment in the set needs a usable route with spare capacity, and every participant's
    net outflow over the set must be covered by its available cash plus undrawn committed
    credit. Starting from all candidates in queue order, the rule repeatedly removes the
    last payment (in queue order) of the participant with the largest uncovered net
    position until the set is covered; the remaining payments settle together. Each
    obligation is discharged in full, but cash moves net, through a clearing account
    outside the accounting boundary that is empty again after every batch. The search is
    repeated (after the normal loop) until nothing more settles in the hour.
    """

    type: Literal["offsetting"] = "offsetting"
    id: Id
    active_from_hour: int = Field(default=0, ge=0)
    operator: str = "Payment system operator (clearing account outside the accounting boundary)"
    description: str = ""


class PriorityReserveIntervention(_Frozen):
    """Treasury rule: hold back routine payments to keep cash for urgent ones (liquidity reservation).

    From ``active_from_hour`` each covered participant releases a payment whose priority
    is *worse* than ``protected_priority`` only if, after paying it, its available cash
    plus undrawn committed credit still covers every unsettled payment of priority
    ``protected_priority`` or better that falls due before ``hour + lookahead_hours``
    (overdue ones included). Protected payments are never held back by the rule. It uses
    only the participant's own known payment calendar and changes nothing else.
    ``participants = None`` applies the rule to every participant.
    """

    type: Literal["priority_reserve"] = "priority_reserve"
    id: Id
    participants: tuple[Id, ...] | None = None
    protected_priority: int = Field(default=1, ge=1, le=8, description="Payments with priority <= this are protected.")
    lookahead_hours: int = Field(default=24, ge=1, description="How far ahead protected payments are reserved for.")
    active_from_hour: int = Field(default=0, ge=0)
    description: str = ""


Intervention = Annotated[
    Union[BackupRouteIntervention, LiquidityBufferIntervention, FasterRecoveryIntervention, CreditLineIntervention,
          OffsettingIntervention, PriorityReserveIntervention],
    Field(discriminator="type"),
]


class SimulationSettings(_Frozen):
    """Switchable engine assumptions (defaults are the documented baseline rules).

    * ``ordering``: queue order. ``priority_due_id`` = (priority, due hour, id);
      ``due_id`` = (due hour, id), i.e. FIFO ignoring priority.
    * ``liquidity_recycling``: ``within_step`` lets cash received in hour h fund
      further payments in hour h; ``next_step`` makes it usable from hour h+1.
    """

    ordering: Literal["priority_due_id", "due_id"] = "priority_due_id"
    liquidity_recycling: Literal["within_step", "next_step"] = "within_step"


# --------------------------------------------------------------------- the scenario


class Scenario(_Frozen):
    """A complete, self-contained run configuration.

    ``disruptions`` and ``interventions`` are applied when the scenario is simulated.
    ``candidate_interventions`` are presets used by comparisons (the app and the
    ``compare`` command); the engine ignores them.
    """

    schema_version: Literal["1.0"] = SCHEMA_VERSION
    id: Id
    name: str
    description: str = ""
    synthetic: bool = True
    disclaimer: str = DISCLAIMER
    currency: Currency
    horizon_hours: int = Field(ge=1, le=24 * 31)
    services: tuple[Service, ...] = Field(min_length=1)
    routes: tuple[Route, ...] = Field(min_length=1)
    participants: tuple[Participant, ...] = Field(min_length=1)
    external_parties: tuple[ExternalParty, ...] = ()
    obligations: tuple[Obligation, ...] = ()
    disruptions: tuple[Disruption, ...] = ()
    interventions: tuple[Intervention, ...] = ()
    candidate_interventions: tuple[Intervention, ...] = ()
    settings: SimulationSettings = SimulationSettings()

    @model_validator(mode="after")
    def _validate(self) -> Scenario:
        errors = scenario_errors(self)
        if errors:
            shown = errors[:_MAX_REPORTED_ERRORS]
            more = len(errors) - len(shown)
            text = "Invalid scenario:\n- " + "\n- ".join(shown)
            if more:
                text += f"\n- ... and {more} more"
            raise ValueError(text)
        return self

    # Convenience lookups (cheap to rebuild; models are immutable).
    def services_by_id(self) -> dict[str, Service]:
        return {s.id: s for s in self.services}

    def routes_by_id(self) -> dict[str, Route]:
        return {r.id: r for r in self.routes}

    def participants_by_id(self) -> dict[str, Participant]:
        return {p.id: p for p in self.participants}

    def obligations_by_id(self) -> dict[str, Obligation]:
        return {o.id: o for o in self.obligations}

    def internal_ids(self) -> frozenset[str]:
        return frozenset(p.id for p in self.participants)

    def names(self) -> dict[str, str]:
        """Display names for every service, route, participant and external party."""
        out: dict[str, str] = {}
        for collection in (self.services, self.routes, self.participants, self.external_parties):
            for item in collection:
                out[item.id] = item.name
        return out


def _duplicates(ids: list[str]) -> list[str]:
    return sorted(i for i, n in Counter(ids).items() if n > 1)


def scenario_errors(s: Scenario) -> list[str]:
    """All referential and structural problems in a scenario (empty list = valid)."""
    errors: list[str] = []
    horizon = s.horizon_hours

    node_ids = [x.id for x in s.services] + [x.id for x in s.routes]
    node_ids += [x.id for x in s.participants] + [x.id for x in s.external_parties]
    for dup in _duplicates(node_ids):
        errors.append(f"Duplicate id '{dup}': services, routes, participants and external parties share one id namespace.")
    for dup in _duplicates([o.id for o in s.obligations]):
        errors.append(f"Duplicate obligation id '{dup}'.")
    for dup in _duplicates([d.id for d in s.disruptions]):
        errors.append(f"Duplicate disruption id '{dup}'.")
    for label, group in (("intervention", s.interventions), ("candidate intervention", s.candidate_interventions)):
        for dup in _duplicates([i.id for i in group]):
            errors.append(f"Duplicate {label} id '{dup}'.")

    services = {x.id for x in s.services}
    routes = {x.id for x in s.routes}
    participants = {x.id for x in s.participants}
    externals = {x.id for x in s.external_parties}
    parties = participants | externals

    for svc in s.services:
        for dep in svc.requires_all:
            if dep == svc.id:
                errors.append(f"Service '{svc.id}' cannot depend on itself.")
            elif dep not in services:
                errors.append(f"Service '{svc.id}' requires unknown service '{dep}'.")
        for dup in _duplicates(list(svc.requires_all)):
            errors.append(f"Service '{svc.id}' lists '{dup}' twice in requires_all.")
        for group in svc.requires_any:
            if len(group) < 2:
                errors.append(
                    f"Service '{svc.id}': a requires_any group needs at least two alternatives "
                    f"(use requires_all for a single required dependency): {list(group)}."
                )
            for dup in _duplicates(list(group)):
                errors.append(f"Service '{svc.id}' lists '{dup}' twice in one requires_any group.")
            for dep in group:
                if dep == svc.id:
                    errors.append(f"Service '{svc.id}' cannot depend on itself.")
                elif dep not in services:
                    errors.append(f"Service '{svc.id}' has unknown alternative dependency '{dep}'.")

    cycle = find_cycle({svc.id: svc.dependency_ids() for svc in s.services})
    if cycle:
        errors.append(
            "Operational dependency cycle: " + " -> ".join(cycle)
            + ". Version 1 rejects dependency cycles (see docs/MODEL.md, 'Simplifications')."
        )

    for route in s.routes:
        for sid in route.path:
            if sid not in services:
                errors.append(f"Route '{route.id}' uses unknown service '{sid}'.")
        for dup in _duplicates(list(route.path)):
            errors.append(f"Route '{route.id}' lists service '{dup}' twice.")

    for ob in s.obligations:
        if ob.payer not in parties:
            errors.append(f"Obligation '{ob.id}': unknown payer '{ob.payer}'.")
        if ob.recipient not in parties:
            errors.append(f"Obligation '{ob.id}': unknown recipient '{ob.recipient}'.")
        if ob.payer == ob.recipient:
            errors.append(f"Obligation '{ob.id}': payer and recipient are the same ('{ob.payer}').")
        if ob.payer in externals and ob.recipient in externals:
            errors.append(f"Obligation '{ob.id}': both parties are outside the accounting boundary.")
        if ob.due_hour >= horizon:
            errors.append(f"Obligation '{ob.id}': due_hour {ob.due_hour} is outside the horizon (0..{horizon - 1}).")
        for rid in ob.routes:
            if rid not in routes:
                errors.append(f"Obligation '{ob.id}': unknown route '{rid}'.")
        for dup in _duplicates(list(ob.routes)):
            errors.append(f"Obligation '{ob.id}' lists route '{dup}' twice.")

    disruption_ids = {d.id for d in s.disruptions}
    for d in s.disruptions:
        if d.service not in services:
            errors.append(f"Disruption '{d.id}': unknown service '{d.service}'.")
        if d.start_hour >= horizon:
            errors.append(f"Disruption '{d.id}': start_hour {d.start_hour} is outside the horizon (0..{horizon - 1}).")

    for label, group in (("Intervention", s.interventions), ("Candidate intervention", s.candidate_interventions)):
        recovery_targets: Counter[str] = Counter()
        credit_holders: Counter[str] = Counter()
        reserve_holders: Counter[str] = Counter()
        offsetting_count = 0
        for iv in group:
            if isinstance(iv, BackupRouteIntervention):
                for rid in iv.primary_routes:
                    if rid not in routes:
                        errors.append(f"{label} '{iv.id}': unknown primary route '{rid}'.")
                if iv.backup_route not in routes:
                    errors.append(f"{label} '{iv.id}': unknown backup route '{iv.backup_route}'.")
                if iv.backup_route in iv.primary_routes:
                    errors.append(f"{label} '{iv.id}': the backup route is also listed as a primary route.")
                if iv.active_from_hour >= horizon:
                    errors.append(f"{label} '{iv.id}': active_from_hour {iv.active_from_hour} is outside the horizon.")
                for pid in iv.payers or ():
                    if pid not in parties:
                        errors.append(f"{label} '{iv.id}': unknown payer '{pid}'.")
            elif isinstance(iv, LiquidityBufferIntervention):
                for alloc in iv.allocations:
                    if alloc.participant not in participants:
                        errors.append(
                            f"{label} '{iv.id}': '{alloc.participant}' is not a participant inside the accounting boundary."
                        )
                for dup in _duplicates([a.participant for a in iv.allocations]):
                    errors.append(f"{label} '{iv.id}': participant '{dup}' is allocated twice.")
                if iv.funding_hour >= horizon:
                    errors.append(f"{label} '{iv.id}': funding_hour {iv.funding_hour} is outside the horizon.")
            elif isinstance(iv, FasterRecoveryIntervention):
                recovery_targets[iv.disruption] += 1
                # Candidates may target a disruption that is configured later (e.g. in the app),
                # so only applied interventions must reference an existing disruption.
                if label == "Intervention" and iv.disruption not in disruption_ids:
                    errors.append(f"{label} '{iv.id}': unknown disruption '{iv.disruption}'.")
            elif isinstance(iv, CreditLineIntervention):
                for facility in iv.facilities:
                    if facility.participant not in participants:
                        errors.append(
                            f"{label} '{iv.id}': '{facility.participant}' is not a participant inside the accounting boundary."
                        )
                for holder in {f.participant for f in iv.facilities}:
                    credit_holders[holder] += 1
                for dup in _duplicates([f.participant for f in iv.facilities]):
                    errors.append(f"{label} '{iv.id}': participant '{dup}' has two facilities.")
                if iv.available_from_hour >= horizon:
                    errors.append(f"{label} '{iv.id}': available_from_hour {iv.available_from_hour} is outside the horizon.")
            elif isinstance(iv, OffsettingIntervention):
                offsetting_count += 1
                if iv.active_from_hour >= horizon:
                    errors.append(f"{label} '{iv.id}': active_from_hour {iv.active_from_hour} is outside the horizon.")
            elif isinstance(iv, PriorityReserveIntervention):
                covered = list(iv.participants) if iv.participants is not None else sorted(participants)
                for pid in covered:
                    if pid not in participants:
                        errors.append(f"{label} '{iv.id}': '{pid}' is not a participant inside the accounting boundary.")
                for dup in _duplicates(covered):
                    errors.append(f"{label} '{iv.id}': participant '{dup}' is listed twice.")
                if iv.participants is not None and not iv.participants:
                    errors.append(f"{label} '{iv.id}': the participant list is empty (use null for every participant).")
                for holder in set(covered):
                    reserve_holders[holder] += 1
                if iv.active_from_hour >= horizon:
                    errors.append(f"{label} '{iv.id}': active_from_hour {iv.active_from_hour} is outside the horizon.")
        for target, count in recovery_targets.items():
            if count > 1:
                errors.append(f"{label}s: more than one faster_recovery for disruption '{target}'.")
        if label == "Intervention":
            # Applied credit lines and reserve rules must not overlap; candidates are alternatives and may.
            for holder, count in sorted(credit_holders.items()):
                if count > 1:
                    errors.append(f"Interventions: participant '{holder}' is covered by more than one applied credit line.")
            for holder, count in sorted(reserve_holders.items()):
                if count > 1:
                    errors.append(f"Interventions: participant '{holder}' is covered by more than one applied reserve rule.")
            if offsetting_count > 1:
                errors.append("Interventions: at most one offsetting (gridlock resolution) intervention can be applied.")
    return errors
