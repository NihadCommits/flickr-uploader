"""Paths, limits and file types shared by all modules.

Everything the application stores lives outside the project folder, in
data_dir (%LOCALAPPDATA%\\flickr-uploader): database, backups, logs.
Machine-local on purpose: SQLite in WAL mode needs a local disk, and the
stored folder paths only make sense on this PC. Credentials are not files at
all; they live in the Windows Credential Manager (see auth.py).

Setting FLICKR_UPLOADER_HOME uses that folder instead, e.g. for a separate
test profile during development.
"""

import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

APP_DIR_NAME = "flickr-uploader"
HOME_ENV_VAR = "FLICKR_UPLOADER_HOME"

# --- Flickr limits ------------------------------------------------------------
# Source: flickrhelp.com, "Flickr upload requirements" (checked 2026-09-30).
# Flickr does not say whether "MB" means 10^6 or 2^20 bytes; the smaller
# reading is used so that no upload is started that Flickr would reject.
MB = 1000 * 1000
MAX_PHOTO_BYTES = 200 * MB
MAX_VIDEO_BYTES = 1000 * MB
# Wider images (extreme panoramas) are rejected by Flickr.
MAX_PHOTO_ASPECT_RATIO = 31.25
# Flickr API terms: at most 3600 calls per hour and API key.
MAX_API_CALLS_PER_HOUR = 3600
# What the tool allows itself: some headroom for the helper scripts, which use
# the same key, and for a restart (the counter lives in memory only).
API_CALL_BUDGET_PER_HOUR = 3400
# Flickr returns at most this many results for any one search query.
MAX_SEARCH_RESULTS = 4000
# How long after an unclear upload a search without hits is trusted to mean
# "not on Flickr". Measured on 2026-09-30: a new photo was found after 26 s;
# ten times that for safety.
SEARCH_INDEX_GRACE_SECONDS = 300

# --- File types ---------------------------------------------------------------
# Formats Flickr accepts. TIFF and BMP are stored as JPEG by Flickr. HEIC/HEIF
# is only accepted through the Flickr mobile app, not the API, so it counts as
# unsupported and has to be converted beforehand, like RAW files.
PHOTO_EXTENSIONS = frozenset({".jpg", ".jpeg", ".png", ".gif", ".tif", ".tiff", ".bmp"})
# .mts is the same container as .m2ts (AVCHD cameras); Flickr takes it
# although its help page only names M2TS (confirmed by the user's uploads).
VIDEO_EXTENSIONS = frozenset(
    {".mp4", ".avi", ".wmv", ".mov", ".mpg", ".mpeg", ".3gp", ".m2ts", ".mts", ".ogg", ".ogv"}
)
# Camera RAW formats (including RAW video). Never uploaded, never converted.
RAW_EXTENSIONS = frozenset(
    {
        ".3fr", ".ari", ".arw", ".braw", ".cr2", ".cr3", ".crw", ".dcr", ".dng",
        ".erf", ".fff", ".gpr", ".iiq", ".kdc", ".mef", ".mos", ".mrw", ".nef",
        ".nrw", ".orf", ".ori", ".pef", ".r3d", ".raf", ".raw", ".rw2", ".rwl",
        ".sr2", ".srf", ".srw", ".x3f",
    }
)  # fmt: skip
# Companion files of photos and videos that carry no picture of their own.
SIDECAR_EXTENSIONS = frozenset({".xmp", ".aae", ".thm", ".lrv", ".pp3", ".dop", ".cos", ".on1"})
# Files the operating system or photo tools drop into folders.
SYSTEM_FILENAMES = frozenset({"thumbs.db", "desktop.ini", ".ds_store", "picasa.ini", ".picasa.ini"})
# Folders Windows keeps at drive roots; never photos, and access is denied.
SYSTEM_DIRNAMES = frozenset({"$recycle.bin", "system volume information"})
# macOS writes "._<name>" resource-fork files next to the real ones on
# external drives; they carry the real file's extension but are no images.
APPLE_DOUBLE_PREFIX = "._"


class ConfigError(Exception):
    """The configured location can't be used safely."""


class MediaKind(StrEnum):
    PHOTO = "photo"
    VIDEO = "video"
    RAW = "raw"
    # Not reported at all: sidecars and system files are no media of their own.
    IGNORED = "ignored"
    # Reported as skipped: a file the user might expect to be uploaded.
    UNSUPPORTED = "unsupported"


def media_kind(filename: str) -> MediaKind:
    """Classify a file by its name only; the content is never opened here."""
    name = filename.casefold()
    if name in SYSTEM_FILENAMES or name.startswith(APPLE_DOUBLE_PREFIX):
        return MediaKind.IGNORED
    extension = os.path.splitext(name)[1]
    if extension in PHOTO_EXTENSIONS:
        return MediaKind.PHOTO
    if extension in VIDEO_EXTENSIONS:
        return MediaKind.VIDEO
    if extension in RAW_EXTENSIONS:
        return MediaKind.RAW
    if extension in SIDECAR_EXTENSIONS:
        return MediaKind.IGNORED
    return MediaKind.UNSUPPORTED


def is_too_large(kind: MediaKind, size_bytes: int) -> bool:
    """Tell whether Flickr would reject a file of this kind and size."""
    limit = {MediaKind.PHOTO: MAX_PHOTO_BYTES, MediaKind.VIDEO: MAX_VIDEO_BYTES}.get(kind)
    return limit is not None and size_bytes > limit


# --- Paths --------------------------------------------------------------------

_EXTENDED_PREFIX = "\\\\?\\"
_DEVICE_PREFIX = "\\\\.\\"


def filesystem_path(path: str | Path) -> str:
    r"""Return the form of `path` to hand to the operating system for file access.

    On Windows this is the extended-length form (\\?\C:\...), which works beyond
    260 characters whether or not LongPathsEnabled is set, and also reaches
    names ending in a space or dot. Windows takes such paths literally (a single
    "/" makes them invalid), so the path is made absolute and normalized first.
    Only for file access: store and display the plain path.
    """
    absolute = os.path.abspath(path)
    if sys.platform != "win32" or absolute.startswith((_EXTENDED_PREFIX, _DEVICE_PREFIX)):
        return absolute
    if absolute.startswith("\\\\"):  # UNC: \\server\share -> \\?\UNC\server\share
        return _EXTENDED_PREFIX + "UNC" + absolute[1:]
    return _EXTENDED_PREFIX + absolute


@dataclass(frozen=True)
class AppPaths:
    data_dir: Path

    @property
    def database(self) -> Path:
        return self.data_dir / "state.db"

    @property
    def backups(self) -> Path:
        return self.data_dir / "backups"

    @property
    def logs(self) -> Path:
        return self.data_dir / "logs"


def app_paths(environ: Mapping[str, str] | None = None) -> AppPaths:
    """Work out where the application keeps its files, and check that it is safe.

    Only computes paths; nothing is created on disk.
    """
    env = os.environ if environ is None else environ
    home = env.get(HOME_ENV_VAR)
    if home:
        data_dir = Path(home).expanduser()
        if not data_dir.is_absolute():
            raise ConfigError(f"{HOME_ENV_VAR} must be an absolute path, got {home!r}")
        # Checked first because it needs no disk access: looking for .git on a
        # disconnected share would hang until the network times out.
        _check_local(data_dir)
        # A custom location could point anywhere, including this repository.
        # The Windows default can't, and is not checked: a home directory
        # managed with git (dotfiles) must not stop the application.
        repo = _enclosing_git_repo(data_dir)
        if repo is not None:
            raise ConfigError(
                f"{data_dir} is inside the git repository {repo}; application data "
                "must never be stored there"
            )
    else:
        local = env.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        data_dir = Path(local) / APP_DIR_NAME
        _check_local(data_dir)
    return AppPaths(data_dir)


def _check_local(data_dir: Path) -> None:
    if _is_network_location(data_dir):
        raise ConfigError(f"{data_dir} is on a network drive; the database needs a local disk")


def _enclosing_git_repo(path: Path) -> Path | None:
    # abspath instead of resolve(): no disk access, so it can't hang on a
    # disconnected drive.
    absolute = Path(os.path.abspath(path))
    for candidate in (absolute, *absolute.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


DRIVE_REMOTE = 4  # GetDriveTypeW result for network drives


def _is_network_location(path: Path) -> bool:
    drive = Path(os.path.abspath(path)).drive
    # UNC paths (\\server\share) are network locations by definition; the
    # \\?\ and \\.\ prefixes mark local device paths instead.
    if drive.startswith("\\\\") and not drive.startswith(("\\\\?\\", "\\\\.\\")):
        return True
    if sys.platform != "win32" or not drive:
        return False
    return _drive_type(drive + "\\") == DRIVE_REMOTE


def _drive_type(root: str) -> int:
    import ctypes

    return ctypes.windll.kernel32.GetDriveTypeW(root)
