from __future__ import annotations

import json
import hashlib
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest


SCRIPT = Path(__file__).parents[1] / "build_core_artifacts.py"


@pytest.mark.skipif(sys.version_info < (3, 11), reason="planrunway package requires Python 3.11+")
def test_builds_release_artifacts(tmp_path: Path) -> None:
    subprocess.run([sys.executable, str(SCRIPT), "--output", str(tmp_path)], check=True)
    wheel = next(tmp_path.glob("planrunway-*.whl"))
    version = wheel.name.split("-")[1]
    assert (tmp_path / f"planrunway-{version}.sha256").is_file()
    assert json.loads((tmp_path / f"planrunway-{version}.sbom.json").read_text(encoding="utf-8"))["bomFormat"] == "CycloneDX"
    assert json.loads((tmp_path / f"planrunway-{version}.provenance.json").read_text(encoding="utf-8"))["subject"]["name"] == wheel.name
    with zipfile.ZipFile(wheel) as archive:
        metadata = archive.read(next(name for name in archive.namelist() if name.endswith(".dist-info/METADATA"))).decode("utf-8")
        assert "License: Mozilla Public License Version 2.0" in metadata
        assert any(name.endswith(".dist-info/licenses/LICENSE") for name in archive.namelist())


@pytest.mark.skipif(sys.version_info < (3, 11), reason="planrunway package requires Python 3.11+")
def test_rejects_unexpected_candidate_digest(tmp_path: Path) -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--output", str(tmp_path), "--expected-sha256", "0" * 64],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 1
    assert "Candidate digest mismatch" in result.stderr


def test_builds_separate_exec_artifacts_and_rejects_wrong_exec_digest(tmp_path: Path) -> None:
    built = subprocess.run([sys.executable, str(SCRIPT), "--output", str(tmp_path), "--exec-version", "0.1.0"], text=True, capture_output=True, check=False)
    assert built.returncode == 0, built.stderr
    wheel = tmp_path / "planrunway_exec-0.1.0-py3-none-any.whl"
    assert wheel.is_file()
    digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
    assert (tmp_path / "planrunway-exec-0.1.0.sha256").read_text(encoding="utf-8") == f"{digest}  {wheel.name}\n"
    assert json.loads((tmp_path / "planrunway-exec-0.1.0.provenance.json").read_text(encoding="utf-8"))["subject"] == {"name": wheel.name, "sha256": digest}
    with zipfile.ZipFile(wheel) as archive:
        assert any(name.endswith("entry_points.txt") and b"planrunway-exec-review" in archive.read(name) for name in archive.namelist())
    rejected = subprocess.run([sys.executable, str(SCRIPT), "--output", str(tmp_path), "--exec-version", "0.1.0", "--expected-exec-sha256", "0" * 64], text=True, capture_output=True, check=False)
    assert rejected.returncode == 1
    assert "Candidate digest mismatch for planrunway-exec" in rejected.stderr
