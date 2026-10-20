"""Tests for albums (flickr_uploader.albums): album folders, traffic light, sync, creation.

Flickr is the in-memory FakeFlickr; folders are real, generated in tmp_path.
"""

import sqlite3
import threading
import time

import pytest

from fake_flickr import FakeFlickr
from flickr_uploader import albums, db, scanner
from flickr_uploader.flickr_client import (
    FlickrRejectedError,
    FlickrUnavailableError,
    OutcomeUnknownError,
)

HASH = "3898db5d5b4a87468b544a01c6d30e1c"


@pytest.fixture
def flickr():
    return FakeFlickr()


@pytest.fixture
def tree(conn, tmp_path):
    """Root 'Bilder' with two trip folders, one of them with subfolders."""
    base = tmp_path / "Bilder"
    for folder in ("2026-06 Portugal/Jpegs", "2026-06 Portugal/Videos", "2026-07 Wandern"):
        (base / folder).mkdir(parents=True)
    scanner.scan_root(conn, scanner.add_root(conn, base))
    return {row["rel_path"]: row["id"] for row in conn.execute("SELECT id, rel_path FROM folders")}


def album_row(conn, album_id):
    return conn.execute("SELECT * FROM albums WHERE id = ?", (album_id,)).fetchone()


def verified_photo(conn, flickr, digest=HASH):
    """A photo that is on (fake) Flickr and verified in the database."""
    flickr_id = flickr.add_photo("IMG_0001", {digest})
    conn.execute(
        "INSERT INTO photos (hash, state, flickr_id, updated_at) VALUES (?, 'verified', ?, ?)",
        (digest, flickr_id, db.utc_now()),
    )
    return flickr_id


def test_normalize_title():
    assert albums.normalize_title("  2026-06   Portugal ") == "2026-06 portugal"
    # Composed and decomposed umlauts are the same title.
    assert albums.normalize_title("München") == albums.normalize_title("München")


# --- Album folders ---------------------------------------------------------------


def test_assign_album_takes_the_folder_name(conn, tree):
    album_id = albums.assign_album(conn, tree["2026-06 Portugal"])
    row = album_row(conn, album_id)
    assert (row["title"], row["state"]) == ("2026-06 Portugal", "planned")


def test_folders_with_the_same_title_share_one_album(conn, tree):
    first = albums.assign_album(conn, tree["2026-06 Portugal"], "Sommer")
    second = albums.assign_album(conn, tree["2026-07 Wandern"], "Sommer")
    assert first == second
    assert conn.execute("SELECT count(*) FROM albums").fetchone()[0] == 1


def test_no_album_folder_inside_another(conn, tree):
    albums.assign_album(conn, tree["2026-06 Portugal"])
    with pytest.raises(sqlite3.IntegrityError, match="nested album folder"):
        albums.assign_album(conn, tree["2026-06 Portugal/Jpegs"])


def test_album_folder_of_a_subfolder(conn, tree):
    album_id = albums.assign_album(conn, tree["2026-06 Portugal"])
    found = albums.album_folder(conn, tree["2026-06 Portugal/Jpegs"])
    assert (found["id"], found["album_id"]) == (tree["2026-06 Portugal"], album_id)
    assert albums.album_folder(conn, tree["2026-07 Wandern"]) is None


def test_ticking_only_inside_an_album_folder(conn, tree):
    with pytest.raises(albums.NotInAlbumFolderError):
        albums.set_included(conn, tree["2026-07 Wandern"], True)
    albums.assign_album(conn, tree["2026-06 Portugal"])
    albums.set_included(conn, tree["2026-06 Portugal/Jpegs"], True)
    included = conn.execute(
        "SELECT included FROM folders WHERE id = ?", (tree["2026-06 Portugal/Jpegs"],)
    )
    assert included.fetchone()[0] == 1


def test_unassigning_unticks_everything_inside(conn, tree):
    album_id = albums.assign_album(conn, tree["2026-06 Portugal"])
    albums.set_included(conn, tree["2026-06 Portugal"], True)
    albums.set_included(conn, tree["2026-06 Portugal/Jpegs"], True)
    albums.assign_album(conn, tree["2026-07 Wandern"])
    albums.set_included(conn, tree["2026-07 Wandern"], True)

    albums.unassign_album(conn, tree["2026-06 Portugal"])
    included = dict(conn.execute("SELECT rel_path, included FROM folders").fetchall())
    assert included["2026-06 Portugal"] == included["2026-06 Portugal/Jpegs"] == 0
    assert included["2026-07 Wandern"] == 1  # another album folder is left alone
    assert album_row(conn, album_id) is None  # the unused plan is forgotten
    assert albums.album_folder(conn, tree["2026-06 Portugal/Jpegs"]) is None


def test_unassigning_keeps_an_album_on_flickr(conn, tree, flickr):
    flickr.add_album("2026-06 Portugal")
    albums.sync_with_flickr(conn, flickr)
    album_id = albums.assign_album(conn, tree["2026-06 Portugal"])
    albums.unassign_album(conn, tree["2026-06 Portugal"])
    assert album_row(conn, album_id)["state"] == "exists"


def test_choosing_another_album_forgets_the_unused_plan(conn, tree, flickr):
    planned = albums.assign_album(conn, tree["2026-06 Portugal"])
    flickr.add_album("Portugal (alt)")
    albums.sync_with_flickr(conn, flickr)
    existing = conn.execute("SELECT id FROM albums WHERE title = 'Portugal (alt)'").fetchone()[0]
    albums.use_album(conn, tree["2026-06 Portugal"], existing)
    assert album_row(conn, planned) is None
    assert albums.album_folder(conn, tree["2026-06 Portugal"])["album_id"] == existing


def test_retitle_only_planned_albums(conn, tree, flickr):
    album_id = albums.assign_album(conn, tree["2026-06 Portugal"])
    albums.retitle_planned(conn, album_id, "  Portugal 2026 ")
    assert album_row(conn, album_id)["title"] == "Portugal 2026"
    with pytest.raises(ValueError):
        albums.retitle_planned(conn, album_id, "   ")
    flickr.add_album("Auf Flickr")
    albums.sync_with_flickr(conn, flickr)
    existing = conn.execute("SELECT id FROM albums WHERE state = 'exists'").fetchone()[0]
    with pytest.raises(albums.AlbumError):
        albums.retitle_planned(conn, existing, "Neu")


# --- Traffic light ---------------------------------------------------------------


@pytest.mark.parametrize(
    "on_flickr, light",
    [
        ([], albums.BLUE),
        (["2026-06 Portugal"], albums.GREEN),
        (["2026-06 portugal"], albums.YELLOW),  # only equal after normalizing
        (["2026-06  Portugal"], albums.YELLOW),
        (["2026-06 Portugal", "2026-06 Portugal"], albums.YELLOW),  # which one?
        (["2026-06 Portugal", "2026-06 portugal"], albums.YELLOW),
        (["2026-07 Wandern"], albums.BLUE),
    ],
)
def test_lights(conn, tree, flickr, on_flickr, light):
    for title in on_flickr:
        flickr.add_album(title)
    # Planned before the sync, so the green one is not merged yet.
    conn.execute(
        "INSERT INTO albums (title, state, updated_at) VALUES ('2026-06 Portugal', 'planned', ?)",
        (db.utc_now(),),
    )
    album_id = conn.execute("SELECT id FROM albums").fetchone()[0]
    albums.sync_with_flickr(conn, flickr)
    if light == albums.GREEN:
        assert album_row(conn, album_id) is None  # merged into the Flickr album
    else:
        assert albums.lights(conn)[album_id] == light


def test_confirming_turns_yellow_into_blue(conn, tree, flickr):
    flickr.add_album("2026-06 portugal")
    album_id = albums.assign_album(conn, tree["2026-06 Portugal"])
    albums.sync_with_flickr(conn, flickr)
    assert albums.lights(conn)[album_id] == albums.YELLOW
    albums.confirm_new_album(conn, album_id)
    assert albums.lights(conn)[album_id] == albums.BLUE
    # A new title is a new question.
    albums.retitle_planned(conn, album_id, "2026-06  PORTUGAL")
    assert albums.lights(conn)[album_id] == albums.YELLOW


def test_two_planned_albums_with_similar_titles_are_yellow(conn, tree):
    first = albums.assign_album(conn, tree["2026-06 Portugal"], "Sommer")
    second = albums.assign_album(conn, tree["2026-07 Wandern"], "sommer")
    current = albums.lights(conn)
    assert current[first] == current[second] == albums.YELLOW


# --- Sync ------------------------------------------------------------------------


def test_sync_mirrors_the_flickr_albums(conn, flickr):
    first = flickr.add_album("2026-06 Portugal", description="Urlaub")
    report = albums.sync_with_flickr(conn, flickr)
    assert (report.flickr_albums, report.new) == (1, 1)
    flickr.sets[first].title = "2026-06 Portugal (Algarve)"
    report = albums.sync_with_flickr(conn, flickr)
    assert report.new == 0
    row = conn.execute("SELECT * FROM albums").fetchone()
    assert (row["title"], row["description"], row["flickr_id"], row["state"]) == (
        "2026-06 Portugal (Algarve)",
        "Urlaub",
        first,
        "exists",
    )
    assert row["created_by_tool"] == 0


def test_sync_keeps_how_much_content_an_album_has(conn, flickr):
    album = flickr.add_album("2026-06 Portugal")  # with one photo as cover
    flickr.add_media(album, "MVI_0001", media="video")
    albums.sync_with_flickr(conn, flickr)
    row = conn.execute("SELECT photo_count, video_count FROM albums").fetchone()
    assert tuple(row) == (1, 1)
    flickr.add_media(album, "MVI_0002", media="video")
    albums.sync_with_flickr(conn, flickr)
    assert conn.execute("SELECT video_count FROM albums").fetchone()[0] == 2


def test_green_album_is_merged_with_its_photos(conn, tree, flickr):
    flickr_album = flickr.add_album("2026-06 Portugal")
    planned = albums.assign_album(conn, tree["2026-06 Portugal"])
    verified_photo(conn, flickr)
    conn.execute(
        "INSERT INTO album_photos VALUES (?, ?, 'pending', ?)", (planned, HASH, db.utc_now())
    )
    report = albums.sync_with_flickr(conn, flickr)
    assert report.merged == 1
    target = conn.execute("SELECT id FROM albums WHERE flickr_id = ?", (flickr_album,)).fetchone()[
        0
    ]
    assert albums.album_folder(conn, tree["2026-06 Portugal"])["album_id"] == target
    pairs = conn.execute("SELECT album_id, state FROM album_photos").fetchall()
    assert [tuple(pair) for pair in pairs] == [(target, "pending")]


def test_an_album_missing_from_the_list_is_not_taken_as_deleted(conn, flickr):
    flickr.add_album("2026-06 Portugal")
    albums.sync_with_flickr(conn, flickr)
    real_albums = flickr.albums
    flickr.albums = lambda: []  # a short answer, but the album is still there
    report = albums.sync_with_flickr(conn, flickr)
    flickr.albums = real_albums
    assert report.deleted == 0
    assert conn.execute("SELECT state FROM albums").fetchone()[0] == "exists"


def test_failing_double_check_changes_nothing(conn, flickr):
    flickr.add_album("2026-06 Portugal")
    albums.sync_with_flickr(conn, flickr)
    flickr.sets.clear()
    flickr.fail_before["album_exists"].append(FlickrUnavailableError("offline"))
    with pytest.raises(FlickrUnavailableError):
        albums.sync_with_flickr(conn, flickr)
    assert conn.execute("SELECT state FROM albums").fetchone()[0] == "exists"


def test_deleted_album_in_use_is_planned_again(conn, tree, flickr):
    flickr_album = flickr.add_album("2026-06 Portugal")
    albums.sync_with_flickr(conn, flickr)
    album_id = albums.assign_album(conn, tree["2026-06 Portugal"])
    verified_photo(conn, flickr)
    conn.execute(
        "INSERT INTO album_photos VALUES (?, ?, 'verified', ?)", (album_id, HASH, db.utc_now())
    )
    del flickr.sets[flickr_album]
    report = albums.sync_with_flickr(conn, flickr)
    assert report.deleted == 1
    row = album_row(conn, album_id)
    assert (row["state"], row["flickr_id"]) == ("planned", None)
    assert conn.execute("SELECT state FROM album_photos").fetchone()[0] == "pending"


def test_deleted_album_nobody_uses_leaves_the_mirror(conn, flickr):
    flickr_album = flickr.add_album("Irgendwas")
    albums.sync_with_flickr(conn, flickr)
    del flickr.sets[flickr_album]
    albums.sync_with_flickr(conn, flickr)
    assert conn.execute("SELECT count(*) FROM albums").fetchone()[0] == 0


# --- Creating albums ---------------------------------------------------------------


def test_create_on_flickr(conn, tree, flickr):
    album_id = albums.assign_album(conn, tree["2026-06 Portugal"])
    cover = verified_photo(conn, flickr)
    flickr_id = albums.create_on_flickr(conn, flickr, album_id, HASH)
    assert flickr.sets[flickr_id].primary == cover
    row = album_row(conn, album_id)
    assert (row["state"], row["flickr_id"], row["created_by_tool"]) == ("exists", flickr_id, 1)
    assert row["description"] == ""
    # Asking again changes nothing on Flickr.
    assert albums.create_on_flickr(conn, flickr, album_id, HASH) == flickr_id
    assert flickr.count("create_album") == 1


def test_create_needs_a_verified_cover(conn, tree, flickr):
    album_id = albums.assign_album(conn, tree["2026-06 Portugal"])
    with pytest.raises(albums.AlbumError):
        albums.create_on_flickr(conn, flickr, album_id, HASH)
    assert album_row(conn, album_id)["state"] == "planned"


def test_yellow_album_is_not_created(conn, tree, flickr):
    flickr.add_album("2026-06 portugal")
    albums.sync_with_flickr(conn, flickr)
    album_id = albums.assign_album(conn, tree["2026-06 Portugal"])
    verified_photo(conn, flickr)
    assert albums.create_on_flickr(conn, flickr, album_id, HASH) is None
    assert flickr.count("create_album") == 0


def test_refused_creation_is_planned_again(conn, tree, flickr):
    album_id = albums.assign_album(conn, tree["2026-06 Portugal"])
    verified_photo(conn, flickr)
    flickr.fail_before["create_album"].append(FlickrRejectedError("nope", code=2))
    with pytest.raises(FlickrRejectedError):
        albums.create_on_flickr(conn, flickr, album_id, HASH)
    assert album_row(conn, album_id)["state"] == "planned"


@pytest.mark.parametrize("created_after_all", [True, False])
def test_creation_without_answer_is_settled_by_the_next_sync(conn, tree, flickr, created_after_all):
    album_id = albums.assign_album(conn, tree["2026-06 Portugal"])
    cover = verified_photo(conn, flickr)
    if created_after_all:
        flickr.lose_answer["create_album"] = 1
    else:
        flickr.fail_before["create_album"].append(OutcomeUnknownError("no answer"))
    # Another album must not be taken for the lost one.
    flickr.add_album("2026-06 Portugal (2)")
    assert albums.create_on_flickr(conn, flickr, album_id, HASH) is None
    assert album_row(conn, album_id)["state"] == "creating"
    # No second attempt while the outcome is unknown.
    assert albums.create_on_flickr(conn, flickr, album_id, HASH) is None
    assert flickr.count("create_album") == 1

    report = albums.sync_with_flickr(conn, flickr)
    assert report.reconciled == 1
    row = album_row(conn, album_id)
    if created_after_all:
        (created,) = [a for a in flickr.sets.values() if a.primary == cover]
        assert (row["state"], row["flickr_id"]) == ("exists", created.id)
    else:
        assert (row["state"], row["flickr_id"]) == ("planned", None)
    # The found album is not mirrored a second time.
    assert conn.execute("SELECT count(*) FROM albums").fetchone()[0] == 2


def test_same_title_with_another_cover_is_not_the_lost_album(conn, tree, flickr):
    album_id = albums.assign_album(conn, tree["2026-06 Portugal"])
    verified_photo(conn, flickr)
    flickr.fail_before["create_album"].append(OutcomeUnknownError("no answer"))
    albums.create_on_flickr(conn, flickr, album_id, HASH)
    other = flickr.add_album("2026-06 Portugal")  # someone else's cover photo
    albums.sync_with_flickr(conn, flickr)
    mirrored = conn.execute("SELECT * FROM albums WHERE flickr_id = ?", (other,)).fetchone()
    assert mirrored["created_by_tool"] == 0
    # Not created, so the plan stands; with exactly one album of that title on
    # Flickr it is green, and that album is used.
    assert albums.album_folder(conn, tree["2026-06 Portugal"])["album_id"] == mirrored["id"]


def test_album_creation_is_serialized(database, tree, flickr):
    with database.connect() as conn:
        album_id = albums.assign_album(conn, tree["2026-06 Portugal"])
        verified_photo(conn, flickr)
    real_create = flickr.create_album

    def slow_create(*args, **kwargs):
        time.sleep(0.2)  # a second creator would get in here without the lock
        return real_create(*args, **kwargs)

    flickr.create_album = slow_create
    results = []

    def create():
        with database.connect() as conn:
            results.append(albums.create_on_flickr(conn, flickr, album_id, HASH))

    threads = [threading.Thread(target=create) for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(flickr.sets) == 1
    # Everyone waited for the one creation and got its album.
    assert results == [next(iter(flickr.sets))] * 3


# --- Renaming ---------------------------------------------------------------------


def test_rename_on_flickr_keeps_the_description(conn, flickr):
    flickr_album = flickr.add_album("2026-06 Portugal", description="Urlaub")
    albums.sync_with_flickr(conn, flickr)
    album_id = conn.execute("SELECT id FROM albums").fetchone()[0]
    albums.rename_on_flickr(conn, flickr, album_id, "2026-06 Portugal und Spanien")
    assert (flickr.sets[flickr_album].title, flickr.sets[flickr_album].description) == (
        "2026-06 Portugal und Spanien",
        "Urlaub",
    )
    row = album_row(conn, album_id)
    assert (row["title"], row["pending_title"]) == ("2026-06 Portugal und Spanien", None)


def test_interrupted_rename_is_sent_again_by_the_next_sync(conn, flickr):
    flickr_album = flickr.add_album("Alt")
    albums.sync_with_flickr(conn, flickr)
    album_id = conn.execute("SELECT id FROM albums").fetchone()[0]
    flickr.fail_before["rename_album"].append(FlickrUnavailableError("offline"))
    with pytest.raises(FlickrUnavailableError):
        albums.rename_on_flickr(conn, flickr, album_id, "Neu")
    assert album_row(conn, album_id)["pending_title"] == "Neu"
    assert albums.sync_with_flickr(conn, flickr).renamed == 1
    assert flickr.sets[flickr_album].title == "Neu"
    assert album_row(conn, album_id)["title"] == "Neu"


def test_refused_rename_is_dropped_and_reported(conn, flickr):
    flickr.add_album("Alt")
    albums.sync_with_flickr(conn, flickr)
    album_id = conn.execute("SELECT id FROM albums").fetchone()[0]
    flickr.fail_before["rename_album"].append(FlickrRejectedError("nope", code=2))
    albums.rename_on_flickr(conn, flickr, album_id, "Neu")
    row = album_row(conn, album_id)
    assert (row["title"], row["pending_title"]) == ("Alt", None)
    event = conn.execute("SELECT level, message FROM events").fetchone()
    assert event["level"] == "error" and "Neu" in event["message"]


def test_planned_albums_are_not_renamed_on_flickr(conn, tree, flickr):
    album_id = albums.assign_album(conn, tree["2026-06 Portugal"])
    with pytest.raises(albums.AlbumError):
        albums.rename_on_flickr(conn, flickr, album_id, "Neu")
