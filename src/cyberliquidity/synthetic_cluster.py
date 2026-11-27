"""Deterministic generator for the second synthetic scenario: a circular-trade cluster (48 hours).

Fictional and illustrative, like the default scenario: organisation names, balances, amounts,
capacities and the payment calendar were *constructed* - here so that payment *gridlock*
can be observed. They are not calibrated to any real market, firm or provider. Regenerate
the committed fixture with:

    cyberliquidity generate-default --which cluster --out scenarios/cluster_gridlock_48h.json

Structure (one currency, EUR, amounts in cents):

* Operational network: one cloud region (East Cloud) hosting PSP Gamma, through which the
  cluster's outside customers pay; Cluster Bank's on-premises e-banking channel, which the
  firms use for their own payments; a narrow host-to-host link that can serve as a backup
  collection channel; and Cluster Bank's core ledger.
* Financial network: eight fictional manufacturing firms on one industrial estate. Birch
  Assembly Works buys parts and services from the other seven and is also their landlord,
  so money flows in circles: invoices out of Birch, rent back into Birch, and the suppliers
  pay one another. Customers, employees, the tax authority and outside suppliers sit
  outside the accounting boundary.
* Calendar: two days (hours 0-47). Day 1 (hours 24-47) is the cluster's settlement day: a
  vehicle maker pays Birch at 09:00, the invoice run is at 10:00, rent at 11:00, tax at
  12:00, a steel delivery at 14:00 and payroll at 16:00.

Each firm holds much less cash than it owes on settlement day, and much of what it owes is
offset by what other firms in the cluster owe it. Normally the vehicle maker's payment
starts the chain of payments. If that payment is stuck, the cluster can lock up although
most firms would be net receivers, which is the case a multilateral offsetting rule (the
``offsetting`` intervention) is designed for.
"""

from __future__ import annotations

from .models import (
    BackupRouteIntervention,
    BufferAllocation,
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
)

EUR = Currency(code="EUR", minor_units=2)
HORIZON = 48


def eur(amount: int) -> int:
    """Whole euros to cents."""
    return amount * 100


SERVICES = (
    Service(id="cloud_east", name="East Cloud Region", kind="cloud",
            description="Fictional public-cloud region. Hosts PSP Gamma."),
    Service(id="psp_gamma", name="PSP Gamma", kind="payment_provider", capacity_per_hour=10,
            requires_all=("cloud_east",),
            description="Fictional payment service provider that collects the cluster's payments from outside customers."),
    Service(id="ebank_cluster", name="Cluster Bank e-banking", kind="channel", capacity_per_hour=30,
            description="Cluster Bank's on-premises e-banking channel; the firms send their own payments through it."),
    Service(id="h2h_cluster", name="Cluster Bank host-to-host link", kind="channel", capacity_per_hour=3,
            description="Narrow direct link for large corporate customers; can be opened as a backup collection channel."),
    Service(id="core_cluster", name="Cluster Bank core ledger", kind="settlement",
            description="Fictional bank that holds every cluster firm's account (book transfers)."),
)

ROUTES = (
    Route(id="gamma_collect", name="PSP Gamma → Cluster Bank", path=("psp_gamma", "core_cluster")),
    Route(id="ebank", name="Cluster Bank e-banking", path=("ebank_cluster", "core_cluster")),
    Route(id="h2h", name="Host-to-host link → Cluster Bank", path=("h2h_cluster", "core_cluster")),
)

PARTICIPANTS = (
    # (id, name, sector, opening cash in EUR)
    ("birch", "Birch Assembly Works", "Final assembly; landlord of the estate", 2_000),
    ("alder", "Alder Precision Parts", "Machining", 3_000),
    ("garnet", "Garnet Metals", "Metal stockholding", 4_000),
    ("fallow", "Fallow Freight", "Logistics", 2_000),
    ("ember", "Ember Toolroom", "Tooling", 1_000),
    ("dunlin", "Dunlin Mouldings", "Plastic mouldings", 3_000),
    ("heron", "Heron Harnesses", "Electrical subassemblies", 2_500),
    ("cobalt", "Cobalt Finishing", "Surface finishing", 3_000),
)
FIRMS = tuple(p[0] for p in PARTICIPANTS)
SUPPLIERS = FIRMS[1:]

EXTERNALS = (
    ExternalParty(id="ext_oem", name="Vehicle maker", description="Large customer of Birch outside the region."),
    ExternalParty(id="ext_wholesale", name="Spare-parts wholesaler", description="Customer of Heron and Alder."),
    ExternalParty(id="ext_employees", name="Employees", description="Payroll recipients."),
    ExternalParty(id="ext_tax", name="Tax authority", description="Tax payments."),
    ExternalParty(id="ext_mill", name="Steel mill", description="Outside supplier of Garnet Metals."),
    ExternalParty(id="ext_energy", name="Energy supplier", description="Electricity and gas for the estate."),
)

CUSTOMERS = ("ext_oem", "ext_wholesale")


def _flows() -> list[tuple[int, str, str, int, int, str]]:
    """(due_hour, payer, recipient, amount EUR, priority, purpose); priority 1 = most urgent.

    The supplier ring (day 1, 10:00) is the gridlock-prone part: each supplier owes the
    next one more than it holds in cash, but its net position over the whole ring is close
    to zero. Normally Birch's invoice payments, funded by the vehicle maker at 09:00, give
    the suppliers the cash to start the ring.
    """
    return [
        # Day 0: an ordinary day
        (9, "ext_wholesale", "heron", 3_000, 3, "spare parts"),
        (10, "garnet", "ext_mill", 3_000, 3, "steel"),
        (11, "alder", "garnet", 2_000, 3, "bar stock"),
        (12, "ext_wholesale", "alder", 2_500, 3, "spare parts"),
        (13, "fallow", "ext_energy", 800, 3, "fuel"),
        (14, "dunlin", "ext_energy", 900, 3, "energy"),
        (15, "cobalt", "ext_energy", 600, 3, "energy"),
        (16, "ember", "garnet", 700, 3, "tool steel"),
        # Day 1, 09:00: outside customers pay through PSP Gamma
        (33, "ext_oem", "birch", 52_000, 2, "vehicle modules"),
        (33, "ext_wholesale", "heron", 4_000, 3, "spare parts"),
        (33, "ext_wholesale", "garnet", 9_000, 3, "scrap metal"),
        # Day 1, 10:00: Birch pays its suppliers ...
        (34, "birch", "alder", 14_000, 3, "machined parts"),
        (34, "birch", "heron", 9_000, 3, "wiring harnesses"),
        (34, "birch", "dunlin", 8_000, 3, "moulded housings"),
        (34, "birch", "cobalt", 6_000, 3, "coating"),
        (34, "birch", "fallow", 5_000, 3, "freight"),
        (34, "birch", "garnet", 4_000, 3, "sheet metal"),
        (34, "birch", "ember", 3_000, 3, "fixtures"),
        # ... and the suppliers pay one another round a ring
        (34, "alder", "garnet", 12_000, 3, "bar stock"),
        (34, "garnet", "fallow", 13_000, 3, "deliveries"),
        (34, "fallow", "ember", 10_000, 3, "fleet tooling"),
        (34, "ember", "dunlin", 9_000, 3, "mould inserts"),
        (34, "dunlin", "heron", 11_000, 3, "connector housings"),
        (34, "heron", "cobalt", 10_000, 3, "cable-gland coating"),
        (34, "cobalt", "alder", 12_000, 3, "machined fixtures"),
        # Day 1, 11:00: rent to Birch, the estate's landlord
        (35, "alder", "birch", 3_000, 2, "unit rent"),
        (35, "garnet", "birch", 3_000, 2, "unit rent"),
        (35, "heron", "birch", 2_500, 2, "unit rent"),
        (35, "dunlin", "birch", 2_500, 2, "unit rent"),
        (35, "fallow", "birch", 2_000, 2, "yard rent"),
        (35, "cobalt", "birch", 2_000, 2, "unit rent"),
        (35, "ember", "birch", 1_500, 2, "unit rent"),
        # Day 1, 12:00 tax and 14:00 a steel delivery
        (36, "birch", "ext_tax", 5_000, 1, "tax"),
        (36, "garnet", "ext_tax", 2_000, 1, "tax"),
        (38, "garnet", "ext_mill", 6_000, 2, "steel"),
        # Day 1, 16:00: payroll
        (40, "birch", "ext_employees", 12_000, 1, "payroll"),
        (40, "alder", "ext_employees", 5_000, 1, "payroll"),
        (40, "heron", "ext_employees", 4_000, 1, "payroll"),
        (40, "dunlin", "ext_employees", 3_500, 1, "payroll"),
        (40, "fallow", "ext_employees", 3_000, 1, "payroll"),
        (40, "garnet", "ext_employees", 3_000, 1, "payroll"),
        (40, "cobalt", "ext_employees", 2_500, 1, "payroll"),
        (40, "ember", "ext_employees", 2_000, 1, "payroll"),
        # Day 1, 17:00: energy for the estate
        (41, "birch", "ext_energy", 1_500, 3, "energy"),
        (41, "garnet", "ext_energy", 600, 3, "energy"),
    ]


def build_cluster_scenario() -> Scenario:
    rows = sorted(_flows(), key=lambda r: (r[0], r[4], r[1], r[2], r[5]))
    obligations = tuple(
        Obligation(
            id=f"CB{i:03d}",
            payer=payer,
            recipient=recipient,
            amount=eur(amount),
            due_hour=hour,
            priority=priority,
            # outside customers pay through PSP Gamma; the firms send everything through e-banking
            routes=("gamma_collect",) if payer in CUSTOMERS else ("ebank",),
            purpose=purpose,
        )
        for i, (hour, payer, recipient, amount, priority, purpose) in enumerate(rows, start=1)
    )
    disruption = Disruption(
        id="D1", service="cloud_east", start_hour=32, duration_hours=12, severity_pct=100,
        description="Illustrative 12-hour outage of the East Cloud region, day 1 08:00-20:00.",
    )
    candidates = (
        BackupRouteIntervention(
            id="backup_h2h", primary_routes=("gamma_collect",), backup_route="h2h", active_from_hour=35,
            description="Contingency arrangement: customers may pay through the host-to-host link 3 h after the outage starts.",
        ),
        LiquidityBufferIntervention(
            id="buffer_birch", allocations=(BufferAllocation(participant="birch", amount=eur(20_000)),), funding_hour=0,
            description="Pre-positioned buffer: EUR 20,000 for Birch Assembly Works at hour 0.",
        ),
        FasterRecoveryIntervention(
            id="faster_recovery", disruption="D1", duration_reduction_hours=6,
            description="Improved recovery runbooks: the outage lasts 6 h less.",
        ),
        OffsettingIntervention(
            id="offsetting", active_from_hour=0,
            description="Cluster Bank runs multilateral offsetting of queued payments every hour.",
        ),
        PriorityReserveIntervention(
            id="reserve_payroll", participants=None, protected_priority=1, lookahead_hours=24,
            description="Every firm holds back routine payments to keep cash for payroll and tax due within 24 h.",
        ),
    )
    return Scenario(
        id="cluster-gridlock-48h",
        name="Illustrative circular-trade cluster, 48 hours",
        description=(
            "Eight fictional manufacturing firms on one industrial estate that buy from, rent from and deliver to one "
            "another, a payment service provider in one cloud region, and one local bank. Synthetic and "
            "uncalibrated; constructed so that payment gridlock can occur when an outside customer's payment is stuck."
        ),
        synthetic=True,
        currency=EUR,
        horizon_hours=HORIZON,
        services=SERVICES,
        routes=ROUTES,
        participants=tuple(Participant(id=i, name=n, sector=s, initial_balance=eur(o)) for i, n, s, o in PARTICIPANTS),
        external_parties=EXTERNALS,
        obligations=obligations,
        disruptions=(disruption,),
        interventions=(),
        candidate_interventions=candidates,
    )
