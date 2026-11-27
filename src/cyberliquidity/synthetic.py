"""Deterministic generator for the default synthetic 72-hour scenario.

Everything here is fictional and illustrative: organisation names, balances, amounts,
capacities and the payment calendar were *constructed* so that the two transmission
mechanisms can be observed. They are not calibrated to any real market, firm or
provider. Regenerate the committed fixture with:

    cyberliquidity generate-default --out scenarios/default_72h.json

Structure (one currency, EUR, amounts in cents):

* Operational network: two cloud regions (Nimbus, Stratus), one identity provider
  replicated across both regions (fails over), two payment service providers (PSP
  Alpha on Nimbus, PSP Beta on Stratus; both use the identity provider), two
  settlement banks (North, South) and North's high-value channel (on-premises,
  uses the identity provider).
* Financial network: 12 fictional businesses. Three larger ones bank through PSP
  Alpha; nine smaller, thinly funded ones through PSP Beta. External parties
  (households, employees, tax authority, suppliers, a lender, export customers and
  a public-sector customer) sit outside the accounting boundary.
* Calendar: 3 business days (hours 0-71). Day 1 (hours 24-47) is payroll day for
  the producers. Payments are scheduled on whole hours between 07:00 and 18:00.
"""

from __future__ import annotations

from dataclasses import dataclass

from .models import (
    BackupRouteIntervention,
    BufferAllocation,
    Currency,
    Disruption,
    ExternalParty,
    FasterRecoveryIntervention,
    LiquidityBufferIntervention,
    Obligation,
    Participant,
    Route,
    Scenario,
    Service,
)

EUR = Currency(code="EUR", minor_units=2)
HORIZON = 72
DAYS = (0, 1, 2)


def eur(amount: int) -> int:
    """Whole euros to cents."""
    return amount * 100


SERVICES = (
    Service(id="cloud_nimbus", name="Nimbus Cloud", kind="cloud",
            description="Fictional public-cloud region. Hosts PSP Alpha."),
    Service(id="cloud_stratus", name="Stratus Cloud", kind="cloud",
            description="Fictional public-cloud region. Hosts PSP Beta."),
    Service(id="idp_keygate", name="KeyGate Identity", kind="identity",
            requires_any=(("cloud_nimbus", "cloud_stratus"),),
            description="Fictional identity/SSO provider replicated in both cloud regions; survives the loss of one region."),
    Service(id="psp_alpha", name="PSP Alpha", kind="payment_provider", capacity_per_hour=10,
            requires_all=("cloud_nimbus", "idp_keygate"),
            description="Fictional payment service provider used by the three larger businesses."),
    Service(id="psp_beta", name="PSP Beta", kind="payment_provider", capacity_per_hour=12,
            requires_all=("cloud_stratus", "idp_keygate"),
            description="Fictional payment service provider used by the nine smaller businesses."),
    Service(id="portal_north", name="North High-Value Channel", kind="channel", capacity_per_hour=6,
            requires_all=("idp_keygate",),
            description="Settlement Bank North's on-premises high-value payment channel (uses KeyGate for sign-on)."),
    Service(id="settle_north", name="Settlement Bank North", kind="settlement", capacity_per_hour=40,
            description="Fictional settlement institution for PSP Alpha and the high-value channel."),
    Service(id="settle_south", name="Settlement Bank South", kind="settlement", capacity_per_hour=40,
            description="Fictional settlement institution for PSP Beta."),
)

ROUTES = (
    Route(id="alpha_north", name="PSP Alpha → Settlement Bank North", path=("psp_alpha", "settle_north")),
    Route(id="beta_south", name="PSP Beta → Settlement Bank South", path=("psp_beta", "settle_south")),
    Route(id="hv_north", name="North High-Value Channel → Settlement Bank North", path=("portal_north", "settle_north")),
)


@dataclass(frozen=True)
class _P:
    id: str
    name: str
    sector: str
    opening: int  # EUR
    route: str


PARTICIPANTS = (
    _P("granite", "Granite Wholesale", "Wholesale distribution", 140_000, "alpha_north"),
    _P("meridian", "Meridian Distribution", "Wholesale distribution", 12_000, "alpha_north"),
    _P("larkspur", "Larkspur Grocers", "Retail", 90_000, "alpha_north"),
    _P("oakridge", "Oakridge Dairy", "Dairy", 4_000, "beta_south"),
    _P("silverfern", "Silverfern Bakery", "Bakery", 10_000, "beta_south"),
    _P("thistle", "Thistle Mill", "Flour milling", 8_500, "beta_south"),
    _P("copperleaf", "Copperleaf Farms", "Agriculture", 3_000, "beta_south"),
    _P("harrow", "Harrow Packaging", "Manufacturing", 4_000, "beta_south"),
    _P("swiftlane", "Swiftlane Logistics", "Logistics", 4_000, "beta_south"),
    _P("brightwater", "Brightwater Cafés", "Hospitality", 9_000, "beta_south"),
    _P("kestrel", "Kestrel Facilities", "Property services", 10_000, "beta_south"),
    _P("bluefin", "Bluefin IT Services", "IT services", 7_000, "beta_south"),
)

EXTERNALS = (
    ExternalParty(id="ext_households", name="Card-paying households", description="Retail customers; card settlements."),
    ExternalParty(id="ext_agency", name="Regional Food Agency", description="Public-sector customer of Meridian (outside the boundary)."),
    ExternalParty(id="ext_export", name="Export customers", description="Foreign buyers of Granite Wholesale."),
    ExternalParty(id="ext_employees", name="Employees", description="Payroll recipients."),
    ExternalParty(id="ext_tax", name="Tax authority", description="Tax payments."),
    ExternalParty(id="ext_suppliers", name="Outside suppliers", description="Raw materials, fuel, maintenance, subscriptions."),
    ExternalParty(id="ext_lender", name="Lender", description="Loan repayments."),
)

PRODUCERS = ("oakridge", "silverfern", "thistle", "copperleaf")


def _flows() -> list[tuple[int, str, str, int, int, str]]:
    """(due_hour, payer, recipient, amount EUR, priority, purpose); priority 1 = most urgent.

    The bread supply chain is funded intraday from the top: the agency pays Meridian
    (07:00), Meridian pays the bakery (08:00), the bakery pays the mill (09:00) and the
    mill pays the farm (10:00). Each of these firms holds little cash overnight, so a
    delay at the top can travel down the chain although their own routes work.
    """
    f: list[tuple[int, str, str, int, int, str]] = []
    for d in DAYS:
        b = 24 * d
        f += [
            (b + 7, "ext_agency", "meridian", 24_000, 2, "contract payment"),
            (b + 8, "meridian", "silverfern", 22_000, 2, "bakery invoice"),
            (b + 8, "granite", "oakridge", 14_000, 2, "dairy invoice"),
            (b + 8, "granite", "copperleaf", 3_000, 2, "produce invoice"),
            (b + 9, "silverfern", "thistle", 14_000, 2, "flour invoice"),
            (b + 9, "silverfern", "oakridge", 3_000, 2, "milk invoice"),
            (b + 9, "larkspur", "granite", 18_000, 2, "supplier invoice"),
            (b + 10, "thistle", "copperleaf", 9_000, 2, "grain invoice"),
            (b + 10, "ext_export", "granite", 30_000, 3, "export receipt"),
            (b + 11, "oakridge", "ext_suppliers", 4_000, 3, "animal feed"),
            (b + 11, "silverfern", "ext_suppliers", 2_000, 3, "energy"),
            (b + 11, "thistle", "ext_suppliers", 1_500, 3, "energy"),
            (b + 11, "copperleaf", "ext_suppliers", 3_000, 3, "seed and fuel"),
            (b + 11, "brightwater", "silverfern", 2_000, 2, "bakery invoice"),
            (b + 11, "brightwater", "meridian", 3_000, 2, "supplier invoice"),
            (b + 12, "ext_households", "larkspur", 20_000, 3, "card settlement"),
            (b + 12, "ext_households", "brightwater", 4_000, 3, "card settlement"),
            (b + 12, "oakridge", "harrow", 1_500, 3, "packaging"),
            (b + 12, "silverfern", "harrow", 1_500, 3, "packaging"),
            (b + 12, "thistle", "harrow", 1_000, 3, "packaging"),
            (b + 12, "copperleaf", "harrow", 1_000, 3, "packaging"),
            (b + 13, "larkspur", "meridian", 4_000, 2, "supplier invoice"),
            (b + 13, "harrow", "ext_suppliers", 2_500, 3, "materials"),
            (b + 14, "granite", "swiftlane", 4_000, 3, "logistics fee"),
            (b + 14, "meridian", "swiftlane", 3_000, 3, "logistics fee"),
            (b + 14, "larkspur", "swiftlane", 4_000, 3, "logistics fee"),
            (b + 16, "swiftlane", "ext_suppliers", 2_500, 3, "fuel"),
            (b + 18, "ext_households", "larkspur", 25_000, 3, "card settlement"),
            (b + 18, "ext_households", "brightwater", 5_000, 3, "card settlement"),
        ]
        f += [(b + 14, p, "swiftlane", 1_000, 3, "logistics fee") for p in PRODUCERS]
        f += [(b + 14, p, "swiftlane", 800, 3, "logistics fee") for p in ("brightwater", "harrow")]

    # Day 0, 09:00: monthly rent to Kestrel Facilities
    f += [(9, p, "kestrel", 8_000, 2, "rent") for p in ("granite", "meridian", "larkspur")]
    f += [(9, p, "kestrel", 2_000, 2, "rent")
          for p in ("oakridge", "silverfern", "thistle", "copperleaf", "brightwater", "swiftlane", "bluefin", "harrow")]

    # Day 1: subscriptions, maintenance, IT fees and the small firms' payroll at 15:00-16:00
    f += [
        (33, "bluefin", "ext_suppliers", 3_000, 3, "cloud subscription"),
        (35, "kestrel", "ext_suppliers", 6_000, 3, "maintenance"),
        (38, "granite", "bluefin", 5_000, 3, "IT services"),
        (38, "meridian", "bluefin", 2_000, 3, "IT services"),
        (38, "larkspur", "bluefin", 5_000, 3, "IT services"),
        (38, "swiftlane", "bluefin", 1_500, 3, "IT services"),
        (38, "kestrel", "bluefin", 1_000, 3, "IT services"),
        (39, "oakridge", "ext_employees", 14_000, 1, "payroll"),
        (39, "silverfern", "ext_employees", 12_000, 1, "payroll"),
        (39, "thistle", "ext_employees", 9_000, 1, "payroll"),
        (39, "copperleaf", "ext_employees", 12_000, 1, "payroll"),
        (39, "harrow", "ext_employees", 5_000, 1, "payroll"),
        (40, "swiftlane", "ext_employees", 14_000, 1, "payroll"),
    ]

    # Day 2: the larger firms' payroll and taxes; the remaining payrolls at 15:00
    f += [
        (57, "granite", "ext_employees", 40_000, 1, "payroll"),
        (57, "larkspur", "ext_employees", 30_000, 1, "payroll"),
        (58, "kestrel", "ext_lender", 20_000, 1, "loan repayment"),
        (60, "granite", "ext_tax", 10_000, 1, "tax"),
        (60, "larkspur", "ext_tax", 10_000, 1, "tax"),
        (61, "meridian", "ext_employees", 20_000, 1, "payroll"),
        (63, "kestrel", "ext_employees", 6_000, 1, "payroll"),
        (63, "bluefin", "ext_employees", 12_000, 1, "payroll"),
        (63, "brightwater", "ext_employees", 6_000, 1, "payroll"),
    ]
    return f


def _route_for(payer: str, recipient: str, purpose: str, routes: dict[str, str]) -> str:
    if purpose == "contract payment":
        return "hv_north"  # the public-sector customer pays through North's high-value channel
    if payer in routes:
        return routes[payer]  # payments are processed by the payer's provider
    return routes[recipient]  # external payers: the recipient's collection route


def build_default_scenario() -> Scenario:
    routes = {p.id: p.route for p in PARTICIPANTS}
    rows = sorted(_flows(), key=lambda r: (r[0], r[4], r[1], r[2], r[5]))
    obligations = tuple(
        Obligation(
            id=f"OB{i:04d}",
            payer=payer,
            recipient=recipient,
            amount=eur(amount),
            due_hour=hour,
            priority=priority,
            routes=(_route_for(payer, recipient, purpose, routes),),
            purpose=purpose,
        )
        for i, (hour, payer, recipient, amount, priority, purpose) in enumerate(rows, start=1)
    )
    disruption = Disruption(
        id="D1", service="cloud_nimbus", start_hour=32, duration_hours=12, severity_pct=100,
        description="Illustrative 12-hour outage of the Nimbus Cloud region, day 1 08:00-20:00.",
    )
    candidates = (
        BackupRouteIntervention(
            id="backup_beta", primary_routes=("alpha_north",), backup_route="beta_south",
            active_from_hour=35,
            description="Contingency arrangement: Alpha's clients may route through PSP Beta 3 h after the outage starts.",
        ),
        LiquidityBufferIntervention(
            id="buffer_producers",
            allocations=tuple(BufferAllocation(participant=p, amount=eur(10_000)) for p in PRODUCERS),
            funding_hour=0,
            description="Pre-positioned buffer: EUR 10,000 for each of the four producers at hour 0.",
        ),
        FasterRecoveryIntervention(
            id="faster_recovery", disruption="D1", duration_reduction_hours=6,
            description="Improved recovery runbooks: the outage lasts 6 h less.",
        ),
    )
    return Scenario(
        id="default-72h",
        name="Illustrative regional supply chain, 72 hours",
        description=(
            "Twelve fictional businesses, two payment service providers, two settlement banks, a high-value channel, "
            "two cloud regions and a shared identity provider. Synthetic and uncalibrated; constructed so that both "
            "direct operational blockage and financial propagation can occur."
        ),
        synthetic=True,
        currency=EUR,
        horizon_hours=HORIZON,
        services=SERVICES,
        routes=ROUTES,
        participants=tuple(
            Participant(id=p.id, name=p.name, sector=p.sector, initial_balance=eur(p.opening)) for p in PARTICIPANTS
        ),
        external_parties=EXTERNALS,
        obligations=obligations,
        disruptions=(disruption,),
        interventions=(),
        candidate_interventions=candidates,
    )
