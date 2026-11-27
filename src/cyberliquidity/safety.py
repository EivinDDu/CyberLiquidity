"""Protective checks at the edges: scenario files coming in, tables going out.

Scenario files can come from anywhere (an upload in the app, a file someone shared), so
before anything is simulated they pass four gates, and each failure produces a short,
plain-language explanation instead of a crash or a stack trace:

1. **Size**: the file is not larger than ``Limits.max_bytes``.
2. **Encoding**: it is UTF-8 text (a byte-order mark is tolerated).
3. **Structure**: it is a valid scenario (``models.Scenario``: types, ranges, references,
   no dependency cycles, plain-text names; see ``docs/scenario.schema.json``).
4. **Complexity**: it is small enough to simulate interactively (``Limits``). The app runs
   well over a hundred simulations for one configuration (comparison, pairs, backstop,
   robustness check), so it accepts smaller scenarios than the command line.

On the way out, ``safe_cell`` neutralises spreadsheet formulas in exported CSV text cells
(a cell that starts with ``=``, ``+``, ``-``, ``@``, a tab or a carriage return is run as a
formula by Excel, LibreOffice and Google Sheets when the file is opened).

These are hardening measures for a local research tool. They are not a security
certification, and the tool is not designed to be exposed to untrusted networks.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from .models import Scenario


@dataclass(frozen=True)
class Limits:
    max_bytes: int = 20_000_000
    max_participants: int = 2_000
    max_external_parties: int = 2_000
    max_services: int = 500
    max_routes: int = 1_000
    max_obligations: int = 100_000
    max_disruptions: int = 50
    max_interventions: int = 100  # applied plus candidates
    max_work: int = 10_000_000  # obligations x horizon hours: a rough proxy for the cost of one run


CLI_LIMITS = Limits()
APP_LIMITS = Limits(max_bytes=5_000_000, max_participants=300, max_external_parties=300, max_services=150,
                    max_routes=300, max_obligations=3_000, max_disruptions=10, max_interventions=30,
                    max_work=250_000)


class ScenarioInputError(ValueError):
    """A scenario file was refused. ``title`` says why in one line; ``problems`` lists the details."""

    def __init__(self, title: str, problems: list[str] | None = None):
        self.title = title
        self.problems = list(problems or [])
        detail = "; ".join(self.problems[:3]) + (" ..." if len(self.problems) > 3 else "")
        super().__init__(f"{title}: {detail}" if detail else title)


def friendly_errors(exc: ValidationError, limit: int = 40) -> list[str]:
    """One readable line per validation problem ("where: what"), without echoing the input."""
    lines: list[str] = []
    for error in exc.errors(include_url=False, include_input=False):
        where = ".".join(str(part) for part in error["loc"]) or "scenario"
        message = str(error["msg"]).removeprefix("Value error, ")
        if message.startswith("Invalid scenario:"):  # the cross-reference check lists several problems
            lines.extend(item.removeprefix("- ") for item in message.splitlines()[1:] if item.strip())
        else:
            lines.append(f"{where}: {message}")
    if len(lines) > limit:
        lines = lines[:limit] + [f"... and {len(lines) - limit} more"]
    return lines


def complexity_problems(scenario: Scenario, limits: Limits) -> list[str]:
    counts = [
        ("participants", len(scenario.participants), limits.max_participants),
        ("external parties", len(scenario.external_parties), limits.max_external_parties),
        ("services", len(scenario.services), limits.max_services),
        ("routes", len(scenario.routes), limits.max_routes),
        ("obligations", len(scenario.obligations), limits.max_obligations),
        ("disruptions", len(scenario.disruptions), limits.max_disruptions),
        ("interventions", len(scenario.interventions) + len(scenario.candidate_interventions), limits.max_interventions),
    ]
    problems = [f"{count:,} {what} (the limit here is {limit:,})" for what, count, limit in counts if count > limit]
    work = len(scenario.obligations) * scenario.horizon_hours
    if work > limits.max_work:
        problems.append(f"{len(scenario.obligations):,} obligations over {scenario.horizon_hours} hours is "
                        f"{work:,} obligation-hours (the limit here is {limits.max_work:,})")
    return problems


def parse_scenario(data: bytes | str, limits: Limits | None = CLI_LIMITS, source: str = "The file") -> Scenario:
    """Validate scenario text through all four gates; raise ``ScenarioInputError`` on the first failure.

    ``limits=None`` skips the size and complexity gates (trusted, programmatic use).
    """
    raw = data.encode("utf-8") if isinstance(data, str) else bytes(data)
    if limits is not None and len(raw) > limits.max_bytes:
        raise ScenarioInputError(f"{source} is too large",
                                 [f"{len(raw):,} bytes; the limit here is {limits.max_bytes:,} bytes"])
    if not raw.strip():
        raise ScenarioInputError(f"{source} is empty", ["a scenario is one JSON document (see docs/scenario.schema.json)"])
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ScenarioInputError(f"{source} is not UTF-8 text",
                                 [f"unreadable byte at position {exc.start}; save the file as UTF-8 JSON"]) from None
    try:
        scenario = Scenario.model_validate_json(text)
    except ValidationError as exc:
        raise ScenarioInputError(f"{source} is not a valid scenario", friendly_errors(exc)) from None
    if limits is not None:
        problems = complexity_problems(scenario, limits)
        if problems:
            raise ScenarioInputError(f"{source} is larger than this tool accepts here", problems)
    return scenario


_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")
_PLAIN_NUMBER = re.compile(r"^[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?$")


def safe_cell(value: Any) -> Any:
    """Neutralise a text cell that a spreadsheet would run as a formula (prefix it with an apostrophe).

    Numbers, and text that is just a number (such as ``-5``), are left alone.
    """
    if isinstance(value, str) and value.startswith(_FORMULA_START) and not _PLAIN_NUMBER.match(value):
        return "'" + value
    return value
