"""Command-line reproduction path, backup suggestions and the research experiment."""

from __future__ import annotations

import csv
import importlib.util
import json
import sys
from pathlib import Path

from pydantic import TypeAdapter

from cyberliquidity import Disruption, SimulationSettings, simulate
from cyberliquidity.cli import main
from cyberliquidity.export import load_scenario
from cyberliquidity.models import Intervention
from cyberliquidity.variants import make_disruption, replace, suggest_backup
from tests.conftest import SCENARIOS, load

ROOT = Path(__file__).resolve().parents[1]


def test_cli_run_prints_the_reproducible_digest(capsys, tmp_path):
    path = SCENARIOS / "default_72h.json"
    assert main(["run", str(path), "--out", str(tmp_path / "run")]) == 0
    out = capsys.readouterr().out
    assert simulate(load_scenario(path)).digest() in out
    exported = load_scenario(tmp_path / "run" / "scenario.json")
    assert simulate(exported).digest() in out  # the exported configuration reproduces the run


def test_cli_validate_and_compare(capsys, tmp_path):
    assert main(["validate", str(SCENARIOS / "three_participant.json")]) == 0
    assert main(["compare", str(SCENARIOS / "three_participant.json"), "--out", str(tmp_path)]) == 0
    assert (tmp_path / "bundle.zip").exists() and (tmp_path / "B" / "events.csv").exists()
    assert "Ranking by overdue value x hours" in capsys.readouterr().out


def test_backup_suggestion_respects_shared_dependencies():
    s = load("default_72h.json")
    nimbus = suggest_backup(s, make_disruption("cloud_nimbus", 32, 12))
    assert nimbus.primary_routes == ("alpha_north",) and nimbus.backup_route == "beta_south" and nimbus.independent
    keygate = suggest_backup(s, make_disruption("idp_keygate", 32, 12))
    assert not keygate.independent and "idp_keygate" in keygate.common_mode_services


def test_experiment_reproduces_and_finds_all_three_mechanisms(tmp_path):
    spec = importlib.util.spec_from_file_location("experiment", ROOT / "experiments" / "redundancy_vs_liquidity.py")
    experiment = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = experiment  # dataclasses resolve annotations through sys.modules
    spec.loader.exec_module(experiment)
    out = tmp_path / "one"
    a = experiment.run(SCENARIOS / "default_72h.json", out)
    assert (out / "comparison.png").stat().st_size > 10_000
    rows = list(csv.DictReader((out / "results.csv").open()))
    n_interventions = 7  # none, backup, two buffers, credit line, faster recovery, backup + buffer
    assert len(rows) == len(experiment.CASES) * len(experiment.DURATIONS) * n_interventions * len(experiment.SETTINGS)

    # Reproducibility: rebuild sampled runs from config.json alone and match their recorded digests.
    config = json.loads((out / "config.json").read_text())
    base = load_scenario(SCENARIOS / "default_72h.json")
    adapter = TypeAdapter(Intervention)
    by_spec = {(r["case"], int(r["duration_h"]), r["intervention"], r["setting"]): r["results_digest"] for r in rows}
    for run_spec in config["runs"][::37]:
        ivs = run_spec["intervention_spec"]
        ivs = [] if ivs is None else (ivs if isinstance(ivs, list) else [ivs])
        scenario = replace(base, disruptions=(Disruption.model_validate(run_spec["disruption"]),),
                           interventions=tuple(adapter.validate_python(x) for x in ivs),
                           settings=SimulationSettings.model_validate(run_spec["settings"]))
        key = (run_spec["case"], run_spec["duration_h"], run_spec["intervention"], run_spec["setting"])
        assert simulate(scenario).digest() == by_spec[key], key

    shares = {(m["case"], m["duration_h"]): m["cash_only_share"] for m in a["mechanisms"]}
    assert all(shares[("stratus_outage", d)] < 0.05 for d in experiment.DURATIONS)  # operational blockage dominates
    assert all(shares[("hv_channel_outage", d)] > 0.5 for d in experiment.DURATIONS if d >= 4)  # delayed cash dominates
    assert all(shares[("idp_outage", d)] == 0 for d in experiment.DURATIONS)  # common mode: nothing but operations
    assert all(r["credit_equals_largest_buffer"] for r in a["rankings"])  # model property, every case and setting
    idp = [r for r in a["rankings"] if r["case"] == "idp_outage" and r["setting"] == "default"]
    assert all(r["reduction_pct_backup_route"] == 0 for r in idp)  # the backup shares the failed identity provider
    assert all(b["verified_sufficient"] for b in a["backstops"])
    assert len(a["offset_stability"]) == len(experiment.CASES) * len(experiment.DURATIONS)
    # the committed outputs are current: a fresh run reproduces them byte for byte
    committed = ROOT / "experiments" / "output" / "redundancy_vs_liquidity"
    for name in ("results.csv", "rankings.csv", "mechanism_split.csv", "complementarity.csv", "start_offsets.csv",
                 "backstop.csv", "findings.md"):
        assert (out / name).read_text(encoding="utf-8") == (committed / name).read_text(encoding="utf-8"), name


def test_new_cli_commands(capsys, tmp_path):
    path = str(SCENARIOS / "default_72h.json")
    assert main(["brief", path]) == 0
    out = capsys.readouterr().out
    assert "**20 of 138 payments are delayed**" in out and "Synthetic scenario" in out
    assert main(["backstop", path]) == 0
    assert "Silverfern Bakery" in capsys.readouterr().out
    assert main(["criticality", path, "--start", "32", "--duration", "12"]) == 0
    assert capsys.readouterr().out.splitlines()[3].startswith("idp_keygate")
    assert main(["profile", path, "--service", "cloud_nimbus", "--start", "32", "--durations", "2,6,8"]) == 0
    assert "first breach at 8 h" in capsys.readouterr().out
    target = tmp_path / "random.json"
    assert main(["generate", "--seed", "3", "--participants", "6", "--out", str(target)]) == 0
    assert main(["validate", str(target)]) == 0


def _load_experiment(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "experiments" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve annotations through sys.modules
    spec.loader.exec_module(module)
    return module


def test_gridlock_experiment_reproduces_its_findings(tmp_path):
    experiment = _load_experiment("gridlock_and_hoarding")
    a = experiment.run(tmp_path, plot_figure=False)
    assert {p.name for p in tmp_path.iterdir()} == {"config.json", "offsetting_grid.csv", "reserve_sweep.csv",
                                                    "reserve_individual.csv", "random_structures.csv", "cluster_pairs.csv",
                                                    "findings.md"}
    c = a["cluster_configured"]
    assert c["within_step"]["reduction_pct"] == 37.91 and c["within_step"]["liquidity_saved"] == 7_200_000
    assert a["chain_batches"] == 0  # no gridlock in the one-directional supply chain
    within = dict((f, pct) for f, pct, _ in a["cluster_by_factor"]["within_step"])
    assert within[0.5] == within[3.0] == 0 and within[1.0] > 0  # only a middle band of cash
    assert a["reserve_worse"] == 27 and a["reserve_stress_runs"] == 28
    assert a["random_offsetting"] == {"lower": 77, "unchanged": 222, "higher": 1}
    committed = (ROOT / "experiments" / "output" / "gridlock_and_hoarding" / "findings.md").read_text(encoding="utf-8")
    assert (tmp_path / "findings.md").read_text(encoding="utf-8") == committed  # the committed write-up is current
    verdicts = {(r["first"], r["second"]): r["verdict"] for r in a["pairs"]}
    assert verdicts[("buffer_birch", "reserve_payroll")] == "more harmful together"
    for name in ("offsetting_grid.csv", "reserve_sweep.csv", "reserve_individual.csv", "random_structures.csv",
                 "cluster_pairs.csv"):
        assert (tmp_path / name).read_text() == (ROOT / "experiments" / "output" / "gridlock_and_hoarding" / name).read_text()
