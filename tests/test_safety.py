"""Protective layers: plain-text fields, the scenario input guard, CSV formula neutralising, the integrity audit."""

from __future__ import annotations

import csv
import io
import json
from dataclasses import replace as dc_replace

import pytest
from pydantic import ValidationError

from cyberliquidity import simulate
from cyberliquidity.audit import audit
from cyberliquidity.cli import main
from cyberliquidity.export import rows_to_csv
from cyberliquidity.generator import random_scenario
from cyberliquidity.models import Scenario
from cyberliquidity.safety import APP_LIMITS, CLI_LIMITS, Limits, ScenarioInputError, parse_scenario, safe_cell
from cyberliquidity.variants import stress
from tests.conftest import ROOT, SCENARIOS, load, tiny

BUILT_IN = sorted(p.name for p in SCENARIOS.glob("*.json"))


# ------------------------------------------------------------------ plain-text fields


def _with(path: list, value) -> dict:
    data = json.loads((SCENARIOS / "three_participant.json").read_text(encoding="utf-8"))
    node = data
    for part in path[:-1]:
        node = node[part]
    node[path[-1]] = value
    return data


@pytest.mark.parametrize(("path", "value"), [
    (["participants", 0, "name"], "<img src=x onerror=alert(1)>"),
    (["participants", 0, "name"], "[click me](https://example.invalid)"),
    (["participants", 0, "name"], "`code`"),
    (["participants", 0, "name"], "back\\slash"),
    (["participants", 0, "name"], "Alpha‮ahplA"),  # right-to-left override (spoofing)
    (["services", 0, "name"], "PSP​Alpha"),  # zero-width space
    (["services", 0, "name"], "bell\x07"),
    (["obligations", 0, "purpose"], "see https://example.invalid"),
    (["routes", 0, "name"], ""),
    (["routes", 0, "name"], "x" * 121),
    (["description"], "x" * 4001),
    (["description"], "hidden⁦text"),
])
def test_names_and_texts_must_be_plain_text(path, value):
    with pytest.raises(ValidationError):
        Scenario.model_validate(_with(path, value))


@pytest.mark.parametrize(("path", "value"), [
    (["participants", 0, "name"], "Café Ærø & Søn (Nord) 🌲"),
    (["participants", 0, "name"], "Smith-Jones Ltd. #2, 50% owned"),
    (["description"], "Line one.\n\tLine two, with *emphasis* and 100 %."),
])
def test_ordinary_text_is_accepted(path, value):
    Scenario.model_validate(_with(path, value))


@pytest.mark.parametrize("name", BUILT_IN)
def test_built_in_scenarios_pass_every_gate(name):
    data = (SCENARIOS / name).read_bytes()
    assert parse_scenario(data, APP_LIMITS) == load(name)
    assert parse_scenario(data.decode("utf-8"), CLI_LIMITS) == load(name)


# ------------------------------------------------------------------ input guard


def _refusal(data, limits=CLI_LIMITS) -> ScenarioInputError:
    with pytest.raises(ScenarioInputError) as caught:
        parse_scenario(data, limits, source="upload.json")
    return caught.value


def test_oversized_files_are_refused_before_parsing():
    err = _refusal(b"{" + b" " * 2_000, Limits(max_bytes=1_000))
    assert err.title == "upload.json is too large" and "1,000 bytes" in err.problems[0]


@pytest.mark.parametrize("data", [b"", b"   \n", "  "])
def test_empty_files_are_refused(data):
    assert _refusal(data).title == "upload.json is empty"


def test_non_utf8_files_are_refused_with_the_byte_position():
    err = _refusal(b'{"id": "\xff\xfe"}')
    assert err.title == "upload.json is not UTF-8 text" and "position 8" in err.problems[0]


def test_a_byte_order_mark_is_tolerated():
    data = (SCENARIOS / "three_participant.json").read_bytes()
    assert parse_scenario(b"\xef\xbb\xbf" + data) == load("three_participant.json")


@pytest.mark.parametrize("data", [b"not json", b"[1, 2]", b'{"id": 5}', b"{" * 5000, b'{"a": 1e999999}'])
def test_malformed_files_get_a_list_of_readable_problems(data):
    err = _refusal(data)
    assert err.title == "upload.json is not a valid scenario"
    assert err.problems and all(isinstance(line, str) and len(line) < 400 for line in err.problems)


def test_problem_lists_do_not_echo_the_input():
    secret = "x" * 50 + "SECRET-LOOKING-TEXT"
    err = _refusal(json.dumps(_with(["participants", 0, "name"], "<b>" + secret + "</b>")).encode())
    assert "participants.0.name" in " ".join(err.problems)
    assert secret not in str(err) and secret not in " ".join(err.problems)


def test_problem_lists_are_capped():
    data = tiny()
    data["obligations"] = [{"id": f"o{i}", "payer": "nobody", "recipient": "nobody", "amount": -1, "due_hour": 99,
                            "priority": 9, "route": "none"} for i in range(60)]
    err = _refusal(json.dumps(data).encode())
    assert len(err.problems) == 41 and err.problems[-1].startswith("... and ")


def test_cross_reference_problems_are_listed_one_per_line():
    data = tiny(obligations=[{"id": "o1", "payer": "P", "recipient": "Q", "amount": 5, "due_hour": 1,
                              "routes": ["missing_route"]}])
    err = _refusal(json.dumps(data).encode())
    assert any("missing_route" in line for line in err.problems)
    assert not any(line.startswith("Value error") for line in err.problems)


def test_the_app_refuses_scenarios_too_large_to_simulate_interactively():
    big = random_scenario(3, participants=40, services=12, routes=6, obligations=3_200, horizon=72)
    text = big.model_dump_json()
    err = _refusal(text, APP_LIMITS)
    assert err.title == "upload.json is larger than this tool accepts here"
    assert any("3,200 obligations" in line for line in err.problems)
    assert parse_scenario(text, CLI_LIMITS) == big  # the command line accepts it
    assert parse_scenario(text, None) == big  # and trusted callers can skip the limits


def test_limits_are_ordered_app_below_cli():
    for field in Limits.__dataclass_fields__:
        assert getattr(APP_LIMITS, field) <= getattr(CLI_LIMITS, field), field


# ------------------------------------------------------------------ command line


def test_cli_explains_a_missing_file(capsys):
    with pytest.raises(SystemExit) as stop:
        main(["validate", "no/such/file.json"])
    assert stop.value.code == 2
    err = capsys.readouterr().err
    assert err.startswith("Scenario not loaded: There is no file at no/such/file.json.") and "Traceback" not in err


def test_cli_explains_an_invalid_file(tmp_path, capsys):
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(_with(["participants", 0, "name"], "<script>")), encoding="utf-8")
    with pytest.raises(SystemExit) as stop:
        main(["validate", str(bad)])
    assert stop.value.code == 2
    err = capsys.readouterr().err
    assert "bad.json is not a valid scenario" in err and "participants.0.name: contains markup" in err


def test_cli_folder_and_allow_large(tmp_path, capsys):
    with pytest.raises(SystemExit):
        main(["validate", str(tmp_path)])
    assert "is a folder, not a scenario file" in capsys.readouterr().err
    assert main(["--allow-large", "validate", str(SCENARIOS / "three_participant.json")]) == 0


# ------------------------------------------------------------------ CSV formula neutralising


@pytest.mark.parametrize(("value", "expected"), [
    ("=HYPERLINK(\"http://x\")", "'=HYPERLINK(\"http://x\")"),
    ("+1+1", "'+1+1"),
    ("@SUM(A1)", "'@SUM(A1)"),
    ("-cmd", "'-cmd"),
    ("-", "'-"),
    ("\tTAB", "'\tTAB"),
    ("\rCR", "'\rCR"),
    ("-5", "-5"),
    ("+2.5", "+2.5"),
    ("-1.5e3", "-1.5e3"),
    ("plain", "plain"),
    ("", ""),
    (-5, -5),
    (2.5, 2.5),
    (None, None),
    (True, True),
])
def test_safe_cell(value, expected):
    assert safe_cell(value) == expected


def test_exported_csv_cells_cannot_start_a_formula():
    rows = [{"name": "=1+1", "amount": -500, "note": "-5", "text": "@x"}]
    parsed = list(csv.DictReader(io.StringIO(rows_to_csv(rows))))
    assert parsed == [{"name": "'=1+1", "amount": "-500", "note": "-5", "text": "'@x"}]


def test_built_in_exports_are_unchanged_by_the_neutraliser():
    from cyberliquidity.export import block_rows, event_rows, ledger_rows, obligation_rows

    r = simulate(stress(load("default_72h.json")))
    for rows in (obligation_rows(r), event_rows(r), ledger_rows(r), block_rows(r)):
        assert all(safe_cell(v) == v for row in rows for v in row.values())


# ------------------------------------------------------------------ integrity audit


@pytest.mark.parametrize("name", BUILT_IN)
def test_audit_passes_on_built_in_runs(name):
    report = audit(simulate(stress(load(name))))
    assert report.ok and report.failures == () and report.summary().startswith("Integrity audit passed")
    assert [c[0] for c in report.checks] == ["ledger replay", "numbering", "settlements", "totals"]


def test_audit_passes_on_random_networks():
    for seed in range(60):
        assert audit(simulate(random_scenario(seed))).ok, seed


def _stressed():
    return simulate(stress(load("default_72h.json")))


def test_audit_detects_a_changed_balance():
    r = _stressed()
    party = next(iter(r.hours[30].balances))
    r.hours[30].balances[party] += 1
    report = audit(r)
    assert not report.ok and "balances at the end of hour 30 do not replay" in report.failures[0]


def test_audit_detects_a_changed_ledger_amount():
    r = _stressed()
    r.ledger[3] = dc_replace(r.ledger[3], amount=r.ledger[3].amount + 100)
    assert not audit(r).ok


def test_audit_detects_numbering_gaps_and_duplicate_settlements():
    r = _stressed()
    r.ledger[2] = dc_replace(r.ledger[2], entry_id=99)
    assert any(f.startswith("numbering") for f in audit(r).failures)

    r = _stressed()
    settled = next(e for e in r.events if e.type == "SETTLEMENT")
    r.events.append(dc_replace(settled, event_id=len(r.events) + 1))
    assert any(f.startswith("settlements") for f in audit(r).failures)


def test_audit_detects_a_wrong_final_total():
    r = _stressed()
    r.totals["final_internal_cash"] += 1
    report = audit(r)
    assert not report.ok and report.failures == ("totals: reported final cash differs from the replayed cash",)
    assert report.summary().startswith("Integrity audit FAILED")


def test_engine_has_no_bare_asserts():
    for path in (ROOT / "src" / "cyberliquidity").glob("*.py"):
        lines = path.read_text(encoding="utf-8").splitlines()
        assert not [n for n, line in enumerate(lines, 1) if line.lstrip().startswith("assert ")], path.name
