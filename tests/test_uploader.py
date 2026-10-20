"""Tests for the upload run (flickr_uploader.uploader).

Photos are generated in tmp_path, Flickr is the in-memory FakeFlickr, and time
is a FakeClock, so waiting for Flickr's search index takes no real time.
"""

import shutil
import threading
from datetime import UTC, datetime, timedelta

import pytest
from PIL import ExifTags, Image

from fake_flickr import FakeFlickr
from flickr_uploader import albums, db, hashing, jobs, precheck, scanner, uploader
from flickr_uploader.flickr_client import FlickrAuthError, FoundPhoto, OutcomeUnknownError

COLORS = [(200, 0, 0), (0, 200, 0), (0, 0, 200)]


class FakeClock:
    def __init__(self):
        self.current = datetime.now(UTC)
        self.slept = 0.0

    def now(self):
        return self.current

    def sleep(self, seconds):
        self.slept += seconds
        self.current += timedelta(seconds=seconds)


def image(path, color, taken=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    exif = Image.Exif()
    if taken:
        exif.get_ifd(ExifTags.IFD.Exif)[ExifTags.Base.DateTimeOriginal] = taken
    Image.new("RGB", (32, 24), color).save(path, exif=exif)
    return path


@pytest.fixture
def flickr():
    return FakeFlickr()


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def trip(conn, tmp_path):
    """A ticked album folder: three dated photos, a video, a RAW and an HEIC file."""
    folder = tmp_path / "Bilder" / "2026-06 Portugal"
    for number, color in enumerate(COLORS, 1):
        image(folder / f"IMG_000{number}.JPG", color, f"2026:06:0{number} 10:00:00")
    (folder / "MVI_0004.MP4").write_bytes(b"not really a video")
    (folder / "IMG_0005.CR3").write_bytes(b"raw")
    (folder / "IMG_0006.HEIC").write_bytes(b"heic")
    scanner.scan_root(conn, scanner.add_root(conn, tmp_path / "Bilder"))
    tick(conn, "2026-06 Portugal")
    return folder


def tick(conn, rel_path, title=None):
    folder_id = folder_id_of(conn, rel_path)
    album_id = albums.assign_album(conn, folder_id, title)
    albums.set_included(conn, folder_id, True)
    return album_id


def run(database, flickr, clock, **kwargs):
    return uploader.run_upload(database, flickr, now=clock.now, sleep=clock.sleep, **kwargs)


def folder_id_of(conn, rel_path):
    return conn.execute("SELECT id FROM folders WHERE rel_path = ?", (rel_path,)).fetchone()[0]


def file_id(conn, name):
    return conn.execute("SELECT id FROM files WHERE name = ?", (name,)).fetchone()[0]


def photo_of(conn, name):
    return conn.execute(
        "SELECT p.* FROM photos p JOIN files f ON f.hash = p.hash WHERE f.name = ?", (name,)
    ).fetchone()


def uploaded_titles(flickr):
    return sorted(photo.title for photo in flickr.photos.values())


def the_album(flickr):
    (album,) = flickr.sets.values()
    return album


def messages(conn, level=None):
    query = "SELECT message FROM events" + (" WHERE level = ?" if level else "")
    return [row[0] for row in conn.execute(query, (level,) if level else ())]


def crashed_upload(conn, folder, name, started="2026-01-01T00:00:00+00:00"):
    """State left by a session that crashed while uploading `name`."""
    digest = hashing.hash_file(folder / name).digest
    conn.execute(
        "INSERT INTO photos (hash, state, attempts, upload_started_at, updated_at) "
        "VALUES (?, 'uploading', 1, ?, ?)",
        (digest, started, db.utc_now()),
    )
    conn.execute("UPDATE files SET hash = ?, status = 'hashed' WHERE name = ?", (digest, name))
    return digest


ALL = ["IMG_0001", "IMG_0002", "IMG_0003", "MVI_0004"]


# --- The normal case ---------------------------------------------------------------


def test_first_run_uploads_everything_into_a_new_album(database, conn, trip, flickr, clock):
    report = run(database, flickr, clock)
    assert report.status == "completed"
    assert (report.files, report.uploaded, report.skipped) == (6, 4, 2)
    assert (report.albums_created, report.added_to_albums) == (1, 4)
    assert uploaded_titles(flickr) == ALL
    assert all(photo.private for photo in flickr.photos.values())

    album = the_album(flickr)
    assert album.title == "2026-06 Portugal"
    assert album.primary == photo_of(conn, "IMG_0001.JPG")["flickr_id"]
    assert sorted(album.photos) == sorted(flickr.photos)

    for name in ("IMG_0001.JPG", "IMG_0002.JPG", "IMG_0003.JPG", "MVI_0004.MP4"):
        photo = photo_of(conn, name)
        assert (photo["state"], photo["origin"], photo["hash_tagged"]) == ("verified", "upload", 1)
        # The hash tag on Flickr is the digest of exactly this file.
        assert flickr.photos[photo["flickr_id"]].digests == {photo["hash"]}
    assert {row[0] for row in conn.execute("SELECT state FROM album_photos")} == {"verified"}
    run_row = conn.execute("SELECT * FROM runs").fetchone()
    assert run_row["status"] == "completed" and run_row["finished_at"]
    assert (run_row["kind"], run_row["items_total"], run_row["items_done"]) == ("upload", 6, 6)


def test_second_run_uploads_nothing(database, conn, trip, flickr, clock):
    run(database, flickr, clock)
    flickr.calls.clear()
    report = run(database, flickr, clock)
    assert (report.uploaded, report.already_done, report.albums_created) == (0, 4, 0)
    assert flickr.count("upload") == flickr.count("create_album") == 0


def test_raw_and_unsupported_files_are_skipped_and_logged(database, conn, trip, flickr, clock):
    run(database, flickr, clock)
    skipped = [m for m in messages(conn) if m.startswith("Skipped")]
    assert any("RAW" in m and m.endswith("IMG_0005.CR3") for m in skipped)
    assert any(m.endswith("IMG_0006.HEIC") for m in skipped)
    assert "IMG_0005" not in uploaded_titles(flickr)


def test_every_run_is_backed_up_first(database, trip, flickr, clock):
    run(database, flickr, clock)
    assert len(list(database.backup_dir.glob("state-*.db"))) == 1


def test_only_one_run_at_a_time(database, flickr, clock):
    with jobs.exclusive(), pytest.raises(jobs.JobInProgressError):
        run(database, flickr, clock)


# --- Decisions the user has to take ------------------------------------------------


def test_open_pre_check_match_holds_the_file_back(database, conn, trip, flickr, clock):
    old = flickr.add_photo("IMG_0002")
    flickr.old_photos.append(FoundPhoto(old, "IMG_0002", "2026-06-02 10:00:00", 32, 24))
    report = run(database, flickr, clock)
    assert report.waiting == 1
    assert "IMG_0002" not in [p.title for p in flickr.photos.values() if p.id != old]

    precheck.choose_upload(conn, file_id(conn, "IMG_0002.JPG"))
    report = run(database, flickr, clock)
    assert (report.uploaded, report.waiting) == (1, 0)


def test_file_added_to_a_done_folder_is_checked_before_upload(database, conn, trip, flickr, clock):
    run(database, flickr, clock)
    # Later, an old photo that is on Flickr since years lands in the folder.
    image(trip / "IMG_0007.JPG", (90, 90, 90), "2026:06:03 12:00:00")
    old = flickr.add_photo("IMG_0007")
    flickr.old_photos.append(FoundPhoto(old, "IMG_0007", "2026-06-03 12:00:00", 32, 24))
    scanner.scan_root(conn, conn.execute("SELECT id FROM roots").fetchone()[0])

    report = run(database, flickr, clock)
    assert (report.uploaded, report.waiting) == (0, 1)
    assert [p.title for p in flickr.photos.values()].count("IMG_0007") == 1


def test_taking_over_an_old_photo(database, conn, trip, flickr, clock):
    old = flickr.add_photo("IMG_0002", private=False)  # uploaded years ago, public
    flickr.old_photos.append(FoundPhoto(old, "IMG_0002", "2026-06-02 10:00:00", 32, 24))
    run(database, flickr, clock)
    precheck.choose_existing(conn, file_id(conn, "IMG_0002.JPG"), old)
    flickr.calls.clear()

    report = run(database, flickr, clock)
    assert (report.adopted, report.uploaded) == (1, 0)
    assert flickr.count("upload") == 0
    photo = photo_of(conn, "IMG_0002.JPG")
    assert (photo["state"], photo["origin"], photo["flickr_id"]) == ("verified", "adopted", old)
    assert photo["hash"] in flickr.photos[old].digests
    # Its visibility is the user's old decision: left alone.
    assert flickr.photos[old].private is False
    assert old in the_album(flickr).photos


def test_yellow_album_waits_for_the_users_decision(database, conn, trip, flickr, clock):
    flickr.add_album("2026-06 portugal")
    report = run(database, flickr, clock)
    assert (report.uploaded, report.waiting) == (0, 4)
    assert flickr.count("upload") == 0
    assert len([m for m in messages(conn, "warning") if "waits for your decision" in m]) == 1

    album_id = albums.album_folder(conn, folder_id_of(conn, "2026-06 Portugal"))["album_id"]
    albums.confirm_new_album(conn, album_id)
    report = run(database, flickr, clock)
    assert (report.uploaded, report.albums_created) == (4, 1)
    assert len(flickr.sets) == 2


def test_files_go_into_the_existing_flickr_album(database, conn, trip, flickr, clock):
    existing = flickr.add_album("2026-06 Portugal")
    report = run(database, flickr, clock)
    assert report.albums_created == 0
    assert len(flickr.sets) == 1
    assert len(flickr.sets[existing].photos) == 1 + 4


def test_folder_outside_any_album_folder_is_not_uploaded(database, conn, tmp_path, flickr, clock):
    image(tmp_path / "Bilder" / "Lose" / "IMG_0001.JPG", COLORS[0])
    scanner.scan_root(conn, scanner.add_root(conn, tmp_path / "Bilder"))
    conn.execute("UPDATE folders SET included = 1 WHERE rel_path = 'Lose'")
    report = run(database, flickr, clock)
    assert (report.uploaded, report.waiting) == (0, 1)


def test_file_changed_since_the_scan_waits_for_a_new_scan(database, conn, trip, flickr, clock):
    path = trip / "IMG_0002.JPG"
    path.write_bytes(path.read_bytes() + b"edited")
    report = run(database, flickr, clock)
    assert "IMG_0002" not in uploaded_titles(flickr)
    assert any("scan again" in m and m.endswith("IMG_0002.JPG") for m in messages(conn))
    assert report.waiting == 1


def test_hashed_file_changed_later_keeps_its_old_hash_off_flickr(
    database, conn, trip, flickr, clock
):
    path = trip / "IMG_0002.JPG"
    digest = hashing.hash_file(path).digest  # hashed in an earlier run
    conn.execute(
        "INSERT INTO photos (hash, state, updated_at) VALUES (?, 'pending', ?)",
        (digest, db.utc_now()),
    )
    conn.execute("UPDATE files SET hash = ?, status = 'hashed' WHERE name = ?", (digest, path.name))
    path.write_bytes(path.read_bytes() + b"edited")
    run(database, flickr, clock)
    assert "IMG_0002" not in uploaded_titles(flickr)


def test_same_content_in_two_folders_is_uploaded_once(database, conn, tmp_path, flickr, clock):
    first = image(tmp_path / "Bilder" / "A" / "IMG_0001.JPG", COLORS[0])
    (tmp_path / "Bilder" / "B").mkdir()
    shutil.copyfile(first, tmp_path / "Bilder" / "B" / "Kopie.JPG")
    scanner.scan_root(conn, scanner.add_root(conn, tmp_path / "Bilder"))
    tick(conn, "A")
    tick(conn, "B")
    report = run(database, flickr, clock)
    assert flickr.count("upload") == 1
    assert (report.uploaded, report.already_done) == (1, 1)
    (photo_id,) = flickr.photos
    assert sorted(a.title for a in flickr.sets.values()) == ["A", "B"]
    assert all(album.photos == [photo_id] for album in flickr.sets.values())


# --- Uploads without answer, crashes, duplicates -----------------------------------


def test_upload_without_answer_that_arrived_is_not_repeated(database, conn, trip, flickr, clock):
    flickr.lose_answer["upload"] = 1
    report = run(database, flickr, clock)
    assert flickr.count("upload") == 4
    assert uploaded_titles(flickr) == ALL
    # Before looking it up, the run gave the search index its time.
    assert clock.slept >= uploader.SEARCH_INDEX_GRACE.total_seconds() - 1
    assert (report.status, report.uploaded, report.unclear) == ("completed", 4, 0)
    assert photo_of(conn, "IMG_0001.JPG")["state"] == "verified"
    assert sorted(the_album(flickr).photos) == sorted(flickr.photos)


def test_upload_that_never_arrived_is_tried_again(database, conn, trip, flickr, clock):
    flickr.fail_before["upload"].append(OutcomeUnknownError("no answer"))
    report = run(database, flickr, clock)
    assert flickr.count("upload") == 5
    assert uploaded_titles(flickr) == ALL
    assert report.status == "completed"
    assert photo_of(conn, "IMG_0001.JPG")["attempts"] == 2


def test_upload_interrupted_by_a_crash_is_looked_up_first(database, conn, trip, flickr, clock):
    digest = crashed_upload(conn, trip, "IMG_0001.JPG")
    flickr.add_photo("IMG_0001", {digest})  # it did arrive before the crash
    report = run(database, flickr, clock)
    assert flickr.count("upload") == 3
    assert uploaded_titles(flickr) == ALL
    assert photo_of(conn, "IMG_0001.JPG")["state"] == "verified"
    assert report.status == "completed"


def test_search_index_lag_never_causes_a_second_upload(database, conn, trip, flickr, clock):
    # A crash moments ago: the photo arrived, but Flickr's search only finds it
    # after a while (measured: 26 s).
    started = clock.now() - timedelta(seconds=10)
    digest = crashed_upload(conn, trip, "IMG_0001.JPG", started.isoformat())
    arrived = flickr.add_photo("IMG_0001", {digest})
    real_find = flickr.find_by_hash

    def lagging_find(wanted):
        indexed = clock.now() - started >= timedelta(seconds=26)
        return [i for i in real_find(wanted) if i != arrived or indexed]

    flickr.find_by_hash = lagging_find
    report = run(database, flickr, clock)
    assert uploaded_titles(flickr) == ALL  # IMG_0001 only once
    assert photo_of(conn, "IMG_0001.JPG")["flickr_id"] == arrived
    assert report.status == "completed"


def test_crashed_upload_that_never_arrived_is_uploaded(database, conn, trip, flickr, clock):
    crashed_upload(conn, trip, "IMG_0001.JPG")  # long ago: no need to wait for the index
    run(database, flickr, clock)
    assert flickr.count("upload") == 4
    assert clock.slept == 0
    assert uploaded_titles(flickr) == ALL


def test_photo_found_twice_on_flickr_is_failed_and_reported(database, conn, trip, flickr, clock):
    digest = crashed_upload(conn, trip, "IMG_0001.JPG")
    flickr.add_photo("IMG_0001", {digest})
    flickr.add_photo("IMG_0001", {digest})
    report = run(database, flickr, clock)
    photo = photo_of(conn, "IMG_0001.JPG")
    assert photo["state"] == "failed" and "2 times" in photo["last_error"]
    assert report.failed == 1
    assert any("2 times" in m for m in messages(conn, "error"))
    assert flickr.count("upload") == 3

    uploader.reset_failed(conn, digest)
    photo = photo_of(conn, "IMG_0001.JPG")
    assert (photo["state"], photo["flickr_id"], photo["last_error"]) == ("pending", None, None)


# --- Verification --------------------------------------------------------------------


def test_upload_that_arrives_public_is_made_private(database, conn, trip, flickr, clock):
    flickr.upload_public = 1
    report = run(database, flickr, clock)
    assert all(photo.private for photo in flickr.photos.values())
    assert any("made private" in m for m in messages(conn, "warning"))
    assert report.failed == 0


def test_photo_that_stays_public_is_failed_loudly(database, conn, trip, flickr, clock):
    flickr.upload_public = 1
    flickr.make_private = lambda photo_id: None  # Flickr ignores the request
    report = run(database, flickr, clock)
    assert report.failed == 1
    assert photo_of(conn, "IMG_0001.JPG")["state"] == "failed"
    assert any("NOT private" in m for m in messages(conn, "error"))
    # Never put into an album as if all was well.
    assert photo_of(conn, "IMG_0001.JPG")["flickr_id"] not in the_album(flickr).photos


def test_upload_missing_on_flickr_is_failed(database, conn, trip, flickr, clock):
    vanish = ["first"]

    def lose_first(photo_id):
        if vanish:
            vanish.pop()
            del flickr.photos[photo_id]

    flickr.on_upload = lose_first
    report = run(database, flickr, clock)
    assert report.failed == 1
    photo = photo_of(conn, "IMG_0001.JPG")
    assert (photo["state"], photo["last_error"]) == ("failed", "missing on Flickr")


def test_upload_without_its_hash_tag_is_failed(database, conn, trip, flickr, clock):
    tagless = ["first"]

    def drop_tag(photo_id):
        if tagless:
            tagless.pop()
            flickr.photos[photo_id].digests.clear()

    flickr.on_upload = drop_tag
    report = run(database, flickr, clock)
    assert report.failed == 1
    photo = photo_of(conn, "IMG_0001.JPG")
    assert (photo["state"], photo["last_error"]) == ("failed", "not own or without its hash tag")


def test_photo_missing_from_its_album_is_added_again(database, conn, trip, flickr, clock):
    real_ids = flickr.album_photo_ids

    def lossy(album_id):  # the last photo got lost from the album on Flickr's side
        flickr.album_photo_ids = real_ids
        flickr.sets[album_id].photos.pop()
        return real_ids(album_id)

    flickr.album_photo_ids = lossy
    run(database, flickr, clock)
    assert any("not found in album" in m for m in messages(conn, "warning"))
    assert conn.execute("SELECT count(*) FROM album_photos WHERE state = 'pending'").fetchone()[0]

    run(database, flickr, clock)
    assert sorted(the_album(flickr).photos) == sorted(flickr.photos)
    assert {row[0] for row in conn.execute("SELECT state FROM album_photos")} == {"verified"}


# --- Albums ---------------------------------------------------------------------------


def test_album_creation_without_answer_is_settled_in_the_same_run(
    database, conn, trip, flickr, clock
):
    flickr.lose_answer["create_album"] = 1
    report = run(database, flickr, clock)
    assert report.status == "completed"
    album = the_album(flickr)  # exactly one, despite the lost answer
    assert sorted(album.photos) == sorted(flickr.photos)
    assert conn.execute("SELECT state FROM albums").fetchone()[0] == "exists"
    assert {row[0] for row in conn.execute("SELECT state FROM album_photos")} == {"verified"}


def test_album_deleted_on_flickr_is_created_again(database, conn, trip, flickr, clock):
    run(database, flickr, clock)
    flickr.sets.clear()
    report = run(database, flickr, clock)
    assert (report.uploaded, report.albums_created) == (0, 1)
    assert sorted(the_album(flickr).photos) == sorted(flickr.photos)


# --- Ending a run early ----------------------------------------------------------------


def test_cancel_finishes_the_current_photo_and_stops(database, conn, trip, flickr, clock):
    cancel = threading.Event()
    flickr.on_upload = lambda photo_id: cancel.set()
    report = run(database, flickr, clock, cancel=cancel)
    assert report.status == "aborted"
    assert flickr.count("upload") == 1
    assert photo_of(conn, "IMG_0001.JPG")["state"] == "verified"
    assert conn.execute("SELECT status FROM runs").fetchone()[0] == "aborted"

    report = run(database, flickr, clock)
    assert (report.status, report.uploaded) == ("completed", 3)


def test_lasting_connection_loss_ends_the_run(database, conn, trip, flickr, clock):
    flickr.fail_before["upload"].extend(OutcomeUnknownError("no answer") for _ in range(3))
    report = run(database, flickr, clock)
    assert report.status == "aborted"
    assert flickr.count("upload") == 3  # the video was not even tried
    assert any("no connection" in m for m in messages(conn, "error"))

    report = run(database, flickr, clock)
    assert report.status == "completed"
    assert uploaded_titles(flickr) == ALL


def test_rejected_login_stops_the_run_and_loses_nothing(database, conn, trip, flickr, clock):
    flickr.fail_before["upload"].append(FlickrAuthError("rejected"))
    report = run(database, flickr, clock)
    assert report.status == "aborted"
    assert flickr.count("upload") == 1
    # Nothing reached Flickr, so no lookup is needed before the next attempt.
    assert photo_of(conn, "IMG_0001.JPG")["state"] == "pending"

    report = run(database, flickr, clock)
    assert (report.status, report.uploaded) == ("completed", 4)
    assert clock.slept == 0


def test_flickr_unreachable_at_the_start_ends_the_run(database, conn, trip, flickr, clock):
    from flickr_uploader.flickr_client import FlickrUnavailableError

    flickr.fail_before["albums"].append(FlickrUnavailableError("offline"))
    report = run(database, flickr, clock)
    assert report.status == "aborted"
    assert flickr.count("upload") == 0


def test_file_gone_after_the_scan_is_reported(database, conn, trip, flickr, clock):
    (trip / "IMG_0003.JPG").unlink()
    report = run(database, flickr, clock)
    assert report.failed == 1
    assert (
        conn.execute("SELECT status FROM files WHERE name = 'IMG_0003.JPG'").fetchone()[0]
        == "unreadable"
    )
    assert uploaded_titles(flickr) == ["IMG_0001", "IMG_0002", "MVI_0004"]


def test_unexpected_error_marks_the_run_crashed(database, conn, trip, flickr, clock):
    def broken(photo_id):
        raise RuntimeError("bug")

    flickr.photo_info = broken
    with pytest.raises(RuntimeError):
        run(database, flickr, clock)
    assert conn.execute("SELECT status FROM runs").fetchone()[0] == "crashed"
    # The id Flickr returned was committed before the crash.
    photo = photo_of(conn, "IMG_0001.JPG")
    assert photo["state"] == "uploaded" and photo["flickr_id"] in flickr.photos
