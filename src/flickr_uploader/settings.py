"""Settings of the user interface, stored in the database (spec, section "GUI").

One row per key in the table `settings`, the value as JSON. Only the keys
defined here exist. Each has a default and a check, so a missing, damaged or
outdated value falls back to the default instead of breaking the window.
Besides what the user sets, it also records which Flickr account the
database belongs to.

Never credentials: those live only in the Windows Credential Manager (auth.py).
"""

import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass

from flickr_uploader import db
from flickr_uploader.downloader import MEDIA_FILTERS

DOWNLOAD_TARGET = "download_target"  # last download folder, set by the folder dialog
DOWNLOAD_MEDIA = "download_media"  # filter preselected when an album is picked
SYNC_AT_START = "sync_albums_at_start"  # read the album list from Flickr at start
FLICKR_ACCOUNT = "flickr_account"  # {"nsid", "username"}: whose photos the database holds


@dataclass(frozen=True)
class _Setting:
    default: object
    valid: Callable[[object], bool]


_SETTINGS = {
    DOWNLOAD_TARGET: _Setting(None, lambda value: value is None or _text(value)),
    DOWNLOAD_MEDIA: _Setting("all", lambda value: value in MEDIA_FILTERS),
    SYNC_AT_START: _Setting(True, lambda value: isinstance(value, bool)),
    FLICKR_ACCOUNT: _Setting(None, lambda value: value is None or _account(value)),
}


class UnknownSettingError(KeyError):
    """There is no setting with this key."""


def get(conn: sqlite3.Connection, key: str) -> object:
    """The stored value, or the default if there is none (or none that is valid)."""
    setting = _setting(key)
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    if row is None:
        return setting.default
    try:
        value = json.loads(row["value"])
    except ValueError:
        return setting.default
    return value if setting.valid(value) else setting.default


def get_all(conn: sqlite3.Connection) -> dict[str, object]:
    return {key: get(conn, key) for key in _SETTINGS}


def put(conn: sqlite3.Connection, key: str, value: object) -> None:
    """Store a value at once; ValueError if it is not valid for this key."""
    if not _setting(key).valid(value):
        raise ValueError(f"invalid value for the setting {key!r}")
    conn.execute(
        "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT (key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
        (key, json.dumps(value), db.utc_now()),
    )


def _setting(key: str) -> _Setting:
    try:
        return _SETTINGS[key]
    except KeyError:
        raise UnknownSettingError(key) from None


def _text(value: object) -> bool:
    return isinstance(value, str) and value.strip() != ""


def _account(value: object) -> bool:
    return (
        isinstance(value, dict)
        and set(value) == {"nsid", "username"}
        and _text(value["nsid"])
        and isinstance(value["username"], str)
    )
