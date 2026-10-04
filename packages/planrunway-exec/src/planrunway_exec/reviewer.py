"""Opt-in Linux/WSL read-only filesystem wrapper for Exec reviewer processes."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path


def reviewer_command(argv: list[str], home: Path) -> list[str]:
    if "--dir" not in argv or argv.index("--dir") + 1 >= len(argv):
        raise ValueError("Reviewer command requires explicit --dir worktree")
    directory = Path(argv[argv.index("--dir") + 1]).resolve()
    if directory == Path("/tmp") or Path("/tmp") in directory.parents:
        raise ValueError("Reviewer worktree must be outside /tmp; private sandbox uses ephemeral /tmp")
    if shutil.which("bwrap") is None:
        raise ValueError("Read-only reviewer requires bubblewrap (bwrap) on Linux/WSL")
    command = ["bwrap", "--die-with-parent", "--ro-bind", "/", "/", "--dev-bind", "/dev", "/dev", "--proc", "/proc", "--tmpfs", "/tmp"]
    for location in (home / ".local" / "share" / "opencode", home / ".cache" / "opencode"):
        if directory == location or location in directory.parents:
            raise ValueError("Reviewer worktree must be outside writable OpenCode state and cache mounts")
        if location.is_dir() and not location.is_symlink():
            command.extend(("--bind", str(location), str(location)))
    return [*command, "--", *argv]


def main() -> int:
    try:
        command = reviewer_command(sys.argv[1:], Path.home())
        return subprocess.run(command, check=False, env=os.environ.copy()).returncode
    except (OSError, ValueError) as error:
        print(f"[planrunway-exec-review] ERROR: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
