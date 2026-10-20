"""Tests for the settings of the user interface (flickr_uploader.settings)."""

import pytest

from flickr_uploader import db, settings


def test_defaults_without_stored_values(conn):
    assert settings.get_all(conn) == {
        "download_target": None,
        "download_media": "all",
        "sync_albums_at_start": True,
        "flickr_account": None,
    }


def test_values_are_stored_at_once_and_replaced(conn):
    settings.put(conn, settings.DOWNLOAD_MEDIA, "videos")
    settings.put(conn, settings.DOWNLOAD_MEDIA, "photos")
    settings.put(conn, settings.SYNC_AT_START, False)
    settings.put(conn, settings.DOWNLOAD_TARGET, r"E:\Downloads")
    settings.put(conn, settings.FLICKR_ACCOUNT, {"nsid": "12345@N00", "username": "someone"})
    assert settings.get_all(conn) == {
        "download_target": r"E:\Downloads",
        "download_media": "photos",
        "sync_albums_at_start": False,
        "flickr_account": {"nsid": "12345@N00", "username": "someone"},
    }
    assert conn.execute("SELECT count(*) FROM settings").fetchone()[0] == 4


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("download_media", "raw"),
        ("download_media", None),
        ("sync_albums_at_start", "yes"),
        ("sync_albums_at_start", 1),
        ("download_target", ""),
        ("download_target", 5),
        ("flickr_account", "12345@N00"),
        ("flickr_account", {"nsid": "", "username": "someone"}),
        ("flickr_account", {"nsid": "12345@N00"}),
    ],
)
def test_invalid_values_are_refused(conn, key, value):
    with pytest.raises(ValueError):
        settings.put(conn, key, value)
    assert conn.execute("SELECT count(*) FROM settings").fetchone()[0] == 0


def test_unknown_keys_are_refused(conn):
    with pytest.raises(settings.UnknownSettingError):
        settings.put(conn, "theme", "xp")
    with pytest.raises(settings.UnknownSettingError):
        settings.get(conn, "theme")


@pytest.mark.parametrize("raw", ["not json", '"raw"', "null", "[1, 2]"])
def test_damaged_value_falls_back_to_the_default(conn, raw):
    conn.execute(
        "INSERT INTO settings (key, value, updated_at) VALUES ('download_media', ?, ?)",
        (raw, db.utc_now()),
    )
    assert settings.get(conn, settings.DOWNLOAD_MEDIA) == "all"
