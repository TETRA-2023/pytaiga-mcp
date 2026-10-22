"""The repository is public: no operator-internal names in tracked files.

CHANGELOG.md (generated from commit history) and uv.lock are exempt.
"""

import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
# Assembled from parts so this file does not match itself.
_FORBIDDEN = re.compile(
    "|".join(
        [
            r"tetra-ai\." + r"(fr|com)",
            "smart" + "agile",
            "tetra-" + "server",
            r"~/\.claude/" + r"\w+\.env",
            r"[Tt]aiga tetra" + r" (issue )?#\d+",
        ]
    )
)
_EXEMPT = {"CHANGELOG.md", "uv.lock"}


def _tracked_files():
    try:
        out = subprocess.run(
            ["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("not a git checkout")
    return [f for f in out.decode().split("\0") if f and f not in _EXEMPT]


def test_no_internal_references_in_tracked_files():
    hits = []
    for name in _tracked_files():
        path = ROOT / name
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, FileNotFoundError, IsADirectoryError):
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            if _FORBIDDEN.search(line):
                hits.append(f"{name}:{lineno}: {line.strip()[:100]}")
    assert not hits, "Internal references in a public repo:\n" + "\n".join(hits)
