"""Fail if any tracked text file is not valid UTF-8."""

from __future__ import annotations

import subprocess
import sys
from pathlib import PurePosixPath

TEXT_SUFFIXES = frozenset(
    {".py", ".md", ".toml", ".yml", ".yaml", ".txt", ".cfg", ".ini", ".sh", ".json"}
)


def main() -> int:
    """Fail if any tracked text file is not valid UTF-8.

    Returns:
        Process exit code: 0 when all files decode, 1 otherwise.
    """
    raw = subprocess.run(["git", "ls-files", "-z"], check=True, capture_output=True).stdout
    bad: list[str] = []
    for item in raw.split(b"\0"):
        if not item:
            continue
        name = item.decode("utf-8", errors="surrogateescape")
        if PurePosixPath(name).suffix.lower() not in TEXT_SUFFIXES:
            continue
        try:
            with open(name, "rb") as fh:
                fh.read().decode("utf-8")
        except UnicodeDecodeError:
            bad.append(name)
        except OSError:
            continue  # tracked but deleted in the working tree
    for name in bad:
        print(f"not valid UTF-8: {name}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
