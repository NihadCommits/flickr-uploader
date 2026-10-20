"""Guard against weakening the .gitignore protection for secrets and personal data."""

import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

MUST_BE_IGNORED = [
    ".env",
    ".env.local",
    "credentials.json",
    "credentials_backup.json",
    "flickr.token",
    "private.pem",
    "state.db",
    "state.db-wal",
    "state.db-shm",
    "oauth-tokens.sqlite",
    "albums_export.csv",
    "scripts/albums_export.csv",
    "my_edits.csv",
    "photos/img_0001.jpg",
    "photos/img_0001.cr3",
    "videos/clip.mp4",
    "uploader.log",
    ".venv/",
]

MUST_NOT_BE_IGNORED = [
    "README.md",
    "requirements.txt",
    "src/flickr_uploader/db.py",
    "src/flickr_uploader/gui/web/index.html",
    "tests/test_repo_hygiene.py",
]


def is_ignored(path: str) -> bool:
    result = subprocess.run(
        ["git", "check-ignore", "--no-index", "--quiet", path],
        cwd=REPO_ROOT,
        check=False,
    )
    return result.returncode == 0


@pytest.mark.parametrize("path", MUST_BE_IGNORED)
def test_sensitive_path_is_ignored(path):
    assert is_ignored(path), f"{path} is not covered by .gitignore"


@pytest.mark.parametrize("path", MUST_NOT_BE_IGNORED)
def test_project_path_is_not_ignored(path):
    assert not is_ignored(path), f"{path} is unexpectedly ignored"
