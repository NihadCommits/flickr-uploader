"""Tests for downloading albums (flickr_uploader.downloader).

Flickr is the in-memory FakeFlickr; MP4/MOV files are synthetic (tests/mp4.py).
"""

import os
import threading
from datetime import UTC, datetime

import pytest

from fake_flickr import SECRET, FakeFlickr
from flickr_uploader import albums, db, downloader, filedates, jobs, scanner
from flickr_uploader.downloader import ALL, PHOTOS, VIDEOS, AlbumChoice
from flickr_uploader.flickr_client import FlickrAuthError, FlickrUnavailableError
from mp4 import movie_dates, quicktime

QUICKTIME_EPOCH = datetime(1904, 1, 1, tzinfo=UTC)
CAMERA_DATE = 3_000_000_000  # what an original video has in its own header
ORIGINAL_MOV = quicktime(created=CAMERA_DATE)
CONVERTED_MP4 = quicktime()  # Flickr's conversions carry no date


@pytest.fixture
def flickr():
    return FakeFlickr()


def new_album(flickr, title):
    album_id = flickr.add_album(title)
    flickr.sets[album_id].photos.clear()  # no stray cover photo in the content
    return album_id


@pytest.fixture
def library(conn, flickr):
    """Two albums on Flickr, mirrored into the database."""
    portugal = new_album(flickr, "2026-06 Portugal")
    flickr.add_media(portugal, "IMG_0001", taken="2026-06-01 10:00:00", original=b"jpeg one")
    flickr.add_media(
        portugal, "IMG_0002", taken="2026-06-02 11:00:00", original=None, converted=b"jpeg two"
    )
    flickr.add_media(
        portugal, "MVI_0003", media="video", taken="2026-06-03 12:00:00", original=ORIGINAL_MOV
    )
    flickr.add_media(
        portugal,
        "MVI_0004",
        media="video",
        taken="2026-06-04 13:00:00",
        original=None,
        converted=CONVERTED_MP4,
    )
    wandern = new_album(flickr, "2026-07 Wandern")
    flickr.add_media(wandern, "IMG_0101", taken="2026-07-01 09:00:00")
    flickr.add_media(
        wandern, "MVI_0102", media="video", taken=None, original=None, converted=CONVERTED_MP4
    )
    albums.sync_with_flickr(conn, flickr)
    return {
        row["title"]: row["id"]
        for row in conn.execute("SELECT id, title FROM albums WHERE state = 'exists'")
    }


def download(database, flickr, target, *choices, **kwargs):
    picked = [AlbumChoice(album_id, media) for album_id, media in choices]
    return downloader.run_download(database, flickr, picked, target, **kwargs)


def listing(folder):
    return sorted(name for name in os.listdir(folder))


def messages(conn):
    return [row[0] for row in conn.execute("SELECT message FROM events ORDER BY id")]


def seconds(taken):
    return int((filedates.taken_time(taken) - QUICKTIME_EPOCH).total_seconds())


# --- What lands where ---------------------------------------------------------------


def test_videos_only_into_the_albums_own_folder(database, conn, library, flickr, tmp_path):
    target = tmp_path / "Downloads"
    report = download(database, flickr, target, (library["2026-06 Portugal"], VIDEOS))
    assert report.status == "completed"
    assert listing(target) == ["2026-06 Portugal"]  # never straight into the target
    assert listing(target / "2026-06 Portugal") == ["MVI_0003.mov", "MVI_0004.mp4"]
    assert (report.albums, report.files, report.downloaded) == (1, 2, 2)


def test_every_album_has_its_own_filter(database, conn, library, flickr, tmp_path):
    report = download(
        database,
        flickr,
        tmp_path,
        (library["2026-06 Portugal"], PHOTOS),
        (library["2026-07 Wandern"], ALL),
    )
    assert listing(tmp_path / "2026-06 Portugal") == ["IMG_0001.jpg", "IMG_0002.jpg"]
    assert listing(tmp_path / "2026-07 Wandern") == ["IMG_0101.jpg", "MVI_0102.mp4"]
    assert report.downloaded == 4


def test_originals_first_otherwise_the_largest_version(database, conn, library, flickr, tmp_path):
    report = download(database, flickr, tmp_path, (library["2026-06 Portugal"], ALL))
    folder = tmp_path / "2026-06 Portugal"
    assert (folder / "IMG_0001.jpg").read_bytes() == b"jpeg one"  # url_o
    assert (folder / "IMG_0002.jpg").read_bytes() == b"jpeg two"  # Large 2048, not Medium
    assert (folder / "MVI_0003.mov").read_bytes() == ORIGINAL_MOV  # Video Original, untouched
    # 1080p rather than "Site MP4" or the still image; the date went into the header.
    converted = (folder / "MVI_0004.mp4").read_bytes()
    assert len(converted) == len(CONVERTED_MP4)
    assert report.not_original == 2
    assert any("no original" in m and m.endswith("MVI_0004.mp4") for m in messages(conn))
    # A photo's original comes with the album listing: no extra call for it.
    assert flickr.count("media_sizes") == 3


def test_recording_dates(database, conn, library, flickr, tmp_path):
    report = download(database, flickr, tmp_path, (library["2026-06 Portugal"], ALL))
    folder = tmp_path / "2026-06 Portugal"
    converted = (folder / "MVI_0004.mp4").read_bytes()
    assert movie_dates(converted) == (seconds("2026-06-04 13:00:00"),) * 2
    assert movie_dates((folder / "MVI_0003.mov").read_bytes()) == (CAMERA_DATE, CAMERA_DATE)
    for name, taken in [
        ("IMG_0001.jpg", "2026-06-01 10:00:00"),
        ("MVI_0004.mp4", "2026-06-04 13:00:00"),
    ]:
        info = os.stat(folder / name)
        assert abs(info.st_mtime - filedates.taken_time(taken).timestamp()) < 1
    assert (report.dated, report.undated) == (4, 0)


def test_unknown_date_is_not_made_up(database, conn, library, flickr, tmp_path):
    report = download(database, flickr, tmp_path, (library["2026-07 Wandern"], VIDEOS))
    video = (tmp_path / "2026-07 Wandern" / "MVI_0102.mp4").read_bytes()
    assert movie_dates(video) == (0, 0)
    assert report.undated == 1


def test_names_stay_unique_and_windows_safe(database, conn, flickr, tmp_path):
    first = new_album(flickr, "Ferien: 2026?")
    flickr.add_media(first, "Strand", original=b"one")
    flickr.add_media(first, "strand", original=b"two")  # same name for Windows
    blank = flickr.add_media(first, "", original=b"three")
    flickr.add_media(first, "CON", original=b"four")
    second = new_album(flickr, "Ferien: 2026?")  # Flickr allows duplicate titles
    flickr.add_media(second, "Berg", original=b"five")
    albums.sync_with_flickr(conn, flickr)
    ids = [row[0] for row in conn.execute("SELECT id FROM albums ORDER BY id")]
    target = tmp_path / "Downloads"
    download(database, flickr, target, *[(album_id, ALL) for album_id in ids])

    folders = listing(target)
    assert folders == sorted(f"Ferien_ 2026_ ({album})" for album in (first, second))
    names = listing(target / f"Ferien_ 2026_ ({first})")
    assert f"{blank}.jpg" in names and "_CON.jpg" in names
    assert len([n for n in names if n.lower().startswith("strand (")]) == 2


# --- Safeguards ----------------------------------------------------------------------


@pytest.mark.parametrize("where", ["Bilder", "Bilder/2026", "."])
def test_never_into_or_around_the_photo_archive(database, conn, library, flickr, tmp_path, where):
    (tmp_path / "Bilder" / "2026").mkdir(parents=True)
    scanner.add_root(conn, tmp_path / "Bilder")
    with pytest.raises(downloader.TargetError):
        download(database, flickr, tmp_path / where, (library["2026-06 Portugal"], ALL))
    assert conn.execute("SELECT count(*) FROM runs").fetchone()[0] == 0
    assert flickr.count("open_download") == 0


def test_next_to_the_archive_is_fine(database, conn, library, flickr, tmp_path):
    (tmp_path / "Bilder").mkdir()
    scanner.add_root(conn, tmp_path / "Bilder")
    report = download(database, flickr, tmp_path / "Downloads", (library["2026-07 Wandern"], ALL))
    assert report.status == "completed"


@pytest.mark.parametrize(
    "choices",
    [
        [],
        [AlbumChoice(999, ALL)],
        [AlbumChoice(1, "everything")],
    ],
)
def test_bad_choices_stop_before_anything_starts(
    database, conn, library, flickr, tmp_path, choices
):
    with pytest.raises(ValueError):
        downloader.run_download(database, flickr, choices, tmp_path)
    assert conn.execute("SELECT count(*) FROM runs").fetchone()[0] == 0


def test_the_same_album_twice_is_refused(database, conn, library, flickr, tmp_path):
    album_id = library["2026-06 Portugal"]
    with pytest.raises(ValueError):
        download(database, flickr, tmp_path, (album_id, VIDEOS), (album_id, PHOTOS))


def test_albums_not_on_flickr_cannot_be_downloaded(database, conn, library, flickr, tmp_path):
    conn.execute(
        "INSERT INTO albums (title, state, updated_at) VALUES ('Geplant', 'planned', ?)",
        (db.utc_now(),),
    )
    planned = conn.execute("SELECT id FROM albums WHERE state = 'planned'").fetchone()[0]
    with pytest.raises(ValueError):
        download(database, flickr, tmp_path, (planned, ALL))


def test_existing_files_are_never_overwritten(database, conn, library, flickr, tmp_path):
    folder = tmp_path / "2026-06 Portugal"
    folder.mkdir()
    (folder / "IMG_0001.png").write_bytes(b"my own file")  # same name, other type
    report = download(database, flickr, tmp_path, (library["2026-06 Portugal"], PHOTOS))
    assert (folder / "IMG_0001.png").read_bytes() == b"my own file"
    assert not (folder / "IMG_0001.jpg").exists()
    assert (report.existing, report.downloaded) == (1, 1)


def test_second_run_only_fills_gaps(database, conn, library, flickr, tmp_path):
    portugal = library["2026-06 Portugal"]
    download(database, flickr, tmp_path, (portugal, ALL))
    folder = tmp_path / "2026-06 Portugal"
    (folder / "IMG_0002.jpg").unlink()
    # An earlier download (like the old helper script) left a video without date.
    (folder / "MVI_0004.mp4").write_bytes(CONVERTED_MP4)
    flickr.calls.clear()

    report = download(database, flickr, tmp_path, (portugal, ALL))
    assert (report.downloaded, report.existing) == (1, 3)
    assert flickr.count("open_download") == 1
    assert movie_dates((folder / "MVI_0004.mp4").read_bytes())[0] == seconds("2026-06-04 13:00:00")
    assert any(m.startswith("Recording date added") for m in messages(conn))


def test_an_error_page_is_not_saved(database, conn, library, flickr, tmp_path):
    for served in flickr.served.values():
        if served.content == b"jpeg one":
            served.content, served.content_type = b"<html>oops</html>", "text/html"
    report = download(database, flickr, tmp_path, (library["2026-06 Portugal"], PHOTOS))
    assert listing(tmp_path / "2026-06 Portugal") == ["IMG_0002.jpg"]
    assert report.failed == 1


def test_an_incomplete_download_is_never_taken_for_the_file(
    database, conn, library, flickr, tmp_path
):
    (broken,) = [s for s in flickr.served.values() if s.content == b"jpeg one"]
    broken.length = 999  # announced more than arrives
    report = download(database, flickr, tmp_path, (library["2026-06 Portugal"], PHOTOS))
    folder = tmp_path / "2026-06 Portugal"
    assert listing(folder) == ["IMG_0001.jpg.part", "IMG_0002.jpg"]
    assert report.failed == 1

    broken.length = None
    download(database, flickr, tmp_path, (library["2026-06 Portugal"], PHOTOS))
    assert listing(folder) == ["IMG_0001.jpg", "IMG_0002.jpg"]
    assert (folder / "IMG_0001.jpg").read_bytes() == b"jpeg one"


def test_no_file_address_ever_reaches_the_log(database, conn, library, flickr, tmp_path):
    download(database, flickr, tmp_path, (library["2026-07 Wandern"], ALL))  # all fine
    for url, served in list(flickr.served.items()):
        if served.content == b"jpeg one":
            del flickr.served[url]  # HTTP 404
        served.cut = True  # the others fail midway
    download(database, flickr, tmp_path, (library["2026-06 Portugal"], ALL))
    logged = messages(conn)
    for start in ("Downloaded", "Not downloaded", "No connection"):
        assert any(m.startswith(start) for m in logged), start
    assert not any(SECRET in m or "flickr.com" in m for m in logged)


def test_flickr_is_only_read(database, conn, library, flickr, tmp_path):
    flickr.calls.clear()
    download(database, flickr, tmp_path, (library["2026-06 Portugal"], ALL))
    assert set(flickr.calls) <= {"album_media", "media_sizes", "open_download"}


# --- Jobs, aborts, trouble ---------------------------------------------------------


def test_the_run_is_recorded_for_the_progress_view(database, conn, library, flickr, tmp_path):
    download(database, flickr, tmp_path, (library["2026-06 Portugal"], ALL))
    run = conn.execute("SELECT * FROM runs").fetchone()
    assert (run["kind"], run["status"], run["items_total"], run["items_done"]) == (
        "download",
        "completed",
        4,
        4,
    )
    assert conn.execute("SELECT count(*) FROM events WHERE run_id = ?", (run["id"],)).fetchone()[0]


def test_only_one_job_at_a_time(database, library, flickr, tmp_path):
    with jobs.exclusive(), pytest.raises(jobs.JobInProgressError):
        download(database, flickr, tmp_path, (library["2026-06 Portugal"], ALL))


def test_cancel_stops_and_the_next_run_goes_on(database, conn, library, flickr, tmp_path):
    cancel = threading.Event()
    flickr.on_download = lambda url: cancel.set()
    report = download(database, flickr, tmp_path, (library["2026-06 Portugal"], ALL), cancel=cancel)
    assert report.status == "aborted"
    assert report.downloaded == 0
    flickr.on_download = None
    report = download(database, flickr, tmp_path, (library["2026-06 Portugal"], ALL))
    assert (report.status, report.downloaded) == ("completed", 4)
    assert not any(n.endswith(".part") for n in listing(tmp_path / "2026-06 Portugal"))


def test_lasting_connection_loss_ends_the_download(database, conn, library, flickr, tmp_path):
    flickr.fail_before["open_download"].extend(FlickrUnavailableError("offline") for _ in range(9))
    report = download(database, flickr, tmp_path, (library["2026-06 Portugal"], ALL))
    assert report.status == "aborted"
    assert flickr.count("open_download") == downloader.MAX_CONNECTION_FAILURES
    assert any("no connection" in m for m in messages(conn))


def test_rejected_login_ends_the_download(database, conn, library, flickr, tmp_path):
    flickr.fail_before["album_media"].append(FlickrAuthError("rejected"))
    report = download(database, flickr, tmp_path, (library["2026-06 Portugal"], ALL))
    assert report.status == "aborted"
    assert flickr.count("open_download") == 0


def test_an_album_gone_from_flickr_does_not_stop_the_others(
    database, conn, library, flickr, tmp_path
):
    gone = conn.execute(
        "SELECT flickr_id FROM albums WHERE id = ?", (library["2026-06 Portugal"],)
    ).fetchone()[0]
    del flickr.sets[gone]
    target = tmp_path / "Downloads"
    report = download(
        database,
        flickr,
        target,
        (library["2026-06 Portugal"], ALL),
        (library["2026-07 Wandern"], ALL),
    )
    assert report.status == "completed"
    assert listing(target) == ["2026-07 Wandern"]
    assert any("could not be read" in m for m in messages(conn))
