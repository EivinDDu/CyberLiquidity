"""Scenario loading/saving and reproducible result exports (CSV + JSON, optional zip bundle).

A bundle contains, for every run, the exact scenario JSON that was simulated, the full
event ledger, the cash ledger, per-obligation outcomes, blocking records, hourly and
per-service series, and a summary with the result digest. Re-running
``cyberliquidity run <run>/scenario.json`` must reproduce the same digest.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import platform
import zipfile
from collections.abc import Mapping
from importlib import metadata
from pathlib import Path
from typing import Any

from .engine import ENGINE_VERSION, SimulationResult
from .metrics import (
    balance_rows,
    block_rows,
    delay_distribution,
    event_rows,
    hourly_rows,
    ledger_rows,
    obligation_rows,
    service_rows,
    summarize,
)
from .models import DISCLAIMER, Scenario
from .safety import safe_cell

_FIXED_ZIP_TIME = (1980, 1, 1, 0, 0, 0)  # deterministic archives


def load_scenario(path: str | Path) -> Scenario:
    return Scenario.model_validate_json(Path(path).read_text(encoding="utf-8"))


def scenario_json(scenario: Scenario) -> str:
    return scenario.model_dump_json(indent=2) + "\n"


def save_scenario(scenario: Scenario, path: str | Path) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(scenario_json(scenario), encoding="utf-8")


def scenario_digest(scenario: Scenario) -> str:
    canonical = json.dumps(scenario.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def rows_to_csv(rows: list[dict[str, Any]]) -> str:
    """CSV text for a list of rows. Text cells that a spreadsheet would run as a formula are neutralised
    (``safety.safe_cell``), so an exported table is safe to open in Excel, LibreOffice or Google Sheets."""
    buffer = io.StringIO()
    if not rows:
        return ""
    writer = csv.DictWriter(buffer, fieldnames=list(rows[0].keys()), lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({k: ("" if v is None else safe_cell(v)) for k, v in row.items()})
    return buffer.getvalue()


def environment_info() -> dict[str, str]:
    info = {"python": platform.python_version(), "engine_version": ENGINE_VERSION}
    for package in ("cyberliquidity", "pydantic", "streamlit", "plotly", "pandas", "matplotlib"):
        try:
            info[package] = metadata.version(package)
        except metadata.PackageNotFoundError:
            continue
    return info


def run_files(result: SimulationResult, label: str = "") -> dict[str, str]:
    """All export files for one run as ``{filename: text}``."""
    summary = summarize(result)
    summary_doc = {
        "label": label,
        "disclaimer": DISCLAIMER,
        "scenario_id": result.scenario.id,
        "scenario_digest": scenario_digest(result.scenario),
        "results_digest": result.digest(),
        "engine_version": result.engine_version,
        "currency": result.scenario.currency.model_dump(),
        "amounts": "integer minor currency units",
        "effective_disruptions": [d.model_dump(mode="json") for d in result.effective_disruptions],
        "summary": summary,
        "delay_distribution": delay_distribution(result),
    }
    return {
        "scenario.json": scenario_json(result.scenario),
        "summary.json": json.dumps(summary_doc, indent=2, sort_keys=True, default=str) + "\n",
        "obligations.csv": rows_to_csv(obligation_rows(result)),
        "events.csv": rows_to_csv(event_rows(result)),
        "ledger.csv": rows_to_csv(ledger_rows(result)),
        "blocks.csv": rows_to_csv(block_rows(result)),
        "hourly.csv": rows_to_csv(hourly_rows(result)),
        "balances.csv": rows_to_csv(balance_rows(result)),
        "services.csv": rows_to_csv(service_rows(result)),
    }


def export_run(result: SimulationResult, out_dir: str | Path, label: str = "") -> Path:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    for name, text in run_files(result, label).items():
        (out / name).write_text(text, encoding="utf-8")
    return out


def _safe(name: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in name)


def manifest(runs: Mapping[str, SimulationResult], labels: Mapping[str, str] | None = None,
             extra: Mapping[str, Any] | None = None) -> dict[str, Any]:
    return {
        "tool": "CyberLiquidity",
        "disclaimer": DISCLAIMER,
        "environment": environment_info(),
        "amounts": "integer minor currency units",
        "runs": {
            _safe(key): {
                "label": (labels or {}).get(key, key),
                "scenario_id": r.scenario.id,
                "scenario_digest": scenario_digest(r.scenario),
                "results_digest": r.digest(),
                "reproduce": f"cyberliquidity run {_safe(key)}/scenario.json  # prints the results digest",
            }
            for key, r in runs.items()
        },
        **dict(extra or {}),
    }


def bundle_zip(runs: Mapping[str, SimulationResult], labels: Mapping[str, str] | None = None,
               extra_files: Mapping[str, str] | None = None, extra_manifest: Mapping[str, Any] | None = None) -> bytes:
    """A deterministic zip archive with every run's configuration, results and ledgers."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        def add(name: str, text: str) -> None:
            info = zipfile.ZipInfo(f"cyberliquidity_bundle/{name}", date_time=_FIXED_ZIP_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, text)

        add("manifest.json", json.dumps(manifest(runs, labels, extra_manifest), indent=2, sort_keys=True) + "\n")
        add("README.txt", (
            f"{DISCLAIMER}\n\n"
            "Each folder holds one simulation run: scenario.json is the exact input; events.csv is the full event\n"
            "ledger; ledger.csv the cash movements; obligations.csv the per-obligation outcomes; blocks.csv every\n"
            "hour an obligation could not settle with all reasons; hourly.csv, balances.csv and services.csv the\n"
            "time series. Amounts are integer minor currency units.\n\n"
            "Reproduce a run (from the CyberLiquidity repository, venv active):\n"
            "    cyberliquidity run <folder>/scenario.json\n"
            "and compare the printed results digest with manifest.json.\n"
        ))
        for key, result in runs.items():
            folder = _safe(key)
            for name, text in run_files(result, (labels or {}).get(key, key)).items():
                add(f"{folder}/{name}", text)
        for name, text in (extra_files or {}).items():
            add(name, text)
    return buffer.getvalue()
