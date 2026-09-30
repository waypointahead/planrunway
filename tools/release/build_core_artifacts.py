from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--expected-sha256")
    args = parser.parse_args()
    root = Path(__file__).parents[2]
    package = root / "packages" / "planrunway"
    if not package.is_dir():
        package = root
    args.output.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [sys.executable, "-m", "pip", "wheel", "--no-deps", "--no-cache-dir", str(package), "--wheel-dir", str(args.output)],
        env={**os.environ, "SOURCE_DATE_EPOCH": "0"},
        check=True,
    )
    wheel = next(args.output.glob("planrunway-*.whl"))
    with zipfile.ZipFile(wheel) as archive:
        metadata = next(name for name in archive.namelist() if name.endswith("METADATA"))
        package_metadata = archive.read(metadata).decode("utf-8")
    version = next(line.split(": ", 1)[1] for line in package_metadata.splitlines() if line.startswith("Version: "))
    digest = sha256(wheel)
    if args.expected_sha256 and digest != args.expected_sha256:
        print(f"Candidate digest mismatch: expected {args.expected_sha256}, got {digest}", file=sys.stderr)
        return 1
    (args.output / f"planrunway-{version}.sha256").write_text(f"{digest}  {wheel.name}\n", encoding="utf-8")
    (args.output / f"planrunway-{version}.sbom.json").write_text(json.dumps({
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "components": [{"type": "library", "name": "planrunway", "version": version}],
    }, indent=2) + "\n", encoding="utf-8")
    (args.output / f"planrunway-{version}.provenance.json").write_text(json.dumps({
        "subject": {"name": wheel.name, "sha256": digest},
        "build": {"package": "planrunway", "version": version},
    }, indent=2) + "\n", encoding="utf-8")
    print(f"Built {wheel.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
