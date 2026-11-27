"""Seeded random scenarios for structural testing, fuzzing and scale benchmarks.

``random_scenario(seed)`` builds a valid, labelled synthetic scenario with a random
acyclic service graph (required and any-of dependencies, optional capacity limits),
random routes, a random payment schedule, optional disruptions (outages, degradation
and recovery ramps), random interventions of every type and random engine settings.
The offsetting and priority-reserve interventions (added in 0.3) are drawn from a
separate random stream, so every other part of a seed's scenario is unchanged.

These networks have no economic meaning. They exist to exercise the rules: the test
suite checks that the engine agrees with an independent reference implementation on
hundreds of them, and that every invariant holds. Nothing here samples incident
probabilities; the seed only picks a structure.

    cyberliquidity generate --seed 7 --participants 40 --obligations 800 --out scenarios/random_7.json
"""

from __future__ import annotations

import random

from .models import (
    DISCLAIMER,
    BackupRouteIntervention,
    BufferAllocation,
    CreditFacility,
    CreditLineIntervention,
    Currency,
    Disruption,
    ExternalParty,
    FasterRecoveryIntervention,
    LiquidityBufferIntervention,
    Obligation,
    OffsettingIntervention,
    Participant,
    PriorityReserveIntervention,
    Route,
    Scenario,
    Service,
    ServiceKind,
    SimulationSettings,
)


def random_scenario(seed: int, participants: int = 8, services: int = 6, routes: int = 3, obligations: int = 60,
                    horizon: int = 24, externals: int = 2, max_disruptions: int = 2, interventions: bool = True,
                    settings: bool = True, max_amount: int = 300, max_balance: int = 400) -> Scenario:
    rng = random.Random(seed)
    kinds = list(ServiceKind)

    service_list: list[Service] = []
    for i in range(max(1, services)):
        earlier = [f"s{j}" for j in range(i)]
        required = sorted(rng.sample(earlier, k=rng.randint(0, min(2, len(earlier))))) if earlier else []
        rest = [e for e in earlier if e not in required]
        groups: tuple[tuple[str, ...], ...] = ()
        if len(rest) >= 2 and rng.random() < 0.35:
            groups = (tuple(sorted(rng.sample(rest, 2))),)
        capacity = rng.randint(1, 8) if rng.random() < 0.6 else None
        service_list.append(Service(id=f"s{i}", name=f"Service {i}", kind=rng.choice(kinds), capacity_per_hour=capacity,
                                    requires_all=tuple(required), requires_any=groups))

    service_ids = [s.id for s in service_list]
    route_list = [
        Route(id=f"r{i}", name=f"Route {i}", path=tuple(rng.sample(service_ids, k=rng.randint(1, min(3, len(service_ids))))))
        for i in range(max(1, routes))
    ]
    route_ids = [r.id for r in route_list]

    participant_list = [
        Participant(id=f"p{i}", name=f"Firm {i}", initial_balance=0 if rng.random() < 0.25 else rng.randint(1, max_balance))
        for i in range(max(1, participants))
    ]
    external_list = [ExternalParty(id=f"x{i}", name=f"Outside party {i}") for i in range(externals)]
    internal = [p.id for p in participant_list]
    parties = internal + [x.id for x in external_list]

    obligation_list: list[Obligation] = []
    for i in range(obligations):
        payer = rng.choice(parties)
        options = [p for p in parties if p != payer and not (payer not in internal and p not in internal)]
        if not options:
            continue
        recipient = rng.choice(options)
        obligation_list.append(Obligation(
            id=f"o{i}", payer=payer, recipient=recipient, amount=rng.randint(1, max_amount),
            due_hour=rng.randrange(horizon), priority=rng.randint(1, 5),
            routes=tuple(rng.sample(route_ids, k=rng.randint(1, min(2, len(route_ids))))),
        ))

    disruption_list = []
    for i in range(rng.randint(0, max_disruptions)):
        recovery = rng.choice([0, 0, rng.randint(1, 3)])
        disruption_list.append(Disruption(
            id=f"D{i + 1}", service=rng.choice(service_ids), start_hour=rng.randrange(horizon),
            duration_hours=rng.randint(0, max(1, horizon // 2)), severity_pct=rng.choice([100, 100, 75, 40]),
            recovery_hours=recovery, recovery_capacity_pct=rng.choice([25, 50, 80]),
        ))

    applied = []
    if interventions:
        if len(route_ids) >= 2 and rng.random() < 0.5:
            primary = rng.sample(route_ids, k=1)
            backup = rng.choice([r for r in route_ids if r not in primary])
            payers = tuple(sorted(rng.sample(parties, k=min(len(parties), rng.randint(1, 3))))) if rng.random() < 0.3 else None
            applied.append(BackupRouteIntervention(id="backup", primary_routes=tuple(primary), backup_route=backup,
                                                   active_from_hour=rng.randrange(horizon), payers=payers))
        if rng.random() < 0.5:
            chosen = rng.sample(internal, k=min(len(internal), rng.randint(1, 3)))
            applied.append(LiquidityBufferIntervention(
                id="buffer", allocations=tuple(BufferAllocation(participant=p, amount=rng.randint(1, max_amount))
                                               for p in sorted(chosen)),
                funding_hour=rng.randrange(horizon)))
        if disruption_list and rng.random() < 0.5:
            target = rng.choice(disruption_list)
            applied.append(FasterRecoveryIntervention(id="faster_recovery", disruption=target.id,
                                                      duration_reduction_hours=rng.randint(1, 5)))
        if rng.random() < 0.5:
            chosen = rng.sample(internal, k=min(len(internal), rng.randint(1, 3)))
            applied.append(CreditLineIntervention(
                id="credit", facilities=tuple(CreditFacility(participant=p, limit=rng.randint(1, max_amount))
                                              for p in sorted(chosen)),
                available_from_hour=rng.randrange(horizon)))
        extra = random.Random(f"cyberliquidity-0.3:{seed}")
        if extra.random() < 0.4:
            applied.append(OffsettingIntervention(id="offsetting", active_from_hour=extra.choice([0, 0, extra.randrange(horizon)])))
        if extra.random() < 0.35:
            covered = None if extra.random() < 0.5 else tuple(sorted(extra.sample(internal, k=min(len(internal), extra.randint(1, 3)))))
            applied.append(PriorityReserveIntervention(
                id="reserve", participants=covered, protected_priority=extra.randint(1, 3),
                lookahead_hours=extra.randint(1, max(1, horizon // 2)), active_from_hour=extra.choice([0, extra.randrange(horizon)])))
    chosen_settings = SimulationSettings(
        ordering=rng.choice(["priority_due_id", "due_id"]) if settings else "priority_due_id",
        liquidity_recycling=rng.choice(["within_step", "next_step"]) if settings else "within_step",
    )
    return Scenario(
        id=f"random-{seed}",
        name=f"Random structural test scenario (seed {seed})",
        description="Randomly generated structure for testing the rules; no economic meaning. " + DISCLAIMER,
        currency=Currency(code="XTS", minor_units=0),
        horizon_hours=horizon,
        services=tuple(service_list),
        routes=tuple(route_list),
        participants=tuple(participant_list),
        external_parties=tuple(external_list),
        obligations=tuple(obligation_list),
        disruptions=tuple(disruption_list),
        interventions=tuple(applied),
        settings=chosen_settings,
    )
