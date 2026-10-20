"""Tests for the pre-check against photos already on Flickr (flickr_uploader.precheck).

Test images are generated with Pillow in a temporary folder; Flickr is a fake.
"""

import itertools
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta

import pytest
from PIL import ExifTags, Image

from flickr_uploader import db, flickr_client, precheck, scanner
from flickr_uploader.flickr_client import FoundPhoto


def jpeg(path, taken=None, size=(60, 40), tag=ExifTags.Base.DateTimeOriginal):
    path.parent.mkdir(parents=True, exist_ok=True)
    exif = Image.Exif()
    if taken is not None:
        if tag == ExifTags.Base.DateTimeOriginal:
            exif.get_ifd(ExifTags.IFD.Exif)[tag] = taken
        else:
            exif[tag] = taken
    Image.new("RGB", size, (10, 20, 30)).save(path, exif=exif)
    return path


class FakeClient:
    """Answers photos_taken_between() from a list of FoundPhoto."""

    def __init__(self, photos=(), max_results=None, error=None):
        self.photos = list(photos)
        self.max_results = max_results
        self.error = error
        self.searches = []

    def photos_taken_between(self, start, end):
        self.searches.append((start, end))
        if self.error:
            raise self.error
        hits = [photo for photo in self.photos if start <= photo.taken <= end]
        if self.max_results is not None and len(hits) > self.max_results:
            raise flickr_client.SearchTooBroadError("too many")
        return hits


@pytest.fixture
def folder(conn, tmp_path):
    """A scanned album folder with two dated photos, a video and a RAW file."""
    base = tmp_path / "Bilder"
    portugal = base / "2026-06 Portugal"
    jpeg(portugal / "IMG_0001.JPG", "2026:06:01 10:00:00", (60, 40))
    jpeg(portugal / "IMG_0002.JPG", "2026:06:03 18:30:00", (40, 60))  # portrait
    (portugal / "MVI_0003.MP4").write_bytes(b"video")
    (portugal / "IMG_0004.CR3").write_bytes(b"raw")  # skipped: never checked
    scanner.scan_root(conn, scanner.add_root(conn, base))
    return conn.execute("SELECT id FROM folders WHERE rel_path = '2026-06 Portugal'").fetchone()[0]


def file_id(conn, name):
    return conn.execute("SELECT id FROM files WHERE name = ?", (name,)).fetchone()[0]


def matches(conn):
    rows = conn.execute(
        "SELECT f.name, m.flickr_id, m.confidence, m.decision "
        "FROM precheck_matches m JOIN files f ON f.id = m.file_id ORDER BY f.name, m.flickr_id"
    )
    return [tuple(row) for row in rows]


def found(photo_id, title, taken, width=None, height=None):
    return FoundPhoto(photo_id, title, taken, width, height)


# --- Reading metadata ---------------------------------------------------------


def test_read_metadata(tmp_path):
    path = jpeg(tmp_path / "IMG_0001.JPG", "2026:06:01 10:00:00", (60, 40))
    assert precheck.read_metadata(path) == precheck.Metadata("2026-06-01 10:00:00", 60, 40)


def test_read_metadata_falls_back_to_datetime(tmp_path):
    path = jpeg(tmp_path / "a.jpg", "2026:06:01 10:00:00", tag=ExifTags.Base.DateTime)
    assert precheck.read_metadata(path).taken == "2026-06-01 10:00:00"


def test_read_metadata_without_exif(tmp_path):
    path = tmp_path / "scan.png"
    Image.new("RGB", (30, 20)).save(path)
    assert precheck.read_metadata(path) == precheck.Metadata(None, 30, 20)


def test_videos_are_not_opened(tmp_path):
    # Would raise if it were opened: the file does not even exist.
    assert precheck.read_metadata(tmp_path / "gone.mp4") == precheck.Metadata(None, None, None)


def test_broken_image_is_an_os_error(tmp_path):
    path = tmp_path / "broken.jpg"
    path.write_bytes(b"not an image at all")
    with pytest.raises(OSError):
        precheck.read_metadata(path)


def test_read_metadata_beyond_260_characters(tmp_path):
    deep = tmp_path.joinpath(*[f"2026-06 Portugal Urlaub Teil {i}" for i in range(10)])
    path = jpeg(deep / "IMG_0001.JPG", "2026:06:01 10:00:00")
    assert len(str(path)) > 300
    assert precheck.read_metadata(path).taken == "2026-06-01 10:00:00"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026:06:01 10:00:00", "2026-06-01 10:00:00"),
        ("2026:06:01 10:00:00\x00", "2026-06-01 10:00:00"),  # NUL-terminated, seen in the wild
        (" 2026:06:01 10:00:00 ", "2026-06-01 10:00:00"),
        ("0000:00:00 00:00:00", None),  # camera without a set clock
        ("2026:13:01 10:00:00", None),
        ("    :  :     :  :  ", None),
        ("", None),
        (None, None),
        (b"2026:06:01 10:00:00", None),
    ],
)
def test_normalize_exif_time(value, expected):
    assert precheck.normalize_exif_time(value) == expected


# --- Matching -----------------------------------------------------------------


def test_certain_and_probable_matches(conn, folder):
    client = FakeClient(
        [
            found("1", "IMG_0001", "2026-06-01 10:00:00", 60, 40),  # all equal: certain
            found("2", "Sonnenuntergang", "2026-06-03 18:30:00", 40, 60),  # renamed: probable
            found("3", "IMG_9999", "2026-06-01 10:00:00", 800, 600),  # same second only
        ]
    )
    report = precheck.precheck_folder(conn, client, folder)

    assert matches(conn) == [
        ("IMG_0001.JPG", "1", "certain", "open"),
        ("IMG_0002.JPG", "2", "probable", "open"),
    ]
    assert (report.files, report.certain, report.probable) == (3, 1, 1)


def test_portrait_size_reported_rotated_still_matches(conn, folder):
    client = FakeClient([found("2", "IMG_0002", "2026-06-03 18:30:00", 60, 40)])
    precheck.precheck_folder(conn, client, folder)
    assert matches(conn) == [("IMG_0002.JPG", "2", "certain", "open")]


def test_title_match_without_size_is_probable(conn, folder):
    client = FakeClient([found("1", "img_0001", "2026-06-01 10:00:00")])  # no o_dims
    precheck.precheck_folder(conn, client, folder)
    assert matches(conn) == [("IMG_0001.JPG", "1", "probable", "open")]


def test_undated_video_matches_by_title_within_the_period(conn, folder):
    client = FakeClient(
        [
            found("7", "MVI_0003", "2026-06-02 12:00:00"),
            found("8", "MVI_0003", "2019-01-01 12:00:00"),  # far outside: not searched
        ]
    )
    report = precheck.precheck_folder(conn, client, folder)
    assert matches(conn) == [("MVI_0003.MP4", "7", "probable", "open")]
    assert report.without_date == 1


def test_search_covers_the_folder_period_plus_a_day(conn, folder):
    client = FakeClient()
    report = precheck.precheck_folder(conn, client, folder)
    assert client.searches == [("2026-05-31 10:00:00", "2026-06-04 18:30:00")]
    assert report.searched == client.searches[0]


def test_photos_the_tool_already_knows_are_no_matches(conn, folder):
    conn.execute(
        "INSERT INTO photos (hash, state, flickr_id, updated_at) VALUES (?, 'verified', '1', ?)",
        ("a" * 32, db.utc_now()),
    )
    client = FakeClient([found("1", "IMG_0001", "2026-06-01 10:00:00", 60, 40)])
    precheck.precheck_folder(conn, client, folder)
    assert matches(conn) == []


def test_period_is_split_while_flickr_would_cut_results(conn, folder):
    photos = [
        found("1", "IMG_0001", "2026-06-01 10:00:00", 60, 40),
        found("2", "IMG_0002", "2026-06-03 18:30:00", 40, 60),
        found("5", "x", "2026-06-02 00:00:00"),
    ]
    client = FakeClient(photos, max_results=1)

    precheck.precheck_folder(conn, client, folder)

    assert [m[1] for m in matches(conn)] == ["1", "2"]
    answered = [s for s in client.searches if sum(s[0] <= p.taken <= s[1] for p in photos) <= 1]
    # The answered periods follow each other without gap or overlap.
    for (_, end), (next_start, _) in itertools.pairwise(answered):
        gap = parse(next_start) - parse(end)
        assert gap == timedelta(seconds=1)


def parse(text):
    return datetime.strptime(text, precheck.FLICKR_TIME)


# --- Bookkeeping --------------------------------------------------------------


def test_metadata_is_stored_and_not_read_twice(conn, folder, monkeypatch):
    precheck.precheck_folder(conn, FakeClient(), folder)
    row = conn.execute(
        "SELECT taken_at, width, height FROM files WHERE name = 'IMG_0002.JPG'"
    ).fetchone()
    assert tuple(row) == ("2026-06-03 18:30:00", 40, 60)

    reads = []
    real = precheck.read_metadata
    monkeypatch.setattr(precheck, "read_metadata", lambda path: reads.append(path) or real(path))
    precheck.precheck_folder(conn, FakeClient(), folder)
    assert [str(path).rsplit("\\", 1)[-1].rsplit("/", 1)[-1] for path in reads] == ["MVI_0003.MP4"]


def test_folder_counts_as_checked_afterwards(conn, folder):
    assert (
        conn.execute("SELECT prechecked_at FROM folders WHERE id = ?", (folder,)).fetchone()[0]
        is None
    )
    precheck.precheck_folder(conn, FakeClient(), folder)
    checked = conn.execute("SELECT prechecked_at FROM folders WHERE id = ?", (folder,)).fetchone()[
        0
    ]
    assert checked is not None


def test_folder_without_any_dates_is_checked_without_searching(conn, tmp_path):
    base = tmp_path / "Bilder"
    (base / "Videos").mkdir(parents=True)
    (base / "Videos" / "MVI_0001.MP4").write_bytes(b"video")
    scanner.scan_root(conn, scanner.add_root(conn, base))
    videos = conn.execute("SELECT id FROM folders WHERE rel_path = 'Videos'").fetchone()[0]

    client = FakeClient()
    report = precheck.precheck_folder(conn, client, videos)

    assert client.searches == []
    assert report.without_date == 1
    assert precheck.is_cleared(conn, file_id(conn, "MVI_0001.MP4"))


def test_unreadable_photo_is_reported(conn, folder, tmp_path):
    (tmp_path / "Bilder" / "2026-06 Portugal" / "IMG_0001.JPG").write_bytes(b"broken")
    scanner.scan_root(conn, 1)
    report = precheck.precheck_folder(conn, FakeClient(), folder)
    assert report.unreadable == 1
    warning = conn.execute("SELECT message FROM events WHERE level = 'warning'").fetchone()[0]
    assert "IMG_0001.JPG" in warning


def test_database_stays_writable_while_files_are_read(conn, folder, database, monkeypatch):
    # Reading a large folder takes time; other writers must not be locked out.
    real = precheck.read_metadata

    def read_while_someone_writes(path):
        with closing(sqlite3.connect(database.path, timeout=0.1)) as other:
            other.execute("INSERT INTO roots (path) VALUES (?)", (str(path),))
            other.commit()
        return real(path)

    monkeypatch.setattr(precheck, "read_metadata", read_while_someone_writes)
    precheck.precheck_folder(conn, FakeClient(), folder)


def test_rerun_keeps_decisions_and_refreshes_suggestions(conn, folder):
    first = FakeClient(
        [
            found("1", "IMG_0001", "2026-06-01 10:00:00", 60, 40),
            found("2", "IMG_0002", "2026-06-03 18:30:00", 40, 60),
        ]
    )
    precheck.precheck_folder(conn, first, folder)
    precheck.choose_upload(conn, file_id(conn, "IMG_0001.JPG"))

    # Meanwhile photo 2 was deleted on Flickr and photo 3 appeared.
    second = FakeClient(
        [
            found("1", "IMG_0001", "2026-06-01 10:00:00", 60, 40),
            found("3", "IMG_0001", "2026-06-01 10:00:00"),
        ]
    )
    precheck.precheck_folder(conn, second, folder)

    assert matches(conn) == [
        ("IMG_0001.JPG", "1", "certain", "different"),  # decision kept
        ("IMG_0001.JPG", "3", "probable", "open"),  # new suggestion
    ]


def test_cancel_stores_what_was_read_but_checks_nothing(conn, folder):
    class CancelAfterFirstFile:
        calls = 0

        def is_set(self):
            self.calls += 1
            return self.calls > 1

    client = FakeClient()
    report = precheck.precheck_folder(conn, client, folder, cancel=CancelAfterFirstFile())

    assert report.cancelled
    assert client.searches == []
    taken = conn.execute("SELECT taken_at FROM files WHERE name = 'IMG_0001.JPG'").fetchone()[0]
    assert taken == "2026-06-01 10:00:00"
    assert not precheck.is_cleared(conn, file_id(conn, "IMG_0001.JPG"))


def test_flickr_error_leaves_the_folder_unchecked(conn, folder):
    client = FakeClient(error=flickr_client.FlickrUnavailableError("offline"))
    with pytest.raises(flickr_client.FlickrUnavailableError):
        precheck.precheck_folder(conn, client, folder)
    assert not precheck.is_cleared(conn, file_id(conn, "IMG_0001.JPG"))
    taken = conn.execute("SELECT taken_at FROM files WHERE name = 'IMG_0001.JPG'").fetchone()[0]
    assert taken is not None  # the reading work is not lost


def test_unknown_folder(conn):
    with pytest.raises(db.RecordNotFoundError):
        precheck.precheck_folder(conn, FakeClient(), 999)


# --- Decisions ----------------------------------------------------------------


@pytest.fixture
def two_candidates(conn, folder):
    client = FakeClient(
        [
            found("1", "IMG_0001", "2026-06-01 10:00:00", 60, 40),
            found("9", "IMG_0001", "2026-06-01 10:00:00"),  # uploaded twice back then
        ]
    )
    precheck.precheck_folder(conn, client, folder)
    return file_id(conn, "IMG_0001.JPG")


def test_open_match_blocks_the_upload(conn, two_candidates):
    assert not precheck.is_cleared(conn, two_candidates)
    assert precheck.is_cleared(conn, file_id(conn, "IMG_0002.JPG"))  # no matches at all


def test_choose_existing(conn, two_candidates):
    precheck.choose_existing(conn, two_candidates, "9")
    assert [(m[1], m[3]) for m in matches(conn)] == [("1", "different"), ("9", "same")]
    assert precheck.chosen_flickr_photo(conn, two_candidates) == "9"
    assert precheck.is_cleared(conn, two_candidates)


def test_choose_existing_only_among_the_matches(conn, two_candidates):
    with pytest.raises(db.RecordNotFoundError):
        precheck.choose_existing(conn, two_candidates, "12345")


def test_choose_upload_and_reset(conn, two_candidates):
    precheck.choose_upload(conn, two_candidates)
    assert precheck.is_cleared(conn, two_candidates)
    assert precheck.chosen_flickr_photo(conn, two_candidates) is None

    precheck.reset_choice(conn, two_candidates)
    assert not precheck.is_cleared(conn, two_candidates)


def test_changed_file_needs_a_new_check(conn, folder, tmp_path):
    precheck.precheck_folder(conn, FakeClient(), folder)
    photo = file_id(conn, "IMG_0001.JPG")
    assert precheck.is_cleared(conn, photo)

    jpeg(tmp_path / "Bilder" / "2026-06 Portugal" / "IMG_0001.JPG", "2026:06:01 10:00:00", (80, 60))
    scanner.scan_root(conn, 1)  # scanner notices the edit

    assert not precheck.is_cleared(conn, photo)
