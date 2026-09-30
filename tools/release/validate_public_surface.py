from __future__ import annotations

import argparse
import io
import tarfile
import zipfile
from pathlib import Path


FORBIDDEN_PATH_PARTS = {"." + "plan", "." + "prway", "." + "planrunway-cutover-backups", "plan" + "forge", "promotion", "archive"}
FORBIDDEN_BYTES = (b"plan" + b"forge", b".pl" + b"an/")


def scan_payload(label: str, payload: bytes, failures: list[str]) -> None:
    lowered = payload.lower()
    for forbidden in FORBIDDEN_BYTES:
        if forbidden in lowered:
            failures.append(f"{label}: forbidden public identifier {forbidden.decode('ascii')!r}")


def scan_archive(label: str, payload: bytes, failures: list[str]) -> None:
    stream = io.BytesIO(payload)
    if zipfile.is_zipfile(stream):
        with zipfile.ZipFile(stream) as archive:
            for member in archive.infolist():
                scan_payload(f"{label}:{member.filename}", member.filename.encode("utf-8"), failures)
                if not member.is_dir():
                    member_payload = archive.read(member)
                    scan_payload(f"{label}:{member.filename}", member_payload, failures)
                    scan_archive(f"{label}:{member.filename}", member_payload, failures)
        return
    stream.seek(0)
    try:
        with tarfile.open(fileobj=stream) as archive:
            for member in archive.getmembers():
                scan_payload(f"{label}:{member.name}", member.name.encode("utf-8"), failures)
                if member.isfile():
                    source = archive.extractfile(member)
                    assert source is not None
                    member_payload = source.read()
                    scan_payload(f"{label}:{member.name}", member_payload, failures)
                    scan_archive(f"{label}:{member.name}", member_payload, failures)
    except tarfile.ReadError:
        return


def validate(root: Path) -> list[str]:
    failures: list[str] = []
    for path in root.rglob("*"):
        relative = path.relative_to(root)
        if any(part.lower() in FORBIDDEN_PATH_PARTS for part in relative.parts):
            failures.append(f"{relative}: forbidden public path")
            continue
        if path.is_symlink():
            failures.append(f"{relative}: symlinks are not allowed in public release source")
            continue
        if not path.is_file():
            continue
        payload = path.read_bytes()
        scan_payload(str(relative), payload, failures)
        scan_archive(str(relative), payload, failures)
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description="Reject private and legacy material from public PlanRunway source.")
    parser.add_argument("root", type=Path, nargs="?", default=Path("."))
    args = parser.parse_args()
    failures = validate(args.root)
    if failures:
        print("\n".join(failures))
        return 1
    print("PASS public surface")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
