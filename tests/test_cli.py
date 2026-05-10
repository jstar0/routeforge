import json
from pathlib import Path

from routeforge.cli import main


def test_demo_cli_outputs_artifact_manifest(tmp_path: Path, capsys) -> None:
    assert main(["demo", "--seed", "5", "--output-dir", str(tmp_path / "demo")]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["instance"]["customers"] == 12
    assert Path(payload["artifacts"]["html"]).exists()
    assert Path(payload["artifacts"]["stops"]).exists()


def test_solve_cli_reads_its_exported_scene_json(tmp_path: Path, capsys) -> None:
    assert main(["demo", "--seed", "5", "--output-dir", str(tmp_path / "first")]) == 0
    first = json.loads(capsys.readouterr().out)
    assert main(["solve", "--instance", first["artifacts"]["instance"], "--output-dir", str(tmp_path / "second")]) == 0
    second = json.loads(capsys.readouterr().out)
    assert second["instance"] == first["instance"]


def test_benchmark_and_sweep_cli_write_reports(tmp_path: Path, capsys) -> None:
    assert main(["benchmark", "--seed", "17", "--output-dir", str(tmp_path / "benchmark")]) == 0
    assert main(["sweep", "--seeds", "17,29", "--output-dir", str(tmp_path / "sweep")]) == 0
    output = capsys.readouterr().out
    assert '"benchmarkVersion": 1' in output
    assert '"sweepVersion": 1' in output
    assert (tmp_path / "benchmark" / "manifest.json").exists()
    assert (tmp_path / "sweep" / "manifest.json").exists()
