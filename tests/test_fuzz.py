"""Property-based fuzzing (Hypothesis) of the protective layers.

Whatever bytes arrive, the loader either returns a valid scenario or raises
``ScenarioInputError`` with readable lines: never another exception. Random networks
always pass the independent integrity audit. Exported cells never start a formula.
"""

from __future__ import annotations

import json
import re

import pytest

hypothesis = pytest.importorskip("hypothesis")
from hypothesis import HealthCheck, given, settings  # noqa: E402
from hypothesis import strategies as st  # noqa: E402

from cyberliquidity import simulate  # noqa: E402
from cyberliquidity.audit import audit  # noqa: E402
from cyberliquidity.generator import random_scenario  # noqa: E402
from cyberliquidity.models import Scenario  # noqa: E402
from cyberliquidity.safety import APP_LIMITS, ScenarioInputError, parse_scenario, safe_cell  # noqa: E402
from tests.conftest import SCENARIOS  # noqa: E402

FAST = settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.too_slow])
BASE = json.loads((SCENARIOS / "three_participant.json").read_text(encoding="utf-8"))


def _load_or_refuse(data) -> Scenario | None:
    try:
        return parse_scenario(data, APP_LIMITS, source="fuzz.json")
    except ScenarioInputError as exc:
        assert exc.title.startswith("fuzz.json ")
        assert all(isinstance(line, str) and line for line in exc.problems)
        return None


@FAST
@given(st.binary(max_size=2_000))
def test_arbitrary_bytes_never_crash_the_loader(data):
    _load_or_refuse(data)


@FAST
@given(st.text(max_size=2_000))
def test_arbitrary_text_never_crashes_the_loader(text):
    _load_or_refuse(text)


JSON_VALUES = st.recursive(
    st.none() | st.booleans() | st.integers() | st.floats(allow_nan=False) | st.text(max_size=30),
    lambda inner: st.lists(inner, max_size=4) | st.dictionaries(st.text(max_size=10), inner, max_size=4),
    max_leaves=20,
)


def _paths(node, prefix=()):
    yield prefix
    if isinstance(node, dict):
        for key, value in node.items():
            yield from _paths(value, (*prefix, key))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from _paths(value, (*prefix, index))


PATHS = [p for p in _paths(BASE) if p]


@FAST
@given(st.sampled_from(PATHS), JSON_VALUES)
def test_any_field_replaced_by_any_json_value_is_loaded_or_refused_cleanly(path, value):
    data = json.loads(json.dumps(BASE))
    node = data
    for part in path[:-1]:
        node = node[part]
    node[path[-1]] = value
    scenario = _load_or_refuse(json.dumps(data))
    if scenario is not None:  # anything accepted must also simulate and pass the audit
        assert audit(simulate(scenario)).ok


@FAST
@given(st.sampled_from(PATHS))
def test_any_field_removed_is_loaded_or_refused_cleanly(path):
    data = json.loads(json.dumps(BASE))
    node = data
    for part in path[:-1]:
        node = node[part]
    del node[path[-1]]
    _load_or_refuse(json.dumps(data))


@settings(max_examples=40, deadline=None)
@given(st.integers(min_value=0, max_value=10**9))
def test_random_networks_always_pass_the_integrity_audit(seed):
    assert audit(simulate(random_scenario(seed))).ok


FORMULA = re.compile(r"^[=+\-@\t\r]")
NUMBER = re.compile(r"^[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?$")


@FAST
@given(st.text(max_size=40))
def test_neutralised_cells_never_start_a_formula_and_lose_nothing(text):
    cell = safe_cell(text)
    assert not FORMULA.match(cell) or NUMBER.match(cell)
    assert cell == text or cell == "'" + text
