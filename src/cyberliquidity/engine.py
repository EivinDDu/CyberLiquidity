"""Deterministic, discrete-time (hourly) settlement engine.

``simulate(scenario)`` is a pure function of the scenario: identical inputs give
identical events, ledger and outcomes. It never imports any interface code.

Step order for every hour ``h`` (docs/MODEL.md section 4):

1. Evaluate every service's effective state and hourly capacity. Capacity is one
   shared pool per service for all users.
2. Apply intervention funding scheduled for ``h`` (an explicit external inflow).
3. Add obligations due in ``h`` to the queue.
4. Settlement loop: scan the queue in the configured order and settle the *first*
   obligation that has a usable route and a payer who can fund it in full; then
   rescan from the top. Stop when a full scan settles nothing. Cash received earlier
   in the loop can fund later payments in the same hour (within-step recycling).
   If the payer holds a committed credit line, a payment that is short of cash only
   draws exactly the shortfall (explicit ledger entry) and settles. A payer under a
   priority-reserve rule releases a routine payment only if the cash it keeps still
   covers its protected payments due within the look-ahead window.
4b. Offsetting (only with the gridlock-resolution intervention): settle a covered set
   of queued payments simultaneously, with net cash movements through a clearing
   account, then run the settlement loop again; repeat until nothing more settles.
5. For every obligation still queued (all of them are now overdue), record *every*
   reason it could not settle.
6. Credit sweep: each participant's end-of-hour cash repays its outstanding draws.
7. Snapshot balances, overdue obligations and service usage, and check invariants.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from enum import Enum
from functools import cached_property
from operator import attrgetter
from typing import Any

from .models import (
    BackupRouteIntervention,
    CreditLineIntervention,
    Disruption,
    FasterRecoveryIntervention,
    LiquidityBufferIntervention,
    Obligation,
    OffsettingIntervention,
    PriorityReserveIntervention,
    Scenario,
)
from .operational import OperationalModel, ServiceState, ServiceStatus

ENGINE_VERSION = "0.4.0"


class InvariantViolation(AssertionError):
    """Raised if an accounting or operational invariant is broken (indicates an engine bug)."""


class Reason(str, Enum):
    SERVICE_UNAVAILABLE = "SERVICE_UNAVAILABLE"
    CAPACITY_EXHAUSTED = "CAPACITY_EXHAUSTED"
    ROUTE_NOT_YET_ACTIVE = "ROUTE_NOT_YET_ACTIVE"
    INSUFFICIENT_CASH = "INSUFFICIENT_CASH"


OPERATIONAL_REASONS = frozenset(
    {Reason.SERVICE_UNAVAILABLE.value, Reason.CAPACITY_EXHAUSTED.value, Reason.ROUTE_NOT_YET_ACTIVE.value}
)
CASH_REASON = Reason.INSUFFICIENT_CASH.value

# Mutually exclusive categories for one obligation in one blocked hour.
CATEGORIES = ("operational_only", "operational_and_cash", "cash_awaiting_inflows", "cash_other")

_AMOUNT = attrgetter("amount")


class Status(str, Enum):
    SETTLED_ON_TIME = "settled_on_time"
    SETTLED_LATE = "settled_late"
    UNSETTLED_AT_HORIZON = "unsettled_at_horizon"


class Mechanism(str, Enum):
    """How a delayed obligation was held up over its blocked hours (docs/MODEL.md 6.3)."""

    NONE = "none"
    DIRECT_OPERATIONAL = "direct_operational"
    FINANCIAL_PROPAGATION = "financial_propagation"
    LIQUIDITY_OTHER = "liquidity_shortfall_other"
    MIXED = "mixed"


# ----------------------------------------------------------------- result types


@dataclass(frozen=True)
class Event:
    event_id: int
    hour: int
    type: str
    obligation_id: str | None = None
    service_id: str | None = None
    participant_id: str | None = None
    amount: int | None = None
    data: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class LedgerEntry:
    """One cash movement. The debit account loses ``amount`` and the credit account gains it.

    Accounts outside the accounting boundary have no tracked balance
    (``*_balance_after`` is ``None`` for them).
    """

    entry_id: int
    hour: int
    event_id: int
    # internal_transfer | external_inflow | external_outflow | intervention_funding | credit_draw | credit_repayment
    # | offset_debit | offset_credit (net movements into and out of an offsetting batch's clearing account)
    kind: str
    reference: str  # obligation id or intervention id
    debit_account: str
    credit_account: str
    amount: int
    debit_balance_after: int | None
    credit_balance_after: int | None


@dataclass(frozen=True)
class BlockRecord:
    """Why one queued obligation did not settle in one hour (all applicable reasons)."""

    hour: int
    obligation_id: str
    payer: str
    amount: int
    reasons: tuple[str, ...]
    route_checks: tuple[dict[str, Any], ...]
    payer_balance: int | None
    available_cash: int | None
    shortfall: int | None
    overdue_inflows: tuple[tuple[str, int], ...]  # payer's overdue incoming obligations (id, amount)
    event_id: int
    available_credit: int | None = None  # undrawn committed credit (only for payers with a facility)
    reserved: int | None = None  # cash kept for protected payments (only for routine payments under a reserve rule)

    # Derived values are computed once per record (the record is immutable) and cached on
    # the instance; they are not dataclass fields, so equality and asdict() are unchanged.
    @cached_property
    def operational(self) -> bool:
        return any(r in OPERATIONAL_REASONS for r in self.reasons)

    @cached_property
    def cash(self) -> bool:
        return CASH_REASON in self.reasons

    @cached_property
    def held_by_reserve(self) -> bool:
        """Cash-blocked only because of the reserve rule: without it the payer could have funded the payment."""
        if not self.cash or not self.reserved:
            return False
        return (self.available_cash or 0) + (self.available_credit or 0) >= self.amount

    @cached_property
    def overdue_inflow_value(self) -> int:
        return sum(amount for _, amount in self.overdue_inflows)

    @cached_property
    def category(self) -> str:
        if self.operational and self.cash:
            return "operational_and_cash"
        if self.operational:
            return "operational_only"
        return "cash_awaiting_inflows" if self.overdue_inflows else "cash_other"


@dataclass(frozen=True)
class HourSnapshot:
    """State at the *end* of an hour."""

    hour: int
    balances: dict[str, int]
    overdue_count: int
    overdue_value: int
    newly_due_count: int
    newly_due_value: int
    settled_count: int
    settled_value: int
    settled_late_count: int
    settled_late_value: int
    category_counts: dict[str, int]
    category_values: dict[str, int]
    reason_counts: dict[str, int]
    reason_values: dict[str, int]
    service_used: dict[str, int]
    cumulative_external_inflow: int
    cumulative_external_outflow: int
    cumulative_intervention_funding: int
    credit_drawn: dict[str, int] = field(default_factory=dict)  # outstanding after the end-of-hour sweep
    cumulative_credit_drawn: int = 0
    cumulative_credit_repaid: int = 0


@dataclass(frozen=True)
class ObligationOutcome:
    obligation_id: str
    payer: str
    recipient: str
    amount: int
    due_hour: int
    priority: int
    purpose: str
    status: Status
    settled_hour: int | None
    delay_hours: int | None  # settled obligations only (0 = on time)
    overdue_hours: int  # hours overdue within the horizon (unsettled: horizon - due_hour)
    route_used: str | None
    via_backup: bool
    blocked_hours: int
    reason_hours: dict[str, int]
    category_hours: dict[str, int]
    mechanism: Mechanism
    first_block_hour: int | None
    final_reasons: tuple[str, ...]
    settlement_event_id: int | None
    via_offsetting: bool = False  # settled inside a multilateral offsetting batch

    @property
    def delayed(self) -> bool:
        return self.status is not Status.SETTLED_ON_TIME


@dataclass(frozen=True)
class FundingItem:
    hour: int
    participant: str
    amount: int
    intervention_id: str
    funder: str


@dataclass(frozen=True)
class CreditTerms:
    participant: str
    limit: int
    available_from_hour: int
    intervention_id: str
    provider: str

    @property
    def account(self) -> str:
        return f"credit:{self.intervention_id}"


@dataclass(frozen=True)
class ReserveTerms:
    participant: str
    protected_priority: int
    lookahead_hours: int
    active_from_hour: int
    intervention_id: str


@dataclass(frozen=True)
class OffsettingTerms:
    intervention_id: str
    active_from_hour: int
    operator: str

    @property
    def account(self) -> str:
        return f"clearing:{self.intervention_id}"


@dataclass(frozen=True)
class CompiledScenario:
    """A scenario with its interventions resolved into engine inputs."""

    scenario: Scenario
    effective_disruptions: tuple[Disruption, ...]
    eligible_routes: dict[str, tuple[tuple[str, int], ...]]  # obligation -> ((route, active_from_hour), ...)
    funding: tuple[FundingItem, ...]
    credit_lines: tuple[CreditTerms, ...] = ()
    reserves: tuple[ReserveTerms, ...] = ()
    offsetting: OffsettingTerms | None = None


@dataclass
class SimulationResult:
    scenario: Scenario
    effective_disruptions: tuple[Disruption, ...]
    eligible_routes: dict[str, tuple[tuple[str, int], ...]]
    funding: tuple[FundingItem, ...]
    service_status: list[dict[str, ServiceStatus]]
    events: list[Event]
    ledger: list[LedgerEntry]
    blocks: list[BlockRecord]
    hours: list[HourSnapshot]
    outcomes: dict[str, ObligationOutcome]
    initial_balances: dict[str, int]
    totals: dict[str, int]
    credit_lines: tuple[CreditTerms, ...] = ()
    credit_peak: dict[str, int] = field(default_factory=dict)  # largest outstanding draw at any moment
    reserves: tuple[ReserveTerms, ...] = ()
    offsetting: OffsettingTerms | None = None
    engine_version: str = ENGINE_VERSION
    _block_index: dict[str, list[BlockRecord]] | None = field(default=None, repr=False, compare=False)

    def blocks_by_obligation(self) -> dict[str, list[BlockRecord]]:
        if self._block_index is None:
            index: dict[str, list[BlockRecord]] = defaultdict(list)
            for record in self.blocks:
                index[record.obligation_id].append(record)
            self._block_index = dict(index)
        return self._block_index

    def event(self, event_id: int) -> Event:
        return self.events[event_id - 1]

    def digest(self, engine_version: str | None = None) -> str:
        """SHA-256 over the canonical event ledger and cash ledger (reproducibility check).

        ``engine_version`` overrides the version stamped into the payload; the
        regression tests use it to show that rules shared with an earlier engine
        version still produce byte-identical events and ledgers.
        """
        payload = {
            "engine_version": engine_version or self.engine_version,
            "events": [asdict(e) for e in self.events],
            "ledger": [asdict(entry) for entry in self.ledger],
        }
        blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=_json_default)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _json_default(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (set, frozenset)):
        return sorted(value)
    raise TypeError(f"not JSON serialisable: {type(value).__name__}")


# ---------------------------------------------------------------- compilation


def compile_scenario(scenario: Scenario) -> CompiledScenario:
    """Resolve applied interventions into disruptions, route eligibility and funding."""
    reductions = {
        iv.disruption: iv.duration_reduction_hours
        for iv in scenario.interventions
        if isinstance(iv, FasterRecoveryIntervention)
    }
    effective = tuple(
        d.model_copy(update={"duration_hours": max(0, d.duration_hours - reductions[d.id])})
        if d.id in reductions
        else d
        for d in scenario.disruptions
    )

    backups = [iv for iv in scenario.interventions if isinstance(iv, BackupRouteIntervention)]
    eligible: dict[str, tuple[tuple[str, int], ...]] = {}
    for ob in scenario.obligations:
        entries = [(rid, 0) for rid in ob.routes]
        present = set(ob.routes)
        for backup in backups:
            if backup.backup_route in present:
                continue
            if not any(rid in backup.primary_routes for rid in ob.routes):
                continue
            if backup.payers is not None and ob.payer not in backup.payers:
                continue
            entries.append((backup.backup_route, backup.active_from_hour))
            present.add(backup.backup_route)
        eligible[ob.id] = tuple(entries)

    funding = sorted(
        (
            FundingItem(iv.funding_hour, alloc.participant, alloc.amount, iv.id, iv.funder)
            for iv in scenario.interventions
            if isinstance(iv, LiquidityBufferIntervention)
            for alloc in iv.allocations
        ),
        key=lambda f: (f.hour, f.intervention_id, f.participant),
    )
    credit = sorted(
        (
            CreditTerms(facility.participant, facility.limit, iv.available_from_hour, iv.id, iv.provider)
            for iv in scenario.interventions
            if isinstance(iv, CreditLineIntervention)
            for facility in iv.facilities
        ),
        key=lambda c: c.participant,
    )
    reserves = sorted(
        (
            ReserveTerms(pid, iv.protected_priority, iv.lookahead_hours, iv.active_from_hour, iv.id)
            for iv in scenario.interventions
            if isinstance(iv, PriorityReserveIntervention)
            for pid in sorted(set(iv.participants if iv.participants is not None else scenario.internal_ids()))
        ),
        key=lambda t: t.participant,
    )
    offsetting = next(
        (OffsettingTerms(iv.id, iv.active_from_hour, iv.operator)
         for iv in scenario.interventions if isinstance(iv, OffsettingIntervention)),
        None,
    )
    return CompiledScenario(scenario, effective, eligible, tuple(funding), tuple(credit), tuple(reserves), offsetting)


def ordering_key(mode: str) -> Callable[[Obligation], tuple]:
    if mode == "priority_due_id":
        return lambda o: (o.priority, o.due_hour, o.id)
    if mode == "due_id":
        return lambda o: (o.due_hour, o.id)
    raise ValueError(f"unknown ordering '{mode}'")


# ------------------------------------------------------------------ simulation


def simulate(scenario: Scenario) -> SimulationResult:
    """Run the deterministic hourly simulation for ``scenario``."""
    result = _Run(scenario).run()
    if result is None:  # only a stop check can abandon a run
        raise RuntimeError("simulation ended without a result")
    return result


HourCheck = Callable[[int, list[Obligation], list[BlockRecord]], bool]


def simulate_until(scenario: Scenario, stop: HourCheck) -> SimulationResult | None:
    """Run ``scenario``, abandoning it as soon as ``stop(hour, settled, blocked)`` returns True.

    ``stop`` is called at the end of every hour with the obligations settled in that hour
    and the block records written for it. Returns None if the run was abandoned and
    otherwise the complete result, identical to ``simulate(scenario)``. Searches that only
    need to know whether a run reaches a given outcome use it to stop once it cannot.
    """
    return _Run(scenario).run(stop)


class _Run:
    def __init__(self, scenario: Scenario):
        self.s = scenario
        self.c = compile_scenario(scenario)
        self.horizon = scenario.horizon_hours
        self.op = OperationalModel(scenario, disruptions=self.c.effective_disruptions)
        self.internal = scenario.internal_ids()
        self.obligations = scenario.obligations_by_id()
        self.paths = {r.id: r.path for r in scenario.routes}
        # Precomputed for speed (results are unchanged): the services on each route path that
        # have a throughput limit, and, per hour, whether every service on the path is up.
        nominal = {svc.id: svc.capacity_per_hour for svc in scenario.services}
        self.limited_path = {rid: tuple(sid for sid in path if nominal[sid] is not None) for rid, path in self.paths.items()}
        self.up: dict[str, bool] = {}
        self._checks: dict[tuple, tuple] = {}  # this hour's route checks per eligible-route tuple (see _route_checks)
        self._inflow_summary: dict[str, tuple[int, list[list[Any]]]] = {}  # this hour's overdue-inflow data per payer
        self.key = ordering_key(scenario.settings.ordering)
        self.recycle_within_step = scenario.settings.liquidity_recycling == "within_step"

        self.balances = {p.id: p.initial_balance for p in scenario.participants}
        self.initial_balances = dict(self.balances)
        self.initial_total = sum(self.balances.values())
        self.events: list[Event] = []
        self.ledger: list[LedgerEntry] = []
        self.blocks: list[BlockRecord] = []
        self.hours: list[HourSnapshot] = []
        self.statuses: list[dict[str, ServiceStatus]] = []
        self.settled_hour: dict[str, int] = {}
        self.route_used: dict[str, str] = {}
        self.settlement_event: dict[str, int] = {}
        self.last_block: dict[str, BlockRecord] = {}
        self.cum_in = 0
        self.cum_out = 0
        self.cum_funding = 0
        self.credit = {terms.participant: terms for terms in self.c.credit_lines}
        self.drawn: dict[str, int] = dict.fromkeys(self.credit, 0)
        self.credit_peak: dict[str, int] = dict.fromkeys(self.credit, 0)
        self.cum_draw = 0
        self.cum_repay = 0
        # priority-reserve rules: each covered payer's protected obligations, and this hour's reserve
        self.reserve = {terms.participant: terms for terms in self.c.reserves}
        self.protected: dict[str, list[Obligation]] = {
            pid: sorted((ob for ob in scenario.obligations if ob.payer == pid and ob.priority <= terms.protected_priority),
                        key=lambda o: (o.due_hour, o.id))
            for pid, terms in self.reserve.items()
        }
        self.protected_ids = frozenset(ob.id for obs in self.protected.values() for ob in obs)
        self.reserved: dict[str, int] = {}  # payer -> cash kept for protected payments (rules active this hour)
        # gridlock resolution (multilateral offsetting)
        self.offset = self.c.offsetting
        self.batches = 0
        self.offset_gross = 0
        self.offset_net = 0
        self.via_offsetting: set[str] = set()
        self.sequence = 0

    # ---------------------------------------------------------------- records

    def _event(self, hour: int, type_: str, **kwargs: Any) -> int:
        event_id = len(self.events) + 1
        self.events.append(Event(event_id, hour, type_, **kwargs))
        return event_id

    def _ledger(self, hour: int, event_id: int, kind: str, reference: str, debit: str, credit: str, amount: int) -> None:
        self.ledger.append(
            LedgerEntry(
                entry_id=len(self.ledger) + 1,
                hour=hour,
                event_id=event_id,
                kind=kind,
                reference=reference,
                debit_account=debit,
                credit_account=credit,
                amount=amount,
                debit_balance_after=self.balances.get(debit),
                credit_balance_after=self.balances.get(credit),
            )
        )

    # ------------------------------------------------------------------ rules

    def _available_cash(self, participant: str, received_this_hour: dict[str, int]) -> int:
        balance = self.balances[participant]
        if not self.recycle_within_step:
            balance -= received_this_hour.get(participant, 0)
        return balance

    def _available_credit(self, participant: str, hour: int) -> int:
        terms = self.credit.get(participant)
        if terms is None or hour < terms.available_from_hour:
            return 0
        return terms.limit - self.drawn[participant]

    def _draw_credit(self, hour: int, participant: str, amount: int, obligation_id: str | None,
                     batch: int | None = None) -> None:
        terms = self.credit[participant]
        if amount > self._available_credit(participant, hour):
            raise InvariantViolation(f"credit draw for {obligation_id or f'batch {batch}'} exceeds the undrawn limit "
                                     f"of {participant}")
        self.balances[participant] += amount
        self.drawn[participant] += amount
        self.credit_peak[participant] = max(self.credit_peak[participant], self.drawn[participant])
        self.cum_draw += amount
        data = {"intervention": terms.intervention_id, "provider": terms.provider, "limit": terms.limit,
                "drawn_after": self.drawn[participant], "balance_after": self.balances[participant]}
        if batch is not None:
            data["offsetting_batch"] = batch
        event_id = self._event(hour, "CREDIT_DRAW", obligation_id=obligation_id, participant_id=participant,
                               amount=amount, data=data)
        self._ledger(hour, event_id, "credit_draw", terms.intervention_id, terms.account, participant, amount)

    def _reserve_need(self, participant: str, hour: int) -> int:
        """Unsettled protected payments of ``participant`` due before ``hour + lookahead`` (overdue ones included)."""
        horizon = hour + self.reserve[participant].lookahead_hours
        return sum(ob.amount for ob in self.protected[participant]
                   if ob.due_hour < horizon and ob.id not in self.settled_hour)

    def _is_protected(self, ob: Obligation) -> bool:
        return ob.id in self.protected_ids

    def _reserved_against(self, ob: Obligation) -> int:
        """Cash a payer keeps back from ``ob`` under its reserve rule (0 for protected payments or no active rule)."""
        if ob.id in self.protected_ids:
            return 0
        return self.reserved.get(ob.payer, 0)

    def _repay_credit(self, hour: int) -> None:
        for participant in sorted(self.drawn):
            outstanding = self.drawn[participant]
            repay = min(outstanding, self.balances[participant])
            if repay <= 0:
                continue
            terms = self.credit[participant]
            self.balances[participant] -= repay
            self.drawn[participant] -= repay
            self.cum_repay += repay
            event_id = self._event(
                hour,
                "CREDIT_REPAYMENT",
                participant_id=participant,
                amount=repay,
                data={"intervention": terms.intervention_id, "drawn_after": self.drawn[participant],
                      "balance_after": self.balances[participant]},
            )
            self._ledger(hour, event_id, "credit_repayment", terms.intervention_id, participant, terms.account, repay)

    def _usable_route(self, ob: Obligation, hour: int, remaining: dict[str, int | None]) -> str | None:
        """First eligible route that is active, has every service up and capacity left on its limited services.

        ``self.up`` holds this hour's "every service on the path is up" flags. On an up route a
        service without a throughput limit always has capacity, so only ``limited_path`` is checked.
        """
        up = self.up
        for route_id, active_from in self.c.eligible_routes[ob.id]:
            if hour < active_from or not up[route_id]:
                continue
            for sid in self.limited_path[route_id]:
                if remaining[sid] < 1:
                    break
            else:
                return route_id
        return None

    def _flow(self, ob: Obligation) -> str:
        if ob.payer in self.internal and ob.recipient in self.internal:
            return "internal_transfer"
        return "external_inflow" if ob.recipient in self.internal else "external_outflow"

    def _discharge(self, hour: int, ob: Obligation, route_id: str, remaining: dict[str, int | None],
                   used: dict[str, int], batch: int | None = None) -> int:
        """Consume capacity, mark ``ob`` settled and record its SETTLEMENT event (cash is moved by the caller)."""
        if ob.id in self.settled_hour:
            raise InvariantViolation(f"obligation {ob.id} would settle twice")
        for sid in self.paths[route_id]:
            if remaining[sid] is not None:
                remaining[sid] -= 1
            used[sid] = used.get(sid, 0) + 1
        if ob.payer in self.reserved and self._is_protected(ob):
            self.reserved[ob.payer] -= ob.amount
        self.sequence += 1
        data = {
            "payer": ob.payer,
            "recipient": ob.recipient,
            "route": route_id,
            "via_backup": route_id not in ob.routes,
            "due_hour": ob.due_hour,
            "delay_hours": hour - ob.due_hour,
            "sequence_in_hour": self.sequence,
            "flow": self._flow(ob),
            "payer_balance_after": self.balances.get(ob.payer),
            "recipient_balance_after": self.balances.get(ob.recipient),
        }
        if batch is not None:
            data["offsetting_batch"] = batch
            self.via_offsetting.add(ob.id)
        event_id = self._event(hour, "SETTLEMENT", obligation_id=ob.id, amount=ob.amount, data=data)
        self.settled_hour[ob.id] = hour
        self.route_used[ob.id] = route_id
        self.settlement_event[ob.id] = event_id
        return event_id

    def _settle(self, hour: int, ob: Obligation, route_id: str,
                remaining: dict[str, int | None], used: dict[str, int], received: dict[str, int]) -> None:
        payer_internal = ob.payer in self.internal
        recipient_internal = ob.recipient in self.internal
        if payer_internal:
            cash = self._available_cash(ob.payer, received)
            if cash < ob.amount:
                self._draw_credit(hour, ob.payer, ob.amount - cash, ob.id)
            self.balances[ob.payer] -= ob.amount
            if self.balances[ob.payer] < 0:
                raise InvariantViolation(f"negative balance for {ob.payer} after {ob.id}")
        if recipient_internal:
            self.balances[ob.recipient] += ob.amount
            received[ob.recipient] = received.get(ob.recipient, 0) + ob.amount
        kind = self._flow(ob)
        if kind == "external_inflow":
            self.cum_in += ob.amount
        elif kind == "external_outflow":
            self.cum_out += ob.amount
        event_id = self._discharge(hour, ob, route_id, remaining, used)
        self._ledger(hour, event_id, kind, ob.id, ob.payer, ob.recipient, ob.amount)

    # ------------------------------------------------------------- offsetting

    def _offsetting_batch(self, hour: int, queue: list[Obligation], remaining: dict[str, int | None],
                          received: dict[str, int]) -> list[tuple[Obligation, str]]:
        """The covered set of queued payments that can settle simultaneously (empty if none).

        Candidates are queued payments of participants inside the boundary, in queue order.
        Routes are assigned in that order against the hour's remaining capacity. While some
        participant's net position is not covered by its available cash plus undrawn credit
        (less, if it pays a routine payment in the set, the reserve it must keep), the last
        payment in queue order of the participant with the largest uncovered amount (ties:
        lowest id) is removed and the assignment repeated.

        Implemented exactly but without needless repetition: when no route check in an
        assignment failed for lack of capacity, removing a payment frees capacity nobody was
        waiting for, so the next assignment is the current one without that payment and is
        not recomputed. Net positions are updated incrementally; only a capacity-bound
        assignment is rebuilt from scratch. (Available cash and credit do not change while
        a batch is being searched, because nothing settles.)
        """
        internal = self.internal
        reserved = self.reserved
        protected_ids = self.protected_ids
        limited_path = self.limited_path
        up = self.up
        eligible = self.c.eligible_routes
        candidates = [ob for ob in queue if ob.payer in internal]
        if len(candidates) < 2:
            return []
        base_cover = {pid: self._available_cash(pid, received) + self._available_credit(pid, hour) for pid in internal}
        removed: set[str] = set()

        def assign() -> tuple[list[tuple[Obligation, str]], bool]:
            capacity = dict(remaining)
            out: list[tuple[Obligation, str]] = []
            bound = False  # did a route check fail because payments earlier in this set used the capacity?
            for ob in candidates:
                if ob.id in removed:
                    continue
                chosen = None
                for route_id, active_from in eligible[ob.id]:
                    if hour < active_from or not up[route_id]:
                        continue
                    for sid in limited_path[route_id]:
                        if capacity[sid] < 1:
                            # a service already saturated before the set fails whatever is removed
                            if remaining[sid] >= 1:
                                bound = True
                            break
                    else:
                        chosen = route_id
                        break
                if chosen is None:
                    continue
                for sid in limited_path[chosen]:
                    capacity[sid] -= 1
                out.append((ob, chosen))
            return out, bound

        while True:
            assigned, bound = assign()
            if len(assigned) < 2:  # a single payment would already have settled on its own
                return []
            alive = [True] * len(assigned)
            alive_count = len(assigned)
            net: dict[str, int] = defaultdict(int)
            appear: Counter[str] = Counter()  # entries in the set naming the participant (as payer or recipient)
            protected_paid: Counter[str] = Counter()
            routine: Counter[str] = Counter()  # routine payments of payers under an active reserve rule
            by_payer: dict[str, list[int]] = defaultdict(list)
            for index, (ob, _) in enumerate(assigned):
                net[ob.payer] -= ob.amount
                appear[ob.payer] += 1
                by_payer[ob.payer].append(index)
                if ob.recipient in internal:
                    net[ob.recipient] += ob.amount
                    appear[ob.recipient] += 1
                if ob.payer in reserved:
                    if ob.id in protected_ids:
                        protected_paid[ob.payer] += ob.amount
                    else:
                        routine[ob.payer] += 1
            while True:
                uncovered: dict[str, int] = {}
                for pid, position in net.items():
                    if not appear[pid]:
                        continue
                    keep = reserved[pid] - protected_paid[pid] if routine[pid] else 0
                    cover = base_cover[pid] + position - keep
                    if cover < 0:
                        uncovered[pid] = -cover
                if not uncovered:
                    return [entry for entry, keep_it in zip(assigned, alive) if keep_it]
                worst = min(uncovered, key=lambda pid: (-uncovered[pid], pid))
                index = by_payer[worst].pop()  # the worst participant's last payment in queue order
                last = assigned[index][0]
                removed.add(last.id)
                if bound:
                    break  # freed capacity can change later assignments: rebuild from scratch
                alive[index] = False
                alive_count -= 1
                if alive_count < 2:
                    return []
                net[last.payer] += last.amount
                appear[last.payer] -= 1
                if last.recipient in internal:
                    net[last.recipient] -= last.amount
                    appear[last.recipient] -= 1
                if last.payer in reserved:
                    if last.id in protected_ids:
                        protected_paid[last.payer] -= last.amount
                    else:
                        routine[last.payer] -= 1

    def _settle_batch(self, hour: int, batch: list[tuple[Obligation, str]], remaining: dict[str, int | None],
                      used: dict[str, int], received: dict[str, int]) -> None:
        """Discharge every obligation in ``batch`` at once; cash moves net through the clearing account."""
        terms = self.offset
        if terms is None:  # only reached when an offsetting intervention is active
            raise InvariantViolation("offsetting batch without an active offsetting intervention")
        self.batches += 1
        number = self.batches
        net: dict[str, int] = defaultdict(int)
        for ob, _ in batch:
            net[ob.payer] -= ob.amount
            if ob.recipient in self.internal:
                net[ob.recipient] += ob.amount
        outside = [ob for ob, _ in batch if ob.recipient not in self.internal]
        gross = sum(ob.amount for ob, _ in batch)
        paid_in = sum(-v for v in net.values() if v < 0)
        paid_out = sum(v for v in net.values() if v > 0) + sum(ob.amount for ob in outside)
        if paid_in != paid_out:
            raise InvariantViolation(f"offsetting batch {number} in hour {hour} does not net to zero")
        self.offset_gross += gross
        self.offset_net += paid_in
        event_id = self._event(
            hour,
            "OFFSETTING_BATCH",
            amount=gross,
            data={
                "intervention": terms.intervention_id,
                "operator": terms.operator,
                "batch": number,
                "obligations": [ob.id for ob, _ in batch],
                "gross_value": gross,
                "net_value": paid_in,
                "liquidity_saved": gross - paid_in,
                "net_positions": {pid: v for pid, v in sorted(net.items())},
            },
        )
        for pid in sorted(net):  # net payers: draw committed credit for any shortfall, then pay in
            if net[pid] >= 0:
                continue
            cash = self._available_cash(pid, received)
            if cash + net[pid] < 0:
                self._draw_credit(hour, pid, -(cash + net[pid]), None, batch=number)
            self.balances[pid] += net[pid]
            if self.balances[pid] < 0:
                raise InvariantViolation(f"negative balance for {pid} in offsetting batch {number}")
            self._ledger(hour, event_id, "offset_debit", terms.intervention_id, pid, terms.account, -net[pid])
        for ob in outside:  # recipients outside the boundary are paid gross from the clearing account
            self.cum_out += ob.amount
            self._ledger(hour, event_id, "external_outflow", ob.id, terms.account, ob.recipient, ob.amount)
        for pid in sorted(net):  # net receivers are paid out
            if net[pid] <= 0:
                continue
            self.balances[pid] += net[pid]
            received[pid] = received.get(pid, 0) + net[pid]
            self._ledger(hour, event_id, "offset_credit", terms.intervention_id, terms.account, pid, net[pid])
        for ob, route_id in batch:
            self._discharge(hour, ob, route_id, remaining, used, batch=number)

    def _route_checks(self, eligible: tuple[tuple[str, int], ...], hour: int, status: dict[str, ServiceStatus],
                      remaining: dict[str, int | None]) -> tuple[tuple[dict[str, Any], ...], bool, tuple[str, ...]]:
        """The state of every eligible route at the end of ``hour``: (checks, any route usable, route reasons).

        Blocks are recorded after the hour's last settlement, so the result depends only on
        the eligible routes; it is computed once per hour for each distinct tuple. The check
        records are shared read-only by every block that has the same eligible routes.
        """
        cached = self._checks.get(eligible)
        if cached is not None:
            return cached
        checks: list[dict[str, Any]] = []
        route_ok = False
        for route_id, active_from in eligible:
            if hour < active_from:
                checks.append({"route": route_id, "status": Reason.ROUTE_NOT_YET_ACTIVE.value, "active_from_hour": active_from})
                continue
            path = self.paths[route_id]
            down = [sid for sid in path if status[sid].pct <= 0]
            if down:
                checks.append({"route": route_id, "status": Reason.SERVICE_UNAVAILABLE.value, "services": down})
                continue
            full = [sid for sid in path if remaining[sid] is not None and remaining[sid] < 1]
            if full:
                checks.append({"route": route_id, "status": Reason.CAPACITY_EXHAUSTED.value, "services": full})
                continue
            checks.append({"route": route_id, "status": "AVAILABLE"})
            route_ok = True
        reasons = () if route_ok else tuple(sorted({c["status"] for c in checks}))
        cached = (tuple(checks), route_ok, reasons)
        self._checks[eligible] = cached
        return cached

    def _record_block(self, hour: int, ob: Obligation, status: dict[str, ServiceStatus],
                      remaining: dict[str, int | None], received: dict[str, int],
                      overdue_inflows: dict[str, tuple[tuple[str, int], ...]]) -> BlockRecord:
        checks, route_ok, route_reasons = self._route_checks(self.c.eligible_routes[ob.id], hour, status, remaining)

        balance = available = shortfall = credit = reserved = None
        inflows: tuple[tuple[str, int], ...] = ()
        cash_ok = True
        if ob.payer in self.internal:
            balance = self.balances[ob.payer]
            available = self._available_cash(ob.payer, received)
            if ob.payer in self.credit:
                credit = self._available_credit(ob.payer, hour)
            if ob.payer in self.reserved and not self._is_protected(ob):
                reserved = self.reserved[ob.payer]
            cash_ok = available + (credit or 0) - (reserved or 0) >= ob.amount
            if not cash_ok:
                shortfall = ob.amount - available - (credit or 0) + (reserved or 0)
                inflows = overdue_inflows.get(ob.payer, ())

        if route_ok and cash_ok:
            raise InvariantViolation(f"{ob.id} could settle in hour {hour} but was left in the queue")

        reasons: list[str] = list(route_reasons)
        if not cash_ok:
            reasons.append(CASH_REASON)

        data: dict[str, Any] = {
            "payer": ob.payer,
            "recipient": ob.recipient,
            "due_hour": ob.due_hour,
            "reasons": reasons,
            "route_checks": list(checks),
        }
        if balance is not None:
            data["payer_balance"] = balance
            data["payer_available_cash"] = available
        if credit is not None:
            data["payer_available_credit"] = credit
        if reserved is not None:
            data["payer_reserved_for_priority"] = reserved
        if shortfall is not None:
            # identical for every cash-blocked payment of this payer in this hour: built once, shared read-only
            summary = self._inflow_summary.get(ob.payer)
            if summary is None:
                summary = self._inflow_summary[ob.payer] = (sum(a for _, a in inflows), [list(x) for x in inflows])
            data["shortfall"] = shortfall
            data["payer_overdue_inflow_count"] = len(inflows)
            data["payer_overdue_inflow_value"] = summary[0]
            data["payer_overdue_inflows"] = summary[1]
        event_id = self._event(hour, "BLOCKED", obligation_id=ob.id, amount=ob.amount, data=data)
        record = BlockRecord(
            hour=hour,
            obligation_id=ob.id,
            payer=ob.payer,
            amount=ob.amount,
            reasons=tuple(reasons),
            route_checks=checks,
            payer_balance=balance,
            available_cash=available,
            shortfall=shortfall,
            overdue_inflows=inflows,
            event_id=event_id,
            available_credit=credit,
            reserved=reserved,
        )
        # Fill the cached derived flags from what is already known (same values the
        # properties compute: route reasons are all operational, cash is the cash flag).
        operational = bool(route_reasons)
        cash = not cash_ok
        if operational:
            category = "operational_and_cash" if cash else "operational_only"
        else:
            category = "cash_awaiting_inflows" if inflows else "cash_other"
        vars(record).update(operational=operational, cash=cash, category=category)
        self.blocks.append(record)
        self.last_block[ob.id] = record
        return record

    # ------------------------------------------------------------------- loop

    def _settlement_loop(self, hour: int, queue: list[Obligation], status: dict[str, ServiceStatus],
                         remaining: dict[str, int | None], used: dict[str, int], received: dict[str, int],
                         settled_now: list[Obligation]) -> list[Obligation]:
        """Settle the first feasible obligation in queue order, then "rescan from the top", until none can settle.

        Implemented exactly but without literal rescans: a settlement only lowers capacity
        and the payer's cash and credit, and raises the recipient's cash. A payer's reserve
        falls by exactly the protected amount it pays, so its free cash for routine payments
        does not rise either. The only queued obligations that can newly become feasible are
        therefore the recipient's own payments (and, under next_step recycling, not even
        those). Every obligation before the scan position stays infeasible except those,
        which are re-checked in order; the first feasible one is exactly what a full rescan
        would find. Returns the obligations still queued.
        """
        alive = [True] * len(queue)
        by_payer: dict[str, list[int]] = defaultdict(list)
        for index, ob in enumerate(queue):
            by_payer[ob.payer].append(index)
        internal = self.internal
        usable_route = self._usable_route
        available_cash = self._available_cash
        available_credit = self._available_credit
        reserved_against = self._reserved_against

        def feasible_route(ob: Obligation) -> str | None:
            route_id = usable_route(ob, hour, remaining)
            if route_id is None:
                return None
            if ob.payer in internal and (
                available_cash(ob.payer, received) + available_credit(ob.payer, hour) - reserved_against(ob) < ob.amount
            ):
                return None
            return route_id

        position = 0
        while position < len(queue):
            ob = queue[position]
            route_id = feasible_route(ob) if alive[position] else None
            if route_id is None:
                position += 1
                continue
            alive[position] = False
            self._settle(hour, ob, route_id, remaining, used, received)
            settled_now.append(ob)
            restart = None
            if self.recycle_within_step and ob.recipient in self.internal:
                for earlier in by_payer.get(ob.recipient, ()):
                    if earlier >= position:
                        break
                    if alive[earlier] and feasible_route(queue[earlier]) is not None:
                        restart = earlier
                        break
            position = restart if restart is not None else position + 1
        return [ob for ob, keep in zip(queue, alive) if keep]

    def run(self, stop: HourCheck | None = None) -> SimulationResult | None:
        due_at: dict[int, list[Obligation]] = defaultdict(list)
        for ob in self.s.obligations:
            due_at[ob.due_hour].append(ob)
        funding_at: dict[int, list[FundingItem]] = defaultdict(list)
        for item in self.c.funding:
            funding_at[item.hour].append(item)

        queue: list[Obligation] = []
        previous: dict[str, tuple] = {}
        previous_status: dict[str, ServiceStatus] | None = None

        for hour in range(self.horizon):
            # 1. operational state and shared capacity for this hour
            status = self.op.status_at(hour)
            self.statuses.append(status)
            # status_at returns the same (cached) mapping while the active disruption phases are
            # unchanged; then no service changed state and there is nothing to record.
            for sid in (self.op.order if status is not previous_status else ()):
                st = status[sid]
                signature = (st.state, st.pct, st.capacity)
                before = previous.get(sid)
                changed = before != signature if before is not None else st.state is not ServiceState.AVAILABLE
                if changed:
                    self._event(
                        hour,
                        "SERVICE_STATE",
                        service_id=sid,
                        data={
                            "state": st.state.value,
                            "pct": st.pct,
                            "capacity": st.capacity,
                            "nominal_capacity": st.nominal_capacity,
                            "previous_state": before[0].value if before else None,
                            "causes": [dict(c) for c in st.causes],
                        },
                    )
                previous[sid] = signature
            remaining: dict[str, int | None] = {sid: st.capacity for sid, st in status.items()}
            used: dict[str, int] = {}  # services' payments this hour, first-use order
            if status is not previous_status:
                self.up = {rid: all(status[sid].pct > 0 for sid in path) for rid, path in self.paths.items()}
            previous_status = status
            self._checks = {}
            self._inflow_summary = {}

            # 2. scheduled intervention funding (explicit external inflow)
            for item in funding_at.get(hour, ()):
                self.balances[item.participant] += item.amount
                self.cum_funding += item.amount
                event_id = self._event(
                    hour,
                    "FUNDING",
                    participant_id=item.participant,
                    amount=item.amount,
                    data={"intervention": item.intervention_id, "funder": item.funder,
                          "balance_after": self.balances[item.participant]},
                )
                self._ledger(hour, event_id, "intervention_funding", item.intervention_id,
                             f"external:{item.intervention_id}", item.participant, item.amount)

            # 3. newly due obligations join the queue
            newly_due = sorted(due_at.get(hour, ()), key=self.key)
            for ob in newly_due:
                self._event(
                    hour,
                    "OBLIGATION_DUE",
                    obligation_id=ob.id,
                    amount=ob.amount,
                    data={"payer": ob.payer, "recipient": ob.recipient, "priority": ob.priority,
                          "eligible_routes": [r for r, _ in self.c.eligible_routes[ob.id]]},
                )
            if newly_due:  # the queue only ever loses entries in between, so it is still in order otherwise
                queue.extend(newly_due)
                queue.sort(key=self.key)

            # 4. settlement loop (see _settlement_loop), then optional offsetting batches
            received: dict[str, int] = {}  # cash received this hour per participant
            settled_now: list[Obligation] = []
            self.sequence = 0
            self.reserved = {pid: self._reserve_need(pid, hour) for pid, terms in self.reserve.items()
                             if hour >= terms.active_from_hour}
            queue = self._settlement_loop(hour, queue, status, remaining, used, received, settled_now)
            if self.offset is not None and hour >= self.offset.active_from_hour:
                while True:
                    batch = self._offsetting_batch(hour, queue, remaining, received)
                    if not batch:
                        break
                    self._settle_batch(hour, batch, remaining, used, received)
                    settled_now.extend(ob for ob, _ in batch)
                    done = {ob.id for ob, _ in batch}
                    queue = [ob for ob in queue if ob.id not in done]
                    # net proceeds (and a smaller reserve) can make further single payments feasible
                    queue = self._settlement_loop(hour, queue, status, remaining, used, received, settled_now)

            # 5. every remaining queued obligation is overdue: record all blocking reasons
            pending_in: dict[str, list[Obligation]] = defaultdict(list)
            for ob in queue:
                if ob.recipient in self.internal:
                    pending_in[ob.recipient].append(ob)
            overdue_inflows = {  # largest first, then id: the order the explanation traces follow
                pid: tuple((o.id, o.amount) for o in sorted(obs, key=lambda o: (-o.amount, o.id)))
                for pid, obs in pending_in.items()
            }
            hour_blocks = [self._record_block(hour, ob, status, remaining, received, overdue_inflows) for ob in queue]

            # 6. end-of-hour credit sweep: cash repays outstanding draws first
            if self.credit:
                self._repay_credit(hour)

            # 7. snapshot and invariant checks
            self._check_invariants(hour, status, used)
            self.hours.append(self._snapshot(hour, queue, newly_due, settled_now, hour_blocks, used))
            if stop is not None and stop(hour, settled_now, hour_blocks):
                return None

        for ob in queue:
            last = self.last_block[ob.id]
            self._event(
                self.horizon - 1,
                "UNSETTLED_AT_HORIZON",
                obligation_id=ob.id,
                amount=ob.amount,
                data={"due_hour": ob.due_hour, "horizon_hours": self.horizon, "last_reasons": list(last.reasons)},
            )

        outcomes = self._outcomes()
        totals = {
            "initial_internal_cash": self.initial_total,
            "final_internal_cash": sum(self.balances.values()),
            "external_inflow": self.cum_in,
            "external_outflow": self.cum_out,
            "intervention_funding": self.cum_funding,
            "credit_drawn": self.cum_draw,
            "credit_repaid": self.cum_repay,
            "credit_outstanding": sum(self.drawn.values()),
            "obligations_count": len(self.s.obligations),
            "obligations_value": sum(o.amount for o in self.s.obligations),
        }
        if self.offset is not None:
            totals.update({"offsetting_batches": self.batches, "offsetting_gross_value": self.offset_gross,
                           "offsetting_net_value": self.offset_net})
        return SimulationResult(
            scenario=self.s,
            effective_disruptions=self.c.effective_disruptions,
            eligible_routes=self.c.eligible_routes,
            funding=self.c.funding,
            service_status=self.statuses,
            events=self.events,
            ledger=self.ledger,
            blocks=self.blocks,
            hours=self.hours,
            outcomes=outcomes,
            initial_balances=self.initial_balances,
            totals=totals,
            credit_lines=self.c.credit_lines,
            credit_peak=dict(self.credit_peak),
            reserves=self.c.reserves,
            offsetting=self.offset,
        )

    def _check_invariants(self, hour: int, status: dict[str, ServiceStatus], used: dict[str, int]) -> None:
        expected = self.initial_total + self.cum_in - self.cum_out + self.cum_funding + self.cum_draw - self.cum_repay
        actual = sum(self.balances.values())
        if actual != expected:
            raise InvariantViolation(f"cash not conserved at hour {hour}: {actual} != {expected}")
        negative = [p for p, b in self.balances.items() if b < 0]
        if negative:
            raise InvariantViolation(f"negative balances at hour {hour}: {negative}")
        for participant, drawn in self.drawn.items():
            if not 0 <= drawn <= self.credit[participant].limit:
                raise InvariantViolation(f"credit outstanding for {participant} outside [0, limit] at hour {hour}")
        if self.cum_draw - self.cum_repay != sum(self.drawn.values()):
            raise InvariantViolation(f"credit draws and repayments do not reconcile at hour {hour}")
        for sid, count in used.items():
            cap = status[sid].capacity
            if cap is not None and count > cap:
                raise InvariantViolation(f"{sid} processed {count} payments with capacity {cap} in hour {hour}")

    def _snapshot(self, hour: int, queue: list[Obligation], newly_due: list[Obligation],
                  settled_now: list[Obligation], hour_blocks: list[BlockRecord], used: dict[str, int]) -> HourSnapshot:
        category_counts = dict.fromkeys(CATEGORIES, 0)
        category_values = dict.fromkeys(CATEGORIES, 0)
        reason_counts: dict[str, int] = {}  # first-seen order, like a Counter
        reason_values: dict[str, int] = {}
        for record in hour_blocks:
            category = record.category
            amount = record.amount
            category_counts[category] += 1
            category_values[category] += amount
            for reason in record.reasons:
                reason_counts[reason] = reason_counts.get(reason, 0) + 1
                reason_values[reason] = reason_values.get(reason, 0) + amount
        late = [ob for ob in settled_now if ob.due_hour < hour]
        return HourSnapshot(
            hour=hour,
            balances=dict(self.balances),
            overdue_count=len(queue),
            overdue_value=sum(map(_AMOUNT, queue)),
            newly_due_count=len(newly_due),
            newly_due_value=sum(map(_AMOUNT, newly_due)),
            settled_count=len(settled_now),
            settled_value=sum(map(_AMOUNT, settled_now)),
            settled_late_count=len(late),
            settled_late_value=sum(map(_AMOUNT, late)),
            category_counts=category_counts,
            category_values=category_values,
            reason_counts=reason_counts,
            reason_values=reason_values,
            service_used=dict(used),
            cumulative_external_inflow=self.cum_in,
            cumulative_external_outflow=self.cum_out,
            cumulative_intervention_funding=self.cum_funding,
            credit_drawn=dict(self.drawn),
            cumulative_credit_drawn=self.cum_draw,
            cumulative_credit_repaid=self.cum_repay,
        )

    def _outcomes(self) -> dict[str, ObligationOutcome]:
        by_obligation: dict[str, list[BlockRecord]] = defaultdict(list)
        for record in self.blocks:
            by_obligation[record.obligation_id].append(record)
        outcomes: dict[str, ObligationOutcome] = {}
        for ob in self.s.obligations:
            records = by_obligation.get(ob.id, [])
            settled = self.settled_hour.get(ob.id)
            if settled is None:
                status, delay, overdue = Status.UNSETTLED_AT_HORIZON, None, self.horizon - ob.due_hour
            elif settled == ob.due_hour:
                status, delay, overdue = Status.SETTLED_ON_TIME, 0, 0
            else:
                status, delay, overdue = Status.SETTLED_LATE, settled - ob.due_hour, settled - ob.due_hour
            if len(records) != overdue:
                raise InvariantViolation(f"{ob.id}: {len(records)} block records for {overdue} overdue hours")
            if records:
                reason_hours = dict(Counter(reason for record in records for reason in record.reasons))
                counted = Counter(record.category for record in records)
                category_hours = {c: counted.get(c, 0) for c in CATEGORIES}
            else:  # never blocked (most obligations): skip building empty counters
                reason_hours, category_hours = {}, dict.fromkeys(CATEGORIES, 0)
            outcomes[ob.id] = ObligationOutcome(
                obligation_id=ob.id,
                payer=ob.payer,
                recipient=ob.recipient,
                amount=ob.amount,
                due_hour=ob.due_hour,
                priority=ob.priority,
                purpose=ob.purpose,
                status=status,
                settled_hour=settled,
                delay_hours=delay,
                overdue_hours=overdue,
                route_used=self.route_used.get(ob.id),
                via_backup=ob.id in self.route_used and self.route_used[ob.id] not in ob.routes,
                blocked_hours=len(records),
                reason_hours=reason_hours,
                category_hours=category_hours,
                mechanism=_classify(records),
                first_block_hour=records[0].hour if records else None,
                final_reasons=records[-1].reasons if records else (),
                settlement_event_id=self.settlement_event.get(ob.id),
                via_offsetting=ob.id in self.via_offsetting,
            )
        return outcomes


def _classify(records: list[BlockRecord]) -> Mechanism:
    if not records:
        return Mechanism.NONE
    any_cash = any(r.cash for r in records)
    any_operational = any(r.operational for r in records)
    if not any_cash:
        return Mechanism.DIRECT_OPERATIONAL
    if not any_operational:
        if any(r.overdue_inflows for r in records):
            return Mechanism.FINANCIAL_PROPAGATION
        return Mechanism.LIQUIDITY_OTHER
    return Mechanism.MIXED
