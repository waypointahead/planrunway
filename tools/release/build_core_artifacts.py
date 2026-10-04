from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build_artifact(package: Path, output: Path, name: str, expected_sha256: str | None = None, expected_version: str | None = None) -> tuple[str, str] | None:
    version = tomllib.loads((package / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    if expected_version is not None and version != expected_version:
        print(f"{name} version mismatch: expected {expected_version}, got {version}", file=sys.stderr)
        return None
    subprocess.run(
        [sys.executable, "-m", "pip", "wheel", "--no-deps", "--no-cache-dir", str(package), "--wheel-dir", str(output)],
        env={**os.environ, "SOURCE_DATE_EPOCH": "0"},
        check=True,
    )
    wheels = list(output.glob(f"{name.replace('-', '_')}-{version}-*.whl"))
    if len(wheels) != 1:
        raise ValueError(f"Expected one {name} {version} wheel, found {len(wheels)}")
    wheel = wheels[0]
    with zipfile.ZipFile(wheel) as archive:
        metadata = next(name for name in archive.namelist() if name.endswith("METADATA"))
        package_metadata = archive.read(metadata).decode("utf-8")
    built_version = next(line.split(": ", 1)[1] for line in package_metadata.splitlines() if line.startswith("Version: "))
    if built_version != version:
        raise ValueError(f"Built {name} metadata version differs from source: {built_version}")
    digest = sha256(wheel)
    if expected_sha256 and digest != expected_sha256:
        print(f"Candidate digest mismatch for {name}: expected {expected_sha256}, got {digest}", file=sys.stderr)
        return None
    (output / f"{name}-{version}.sha256").write_text(f"{digest}  {wheel.name}\n", encoding="utf-8")
    (output / f"{name}-{version}.sbom.json").write_text(json.dumps({
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "components": [{"type": "library", "name": name, "version": version}],
    }, indent=2) + "\n", encoding="utf-8")
    (output / f"{name}-{version}.provenance.json").write_text(json.dumps({
        "subject": {"name": wheel.name, "sha256": digest},
        "build": {"package": name, "version": version},
    }, indent=2) + "\n", encoding="utf-8")
    print(f"Built {wheel.name}")
    return version, digest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--expected-sha256")
    parser.add_argument("--exec-version")
    parser.add_argument("--expected-exec-sha256")
    args = parser.parse_args()
    root = Path(__file__).parents[2]
    package = root / "packages" / "planrunway"
    if not package.is_dir():
        package = root
    args.output.mkdir(parents=True, exist_ok=True)
    if build_artifact(package, args.output, "planrunway", args.expected_sha256) is None:
        return 1
    if args.exec_version:
        execution = root / "packages" / "planrunway-exec"
        if not execution.is_dir():
            print("Exec source is missing from public export", file=sys.stderr)
            return 1
        if build_artifact(execution, args.output, "planrunway-exec", args.expected_exec_sha256, args.exec_version) is None:
            return 1
    elif args.expected_exec_sha256:
        print("--expected-exec-sha256 requires --exec-version", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
