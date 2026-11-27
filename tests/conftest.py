from __future__ import annotations

from pathlib import Path

import pytest

from cyberliquidity.models import Scenario

ROOT = Path(__file__).resolve().parents[1]
SCENARIOS = ROOT / "scenarios"


def load(name: str) -> Scenario:
    return Scenario.model_validate_json((SCENARIOS / name).read_text(encoding="utf-8"))


@pytest.fixture
def three() -> Scenario:
    return load("three_participant.json")


def tiny(**overrides) -> dict:
    """A minimal valid scenario as a plain dict, for building edge-case tests."""
    base = {
        "id": "tiny",
        "name": "tiny",
        "currency": {"code": "XTS", "minor_units": 0},
        "horizon_hours": 6,
        "services": [{"id": "svc", "name": "Service", "kind": "payment_provider", "capacity_per_hour": 10}],
        "routes": [{"id": "r", "name": "Route", "path": ["svc"]}],
        "participants": [
            {"id": "P", "name": "P", "initial_balance": 1000},
            {"id": "Q", "name": "Q", "initial_balance": 0},
        ],
        "external_parties": [{"id": "X", "name": "Outside"}],
        "obligations": [],
    }
    base.update(overrides)
    return base
