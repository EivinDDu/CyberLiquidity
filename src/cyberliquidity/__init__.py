"""CyberLiquidity: deterministic simulation of how cyber-related operational disruptions
propagate into payment delays, liquidity shortfalls and financial contagion.

Research prototype operating on synthetic data. Results are conditional on the model's
assumptions (docs/MODEL.md) and are not forecasts of real incidents.
"""

from .engine import (
    ENGINE_VERSION,
    BlockRecord,
    Event,
    InvariantViolation,
    LedgerEntry,
    Mechanism,
    ObligationOutcome,
    Reason,
    SimulationResult,
    Status,
    compile_scenario,
    simulate,
)
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
    SimulationSettings,
    format_money,
)
from .operational import OperationalModel, ServiceState

__version__ = "0.4.0"

__all__ = [
    "DISCLAIMER",
    "ENGINE_VERSION",
    "BackupRouteIntervention",
    "BlockRecord",
    "BufferAllocation",
    "CreditFacility",
    "CreditLineIntervention",
    "Currency",
    "Disruption",
    "Event",
    "ExternalParty",
    "FasterRecoveryIntervention",
    "InvariantViolation",
    "LedgerEntry",
    "LiquidityBufferIntervention",
    "Mechanism",
    "Obligation",
    "ObligationOutcome",
    "OffsettingIntervention",
    "OperationalModel",
    "Participant",
    "PriorityReserveIntervention",
    "Reason",
    "Route",
    "Scenario",
    "Service",
    "ServiceState",
    "SimulationResult",
    "SimulationSettings",
    "Status",
    "compile_scenario",
    "format_money",
    "simulate",
]
