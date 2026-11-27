"""An independent, deliberately naive re-implementation of the settlement rules (docs/MODEL.md 3-4).

It shares nothing with ``cyberliquidity.engine`` or ``cyberliquidity.operational`` except
the validated input models, and it is written for clarity rather than speed: recursive
service evaluation, a full rescan of the queue after every settlement, reserves and
offsetting positions recomputed from scratch at every check, and plain dictionaries. ``tests/test_differential.py`` checks that the engine and this model agree
event for event on hundreds of random scenarios.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from cyberliquidity.models import (
    BackupRouteIntervention,
    CreditLineIntervention,
    FasterRecoveryIntervention,
    LiquidityBufferIntervention,
    OffsettingIntervention,
    PriorityReserveIntervention,
    Scenario,
)


@dataclass
class ReferenceResult:
    settled_hour: dict[str, int] = field(default_factory=dict)
    route_used: dict[str, str] = field(default_factory=dict)
    balances: list[dict[str, int]] = field(default_factory=list)  # end of each hour
    credit_drawn: list[dict[str, int]] = field(default_factory=list)  # end of each hour, after the sweep
    reasons: dict[tuple[str, int], tuple[str, ...]] = field(default_factory=dict)  # (obligation, hour) -> reasons
    credit_peak: dict[str, int] = field(default_factory=dict)
    settlement_order: list[tuple[int, str]] = field(default_factory=list)
    batches: list[tuple[int, tuple[str, ...]]] = field(default_factory=list)  # (hour, obligation ids) per offsetting batch


def _service_pct(scenario: Scenario, durations: dict[str, int], hour: int) -> dict[str, int]:
    services = {s.id: s for s in scenario.services}
    own: dict[str, int] = {sid: 100 for sid in services}
    for d in scenario.disruptions:
        duration = durations[d.id]
        end = d.start_hour + duration
        if d.start_hour <= hour < end:
            own[d.service] = min(own[d.service], 100 - d.severity_pct)
        elif d.recovery_hours and end <= hour < end + d.recovery_hours:
            own[d.service] = min(own[d.service], d.recovery_capacity_pct)
    memo: dict[str, int] = {}

    def pct(sid: str) -> int:
        if sid not in memo:
            svc = services[sid]
            value = own[sid]
            for dep in svc.requires_all:
                value = min(value, pct(dep))
            for group in svc.requires_any:
                value = min(value, max(pct(m) for m in group))
            memo[sid] = value
        return memo[sid]

    return {sid: pct(sid) for sid in services}


def reference_simulate(scenario: Scenario) -> ReferenceResult:
    H = scenario.horizon_hours
    internal = {p.id for p in scenario.participants}
    paths = {r.id: list(r.path) for r in scenario.routes}
    nominal = {s.id: s.capacity_per_hour for s in scenario.services}
    ivs = list(scenario.interventions)

    durations = {d.id: d.duration_hours for d in scenario.disruptions}
    for iv in ivs:
        if isinstance(iv, FasterRecoveryIntervention):
            durations[iv.disruption] = max(0, durations[iv.disruption] - iv.duration_reduction_hours)

    eligible: dict[str, list[tuple[str, int]]] = {}
    for ob in scenario.obligations:
        entries = [(r, 0) for r in ob.routes]
        for iv in ivs:
            if not isinstance(iv, BackupRouteIntervention):
                continue
            if iv.backup_route in [r for r, _ in entries]:
                continue
            if not set(ob.routes) & set(iv.primary_routes):
                continue
            if iv.payers is not None and ob.payer not in iv.payers:
                continue
            entries.append((iv.backup_route, iv.active_from_hour))
        eligible[ob.id] = entries

    funding: dict[int, list[tuple[str, int]]] = {}
    for iv in ivs:
        if isinstance(iv, LiquidityBufferIntervention):
            for a in iv.allocations:
                funding.setdefault(iv.funding_hour, []).append((a.participant, a.amount))
    limit: dict[str, int] = {}
    credit_from: dict[str, int] = {}
    for iv in ivs:
        if isinstance(iv, CreditLineIntervention):
            for f in iv.facilities:
                limit[f.participant] = f.limit
                credit_from[f.participant] = iv.available_from_hour

    rules: dict[str, tuple[int, int, int]] = {}  # participant -> (protected priority, look-ahead, active from)
    for iv in ivs:
        if isinstance(iv, PriorityReserveIntervention):
            for p in (iv.participants if iv.participants is not None else sorted(internal)):
                rules[p] = (iv.protected_priority, iv.lookahead_hours, iv.active_from_hour)
    offsetting_from = next((iv.active_from_hour for iv in ivs if isinstance(iv, OffsettingIntervention)), None)

    if scenario.settings.ordering == "priority_due_id":
        def key(o):
            return (o.priority, o.due_hour, o.id)
    else:
        def key(o):
            return (o.due_hour, o.id)

    balance = {p.id: p.initial_balance for p in scenario.participants}
    drawn = dict.fromkeys(limit, 0)
    out = ReferenceResult(credit_peak=dict.fromkeys(limit, 0))
    queue = []
    for hour in range(H):
        pct = _service_pct(scenario, durations, hour)
        remaining = {}
        for sid, value in pct.items():
            if value <= 0:
                remaining[sid] = 0
            elif nominal[sid] is None:
                remaining[sid] = None
            else:
                remaining[sid] = nominal[sid] * value // 100
        for participant, amount in funding.get(hour, []):
            balance[participant] += amount
        queue += [o for o in scenario.obligations if o.due_hour == hour]
        queue.sort(key=key)
        received = dict.fromkeys(balance, 0)

        def route_status(route: str, active_from: int) -> str:
            if hour < active_from:
                return "ROUTE_NOT_YET_ACTIVE"
            if any(pct[s] <= 0 for s in paths[route]):
                return "SERVICE_UNAVAILABLE"
            if any(remaining[s] is not None and remaining[s] < 1 for s in paths[route]):
                return "CAPACITY_EXHAUSTED"
            return "AVAILABLE"

        def cash(participant: str) -> int:
            value = balance[participant]
            if scenario.settings.liquidity_recycling == "next_step":
                value -= received[participant]
            return value

        def credit(participant: str) -> int:
            if participant not in limit or hour < credit_from[participant]:
                return 0
            return limit[participant] - drawn[participant]

        def rule_active(participant: str) -> bool:
            return participant in rules and hour >= rules[participant][2]

        def routine(ob) -> bool:  # held to the reserve rule
            return rule_active(ob.payer) and ob.priority > rules[ob.payer][0]

        def reserve_need(participant: str, treat_as_settled=()) -> int:
            protected, lookahead, _ = rules[participant]
            return sum(o.amount for o in scenario.obligations
                       if o.payer == participant and o.priority <= protected and o.due_hour < hour + lookahead
                       and o.id not in out.settled_hour and o.id not in treat_as_settled)

        def keep(ob) -> int:
            return reserve_need(ob.payer) if routine(ob) else 0

        def first_usable_route(ob, capacity) -> str | None:
            for r, a in eligible[ob.id]:
                if hour < a or any(pct[s] <= 0 for s in paths[r]):
                    continue
                if any(capacity[s] is not None and capacity[s] < 1 for s in paths[r]):
                    continue
                return r
            return None

        def mark_settled(ob, route) -> None:
            for s in paths[route]:
                if remaining[s] is not None:
                    remaining[s] -= 1
            queue.remove(ob)
            out.settled_hour[ob.id] = hour
            out.route_used[ob.id] = route
            out.settlement_order.append((hour, ob.id))

        def draw(participant: str, amount: int) -> None:
            balance[participant] += amount
            drawn[participant] += amount
            out.credit_peak[participant] = max(out.credit_peak[participant], drawn[participant])

        def settle_singles() -> None:
            progress = True
            while progress:
                progress = False
                for ob in list(queue):
                    route = next((r for r, a in eligible[ob.id] if route_status(r, a) == "AVAILABLE"), None)
                    if route is None:
                        continue
                    if ob.payer in internal and cash(ob.payer) + credit(ob.payer) - keep(ob) < ob.amount:
                        continue
                    if ob.payer in internal:
                        short = ob.amount - cash(ob.payer)
                        if short > 0:
                            draw(ob.payer, short)
                        balance[ob.payer] -= ob.amount
                    if ob.recipient in internal:
                        balance[ob.recipient] += ob.amount
                        received[ob.recipient] += ob.amount
                    mark_settled(ob, route)
                    progress = True
                    break  # rescan from the top

        def find_batch() -> list:
            removed: set[str] = set()
            while True:
                capacity = dict(remaining)
                chosen = []
                for ob in queue:
                    if ob.payer not in internal or ob.id in removed:
                        continue
                    route = first_usable_route(ob, capacity)
                    if route is None:
                        continue
                    for s in paths[route]:
                        if capacity[s] is not None:
                            capacity[s] -= 1
                    chosen.append((ob, route))
                if len(chosen) < 2:
                    return []
                ids = {ob.id for ob, _ in chosen}
                touched = {ob.payer for ob, _ in chosen} | {ob.recipient for ob, _ in chosen if ob.recipient in internal}
                deficit = {}
                for p in sorted(touched):
                    position = (sum(ob.amount for ob, _ in chosen if ob.recipient == p)
                                - sum(ob.amount for ob, _ in chosen if ob.payer == p))
                    pays_routine = any(ob.payer == p and routine(ob) for ob, _ in chosen)
                    kept = reserve_need(p, treat_as_settled=ids) if pays_routine else 0
                    cover = cash(p) + credit(p) + position - kept
                    if cover < 0:
                        deficit[p] = -cover
                if not deficit:
                    return chosen
                worst = sorted(deficit, key=lambda p: (-deficit[p], p))[0]
                removed.add([ob for ob, _ in chosen if ob.payer == worst][-1].id)

        def settle_batch(batch) -> None:
            net: dict[str, int] = {}
            for ob, _ in batch:
                net[ob.payer] = net.get(ob.payer, 0) - ob.amount
                if ob.recipient in internal:
                    net[ob.recipient] = net.get(ob.recipient, 0) + ob.amount
            for p in sorted(net):
                if net[p] < 0:
                    short = -(cash(p) + net[p])
                    if short > 0:
                        draw(p, short)
                    balance[p] += net[p]
            for p in sorted(net):
                if net[p] > 0:
                    balance[p] += net[p]
                    received[p] += net[p]
            for ob, route in batch:
                mark_settled(ob, route)
            out.batches.append((hour, tuple(ob.id for ob, _ in batch)))

        settle_singles()
        while offsetting_from is not None and hour >= offsetting_from:
            batch = find_batch()
            if not batch:
                break
            settle_batch(batch)
            settle_singles()

        for ob in queue:
            statuses = [route_status(r, a) for r, a in eligible[ob.id]]
            reasons = [] if "AVAILABLE" in statuses else sorted(set(statuses))
            if ob.payer in internal and cash(ob.payer) + credit(ob.payer) - keep(ob) < ob.amount:
                reasons.append("INSUFFICIENT_CASH")
            out.reasons[(ob.id, hour)] = tuple(reasons)

        for participant in sorted(drawn):
            repay = min(drawn[participant], balance[participant])
            balance[participant] -= repay
            drawn[participant] -= repay
        out.balances.append(dict(balance))
        out.credit_drawn.append(dict(drawn))
    return out
