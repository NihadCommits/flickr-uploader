"""Tests for paths, limits and file types (flickr_uploader.config)."""

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from flickr_uploader import config
from flickr_uploader.config import MediaKind

REPO_ROOT = Path(__file__).resolve().parent.parent


# --- Paths --------------------------------------------------------------------


def test_default_paths_follow_windows_conventions(tmp_path):
    paths = config.app_paths({"LOCALAPPDATA": str(tmp_path / "Local")})
    assert paths.database == tmp_path / "Local" / "flickr-uploader" / "state.db"
    assert paths.backups == tmp_path / "Local" / "flickr-uploader" / "backups"


def test_no_file_location_exists_for_credentials():
    # Credentials live only in the Windows Credential Manager (auth.py).
    names = [name for name in dir(config.AppPaths) if not name.startswith("_")]
    assert not [name for name in names if "cred" in name or "secret" in name]


def test_default_paths_without_environment_use_the_home_folder(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    paths = config.app_paths({})
    assert paths.data_dir == tmp_path / "AppData" / "Local" / "flickr-uploader"


def test_app_paths_creates_nothing(tmp_path):
    config.app_paths({"LOCALAPPDATA": str(tmp_path / "L")})
    assert list(tmp_path.iterdir()) == []


def test_home_override(tmp_path):
    paths = config.app_paths({config.HOME_ENV_VAR: str(tmp_path / "profile")})
    assert paths.database == tmp_path / "profile" / "state.db"


def test_home_override_must_be_absolute():
    with pytest.raises(config.ConfigError, match="absolute"):
        config.app_paths({config.HOME_ENV_VAR: "relative/profile"})


def test_home_override_inside_this_repository_is_refused():
    with pytest.raises(config.ConfigError, match="git repository"):
        config.app_paths({config.HOME_ENV_VAR: str(REPO_ROOT / "local-data")})


def test_home_override_inside_any_git_repository_is_refused(tmp_path):
    (tmp_path / "some-repo" / ".git").mkdir(parents=True)
    with pytest.raises(config.ConfigError, match="git repository"):
        config.app_paths({config.HOME_ENV_VAR: str(tmp_path / "some-repo" / "deep" / "dir")})


def test_default_location_inside_a_dotfiles_repository_is_allowed(tmp_path):
    (tmp_path / ".git").mkdir()
    paths = config.app_paths({"LOCALAPPDATA": str(tmp_path / "Local")})
    assert paths.data_dir == tmp_path / "Local" / "flickr-uploader"


@pytest.mark.parametrize("unc", [r"\\nas\photos", r"\\192.168.1.10\share\apps"])
def test_network_share_is_refused(unc):
    with pytest.raises(config.ConfigError, match="network drive"):
        config.app_paths({config.HOME_ENV_VAR: unc})
    with pytest.raises(config.ConfigError, match="network drive"):
        config.app_paths({"LOCALAPPDATA": unc})


def test_network_check_needs_no_disk_access(monkeypatch):
    # Touching a disconnected share would hang until the network times out.
    def must_not_run(path):
        raise AssertionError(f"disk access for {path}")

    monkeypatch.setattr(config, "_enclosing_git_repo", must_not_run)
    with pytest.raises(config.ConfigError, match="network drive"):
        config.app_paths({config.HOME_ENV_VAR: r"\\nas\photos"})


@pytest.mark.skipif(sys.platform != "win32", reason="drive letters are Windows-only")
def test_mapped_network_drive_is_refused(monkeypatch):
    monkeypatch.setattr(config, "_drive_type", lambda root: config.DRIVE_REMOTE)
    with pytest.raises(config.ConfigError, match="network drive"):
        config.app_paths({"LOCALAPPDATA": r"Z:\AppData"})


@pytest.mark.skipif(sys.platform != "win32", reason="drive letters are Windows-only")
def test_local_disk_is_accepted(tmp_path):
    # Real GetDriveTypeW call against the drive the tests run on.
    config.app_paths({config.HOME_ENV_VAR: str(tmp_path)})


# --- Paths for file access ----------------------------------------------------

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="Windows path forms")


@windows_only
@pytest.mark.parametrize(
    ("given", "expected"),
    [
        (r"C:\Bilder\IMG_0001.JPG", r"\\?\C:\Bilder\IMG_0001.JPG"),
        ("C:/Bilder/./2026/../IMG_0001.JPG", r"\\?\C:\Bilder\IMG_0001.JPG"),  # normalized
        (r"\\nas\fotos\IMG_0001.JPG", r"\\?\UNC\nas\fotos\IMG_0001.JPG"),
        (r"\\?\C:\Bilder\IMG_0001.JPG", r"\\?\C:\Bilder\IMG_0001.JPG"),  # already prefixed
    ],
)
def test_filesystem_path(given, expected):
    assert config.filesystem_path(given) == expected


@windows_only
def test_filesystem_path_makes_relative_paths_absolute(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    assert config.filesystem_path("IMG_0001.JPG") == "\\\\?\\" + str(tmp_path / "IMG_0001.JPG")


def test_filesystem_path_reaches_files_beyond_260_characters(tmp_path):
    deep = tmp_path.joinpath(*[f"2026-06 Portugal Urlaub Teil {i}" for i in range(10)])
    assert len(str(deep)) > 300
    os.makedirs(config.filesystem_path(deep))
    photo = config.filesystem_path(deep / "IMG_0001.JPG")
    with open(photo, "wb") as file:
        file.write(b"photo")
    with open(photo, "rb") as file:
        assert file.read() == b"photo"


# --- File types ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("filename", "kind"),
    [
        ("IMG_0001.JPG", MediaKind.PHOTO),
        ("scan.tiff", MediaKind.PHOTO),
        ("clip.MOV", MediaKind.VIDEO),
        ("MAH00012.m2ts", MediaKind.VIDEO),
        ("00012.MTS", MediaKind.VIDEO),  # AVCHD, same container as .m2ts
        ("IMG_0001.CR3", MediaKind.RAW),
        ("DSC_0001.nef", MediaKind.RAW),
        ("pano.dng", MediaKind.RAW),
        ("IMG_0001.HEIC", MediaKind.UNSUPPORTED),  # API does not accept HEIC
        ("IMG_0001.xmp", MediaKind.IGNORED),
        ("IMG_0001.AAE", MediaKind.IGNORED),
        ("Thumbs.db", MediaKind.IGNORED),
        ("desktop.ini", MediaKind.IGNORED),
        ("._IMG_0001.JPG", MediaKind.IGNORED),  # macOS resource fork, no image
        ("notes.txt", MediaKind.UNSUPPORTED),
        ("README", MediaKind.UNSUPPORTED),
        ("photo.jpg.bak", MediaKind.UNSUPPORTED),
        (".jpg", MediaKind.UNSUPPORTED),  # hidden file named ".jpg", no extension
    ],
)
def test_media_kind(filename, kind):
    assert config.media_kind(filename) is kind


def test_extension_lists_are_disjoint_and_normalized():
    lists = [
        config.PHOTO_EXTENSIONS,
        config.VIDEO_EXTENSIONS,
        config.RAW_EXTENSIONS,
        config.SIDECAR_EXTENSIONS,
    ]
    everything = [ext for extensions in lists for ext in extensions]
    assert len(everything) == len(set(everything))
    assert all(ext.startswith(".") and ext == ext.lower() for ext in everything)


@pytest.mark.parametrize(
    ("kind", "size", "too_large"),
    [
        (MediaKind.PHOTO, config.MAX_PHOTO_BYTES, False),
        (MediaKind.PHOTO, config.MAX_PHOTO_BYTES + 1, True),
        (MediaKind.VIDEO, config.MAX_VIDEO_BYTES, False),
        (MediaKind.VIDEO, config.MAX_VIDEO_BYTES + 1, True),
        (MediaKind.RAW, 10**12, False),  # skipped as RAW, size is irrelevant
        (MediaKind.UNSUPPORTED, 10**12, False),
    ],
)
def test_is_too_large(kind, size, too_large):
    assert config.is_too_large(kind, size) is too_large


def test_size_limits_use_decimal_megabytes():
    assert config.MAX_PHOTO_BYTES == 200_000_000
    assert config.MAX_VIDEO_BYTES == 1_000_000_000


# --- Personal media must never reach the repository ---------------------------

# Photos and videos (plus HEIC); PNG, GIF and BMP stay allowed for UI assets.
PERSONAL_MEDIA_EXTENSIONS = sorted(
    config.RAW_EXTENSIONS
    | config.VIDEO_EXTENSIONS
    | {".jpg", ".jpeg", ".tif", ".tiff", ".heic", ".heif"}
)


def test_gitignore_covers_all_known_media_types():
    names = [f"photos/example{ext}" for ext in PERSONAL_MEDIA_EXTENSIONS]
    # NUL-separated (-z): text mode on Windows would append "\r" to every line.
    result = subprocess.run(
        ["git", "check-ignore", "--no-index", "--verbose", "--non-matching", "--stdin", "-z"],
        input="\0".join(names).encode(),
        cwd=REPO_ROOT,
        capture_output=True,
        check=False,
    )
    # Records of four fields: source, line number, pattern, path; a path that
    # no pattern matches comes with an empty source.
    fields = result.stdout.decode().split("\0")
    records = [fields[i : i + 4] for i in range(0, len(fields) - 1, 4)]
    assert len(records) == len(names)
    not_ignored = [path for source, _, _, path in records if not source]
    assert not_ignored == []


def test_pre_commit_blocks_all_known_media_types():
    text = (REPO_ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8")
    hook = re.search(r"id: forbid-media-files.*?files: '([^']+)'", text, re.S)
    pattern = re.compile(hook.group(1))
    unblocked = [ext for ext in PERSONAL_MEDIA_EXTENSIONS if not pattern.search(f"a/b{ext}")]
    assert unblocked == []
