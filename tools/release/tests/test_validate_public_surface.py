from __future__ import annotations

import importlib.util
import zipfile
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "validate_public_surface.py"
SPEC = importlib.util.spec_from_file_location("validate_public_surface", SCRIPT)
assert SPEC and SPEC.loader
VALIDATOR = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(VALIDATOR)


def test_accepts_clean_public_tree(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("# PlanRunway\n", encoding="utf-8")
    (tmp_path / "LICENSE").write_text("MPL-2.0\n", encoding="utf-8")

    assert VALIDATOR.validate(tmp_path) == []


def test_rejects_private_paths_and_legacy_content(tmp_path: Path) -> None:
    private_state = tmp_path / ("." + "prway")
    private_state.mkdir()
    (private_state / "meta.json").write_text("{}\n", encoding="utf-8")
    (tmp_path / "guide.md").write_text("Plan" + "Forge state\n", encoding="utf-8")

    failures = VALIDATOR.validate(tmp_path)

    assert any("forbidden public path" in failure for failure in failures)
    assert any(("plan" + "forge") in failure for failure in failures)


def test_rejects_legacy_content_in_wheel(tmp_path: Path) -> None:
    wheel = tmp_path / "artifact.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("package/guide.md", "Plan" + "Forge state\n")

    assert any("artifact.whl:package/guide.md" in failure for failure in VALIDATOR.validate(tmp_path))
