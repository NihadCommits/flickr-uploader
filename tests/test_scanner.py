"""Tests for scanning the folder tree (flickr_uploader.scanner)."""

import os
import shutil
import sys

import pytest

from flickr_uploader import config, db, hashing, scanner

OLD_MTIME_NS = 1_600_000_000 * 10**9
HASH_A = "a" * 32

TREE = {
    "2026/2026-06 Portugal/Jpegs/IMG_0001.JPG": 100,
    "2026/2026-06 Portugal/Jpegs/IMG_0001.xmp": 10,  # sidecar: ignored
    "2026/2026-06 Portugal/RAWs/IMG_0001.CR3": 200,
    "2026/2026-06 Portugal/Videos/clip.mp4": 300,
    "2026/2026-06 Portugal/iphone.HEIC": 50,
    "2026/Thumbs.db": 5,  # system file: ignored
}


def make_files(base, files):
    """Create files (relative path -> size in bytes) with a fixed old mtime."""
    for rel_path, size in files.items():
        path = base.joinpath(*rel_path.split("/"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * size)
        os.utime(path, ns=(OLD_MTIME_NS, OLD_MTIME_NS))


@pytest.fixture
def photos(tmp_path):
    base = tmp_path / "Bilder"
    make_files(base, TREE)
    (base / "2026" / "leer").mkdir()
    return base


def scan(conn, base, **kwargs):
    root = scanner.add_root(conn, base)
    return root, scanner.scan_root(conn, root, **kwargs)


def files(conn):
    rows = conn.execute(
        "SELECT d.rel_path, f.name, f.status FROM files f JOIN folders d ON d.id = f.folder_id"
    )
    return {scanner._child(row[0], row[1]): row[2] for row in rows}


def folders(conn):
    return {row[0]: row[1] for row in conn.execute("SELECT rel_path, missing FROM folders")}


def file_row(conn, rel_path):
    folder, _, name = rel_path.rpartition("/")
    return conn.execute(
        "SELECT f.* FROM files f JOIN folders d ON d.id = f.folder_id "
        "WHERE d.rel_path = ? AND f.name = ?",
        (folder, name),
    ).fetchone()


def folder_row(conn, rel_path):
    return conn.execute("SELECT * FROM folders WHERE rel_path = ?", (rel_path,)).fetchone()


def mark_hashed(conn, rel_path):
    conn.execute(
        "INSERT OR IGNORE INTO photos (hash, state, updated_at) VALUES (?, 'pending', ?)",
        (HASH_A, db.utc_now()),
    )
    conn.execute(
        "UPDATE files SET hash = ?, status = 'hashed', taken_at = '2026:06:01 10:00:00' "
        "WHERE id = ?",
        (HASH_A, file_row(conn, rel_path)["id"]),
    )


def assign_album(conn, rel_path, title="2026-06 Portugal"):
    album = conn.execute(
        "INSERT INTO albums (title, state, updated_at) VALUES (?, 'planned', ?)",
        (title, db.utc_now()),
    ).lastrowid
    conn.execute("UPDATE folders SET album_id = ? WHERE rel_path = ?", (album, rel_path))
    return album


# --- First scan ---------------------------------------------------------------


def test_first_scan_records_folders_and_classifies_files(conn, photos):
    _, report = scan(conn, photos)

    assert folders(conn) == {
        "": 0,
        "2026": 0,
        "2026/2026-06 Portugal": 0,
        "2026/2026-06 Portugal/Jpegs": 0,
        "2026/2026-06 Portugal/RAWs": 0,
        "2026/2026-06 Portugal/Videos": 0,
        "2026/leer": 0,
    }
    assert files(conn) == {
        "2026/2026-06 Portugal/Jpegs/IMG_0001.JPG": "new",
        "2026/2026-06 Portugal/RAWs/IMG_0001.CR3": "skipped_raw",
        "2026/2026-06 Portugal/Videos/clip.mp4": "new",
        "2026/2026-06 Portugal/iphone.HEIC": "skipped_unsupported",
    }
    assert (report.folders, report.files, report.new_files) == (7, 4, 4)
    assert not report.cancelled


def test_scan_records_size_and_mtime(conn, photos):
    scan(conn, photos)
    row = file_row(conn, "2026/2026-06 Portugal/Jpegs/IMG_0001.JPG")
    assert (row["size_bytes"], row["mtime_ns"]) == (100, OLD_MTIME_NS)


def test_too_large_files_are_skipped(conn, photos, monkeypatch):
    monkeypatch.setattr(config, "MAX_PHOTO_BYTES", 99)
    scan(conn, photos)
    assert files(conn)["2026/2026-06 Portugal/Jpegs/IMG_0001.JPG"] == "skipped_too_large"


def test_complete_scan_sets_scanned_at(conn, photos):
    root, _ = scan(conn, photos)
    scanned_at = conn.execute("SELECT scanned_at FROM roots WHERE id = ?", (root,)).fetchone()[0]
    assert scanned_at is not None


def test_system_folders_are_skipped(conn, photos):
    make_files(photos, {"$RECYCLE.BIN/deleted.jpg": 10, "System Volume Information/x.jpg": 10})
    scan(conn, photos)
    assert not any(path.startswith(("$", "System")) for path in folders(conn))


# --- Rescans ------------------------------------------------------------------


def test_rescan_without_changes_changes_nothing(conn, photos):
    root, _ = scan(conn, photos)
    before = conn.execute("SELECT * FROM files ORDER BY id").fetchall()

    report = scanner.scan_root(conn, root)

    assert (report.new_files, report.changed_files, report.missing_files) == (0, 0, 0)
    assert [tuple(r) for r in conn.execute("SELECT * FROM files ORDER BY id")] == [
        tuple(r) for r in before
    ]


def test_unchanged_hashed_file_keeps_its_hash(conn, photos):
    root, _ = scan(conn, photos)
    mark_hashed(conn, "2026/2026-06 Portugal/Jpegs/IMG_0001.JPG")
    scanner.scan_root(conn, root)
    row = file_row(conn, "2026/2026-06 Portugal/Jpegs/IMG_0001.JPG")
    assert (row["status"], row["hash"]) == ("hashed", HASH_A)


def test_changed_file_loses_everything_derived_from_the_old_content(conn, photos):
    rel_path = "2026/2026-06 Portugal/Jpegs/IMG_0001.JPG"
    root, _ = scan(conn, photos)
    mark_hashed(conn, rel_path)
    file_id = file_row(conn, rel_path)["id"]
    conn.execute(
        "INSERT INTO precheck_matches (file_id, flickr_id, confidence, checked_at) "
        "VALUES (?, '123', 'certain', ?)",
        (file_id, db.utc_now()),
    )
    conn.execute("UPDATE folders SET prechecked_at = ?", (db.utc_now(),))

    make_files(photos, {rel_path: 150})  # edited: new size
    report = scanner.scan_root(conn, root)

    row = file_row(conn, rel_path)
    assert (row["id"], row["status"], row["hash"], row["taken_at"]) == (file_id, "new", None, None)
    assert row["size_bytes"] == 150
    assert conn.execute("SELECT count(*) FROM precheck_matches").fetchone()[0] == 0
    assert folder_row(conn, "2026/2026-06 Portugal/Jpegs")["prechecked_at"] is None
    assert report.changed_files == 1


def test_new_file_in_a_checked_folder_needs_a_new_pre_check(conn, photos):
    root, _ = scan(conn, photos)
    conn.execute("UPDATE folders SET prechecked_at = ?", (db.utc_now(),))
    scanner.scan_root(conn, root)  # nothing new: stays checked
    assert folder_row(conn, "2026/2026-06 Portugal/Jpegs")["prechecked_at"] is not None

    make_files(photos, {"2026/2026-06 Portugal/Jpegs/IMG_0009.JPG": 30})
    scanner.scan_root(conn, root)
    assert folder_row(conn, "2026/2026-06 Portugal/Jpegs")["prechecked_at"] is None
    # Other folders keep their check.
    assert folder_row(conn, "2026/2026-06 Portugal")["prechecked_at"] is not None


def test_file_no_longer_skipped_needs_a_new_pre_check(conn, photos, monkeypatch):
    rel_path = "2026/2026-06 Portugal/Jpegs/IMG_0001.JPG"
    monkeypatch.setattr(config, "MAX_PHOTO_BYTES", 99)
    root, _ = scan(conn, photos)
    assert files(conn)[rel_path] == "skipped_too_large"
    conn.execute("UPDATE folders SET prechecked_at = ?", (db.utc_now(),))

    monkeypatch.undo()  # e.g. Flickr accepts more than before
    scanner.scan_root(conn, root)
    assert files(conn)[rel_path] == "new"
    assert folder_row(conn, "2026/2026-06 Portugal/Jpegs")["prechecked_at"] is None


def test_deleted_file_is_marked_missing_and_can_come_back(conn, photos):
    rel_path = "2026/2026-06 Portugal/Jpegs/IMG_0001.JPG"
    root, _ = scan(conn, photos)
    mark_hashed(conn, rel_path)
    path = photos.joinpath(*rel_path.split("/"))
    path.rename(path.with_suffix(".bak"))

    report = scanner.scan_root(conn, root)
    assert files(conn)[rel_path] == "missing"
    assert report.missing_files == 1

    path.with_suffix(".bak").rename(path)  # same content, same mtime
    scanner.scan_root(conn, root)
    row = file_row(conn, rel_path)
    assert (row["status"], row["hash"]) == ("hashed", HASH_A)


def test_deleted_folder_keeps_its_row_and_album(conn, photos):
    root, _ = scan(conn, photos)
    album = assign_album(conn, "2026/2026-06 Portugal")
    folder_id = folder_row(conn, "2026/2026-06 Portugal")["id"]
    shutil.move(photos / "2026" / "2026-06 Portugal", photos.parent / "away")

    report = scanner.scan_root(conn, root)

    assert folder_row(conn, "2026/2026-06 Portugal")["missing"] == 1
    assert folder_row(conn, "2026/2026-06 Portugal/Jpegs")["missing"] == 1
    assert folder_row(conn, "2026/2026-06 Portugal")["album_id"] == album
    assert set(files(conn).values()) == {"missing"}
    assert (report.missing_folders, report.missing_files) == (4, 4)

    shutil.move(photos.parent / "away", photos / "2026" / "2026-06 Portugal")
    scanner.scan_root(conn, root)
    row = folder_row(conn, "2026/2026-06 Portugal")
    assert (row["id"], row["missing"], row["album_id"]) == (folder_id, 0, album)
    assert "missing" not in files(conn).values()


def test_case_only_rename_keeps_folder_and_album(conn, photos):
    root, _ = scan(conn, photos)
    album = assign_album(conn, "2026/2026-06 Portugal")
    folder_id = folder_row(conn, "2026/2026-06 Portugal")["id"]
    os.rename(photos / "2026" / "2026-06 Portugal", photos / "2026" / "2026-06 PORTUGAL")

    report = scanner.scan_root(conn, root)

    row = folder_row(conn, "2026/2026-06 PORTUGAL")
    assert (row["id"], row["album_id"]) == (folder_id, album)
    assert folder_row(conn, "2026/2026-06 PORTUGAL/Jpegs") is not None
    assert (report.new_files, report.changed_files, report.missing_files) == (0, 0, 0)


# --- Things that must never be mistaken for "deleted" -------------------------


def test_unplugged_root_changes_nothing(conn, photos):
    root, _ = scan(conn, photos)
    before = files(conn)
    photos.rename(photos.parent / "unplugged")

    with pytest.raises(scanner.RootUnavailableError):
        scanner.scan_root(conn, root)
    assert files(conn) == before
    assert set(folders(conn).values()) == {0}


def test_unreadable_folder_and_everything_below_are_left_as_they_were(conn, photos, monkeypatch):
    root, _ = scan(conn, photos)
    before_files = files(conn)
    real_scandir = os.scandir

    def scandir(path):
        if os.fspath(path).endswith("2026-06 Portugal"):
            raise PermissionError(13, "Access denied", os.fspath(path))
        return real_scandir(path)

    monkeypatch.setattr(os, "scandir", scandir)
    report = scanner.scan_root(conn, root)

    # The subfolders could not be looked at, so they must not count as deleted.
    assert set(folders(conn).values()) == {0}
    assert files(conn) == before_files
    assert report.unreadable == [str(photos / "2026" / "2026-06 Portugal")]
    assert report.missing_folders == report.missing_files == 0
    event = conn.execute("SELECT level, message FROM events").fetchone()
    assert event["level"] == "warning"
    assert "2026-06 Portugal" in event["message"]


def test_cancelled_scan_keeps_progress_but_marks_nothing_missing(conn, photos):
    root, _ = scan(conn, photos)
    first_scan = conn.execute("SELECT scanned_at FROM roots").fetchone()[0]
    shutil.rmtree(photos / "2026" / "leer")
    make_files(photos, {"neu.jpg": 10})

    class CancelAfterFirstFolder:
        calls = 0

        def is_set(self):
            self.calls += 1
            return self.calls > 1

    report = scanner.scan_root(conn, root, cancel=CancelAfterFirstFolder())

    assert report.cancelled
    assert files(conn)["neu.jpg"] == "new"  # the root folder was done
    assert folders(conn)["2026/leer"] == 0  # not visited, so not marked missing
    assert conn.execute("SELECT scanned_at FROM roots").fetchone()[0] == first_scan


@pytest.mark.skipif(sys.platform != "win32", reason="junctions are Windows-only")
def test_junctions_are_not_followed(conn, photos):
    import _winapi

    loop = photos / "2026" / "loop"
    _winapi.CreateJunction(str(photos), str(loop))  # points back up the tree

    _, report = scan(conn, photos)

    assert "2026/loop" not in folders(conn)
    assert report.skipped_links == [str(loop)]
    assert conn.execute("SELECT count(*) FROM events WHERE level = 'info'").fetchone()[0] == 1


# --- Paths --------------------------------------------------------------------


def test_paths_longer_than_260_characters(conn, tmp_path):
    base = tmp_path / "Bilder"
    deep = "/".join(f"2026-06 Portugal Urlaub mit der Familie Teil {i}" for i in range(7))
    make_files(base, {f"{deep}/IMG_0001.JPG": 10})
    assert len(str(base / deep / "IMG_0001.JPG")) > 300

    scan(conn, base)

    assert files(conn) == {f"{deep}/IMG_0001.JPG": "new"}


@pytest.mark.skipif(sys.platform != "win32", reason="Windows path forms")
def test_file_access_uses_extended_paths(conn, photos, monkeypatch):
    # On a PC with LongPathsEnabled the long path test above passes either way;
    # this makes sure the scanner does not depend on that setting.
    listed = []
    real_scandir = os.scandir

    def scandir(path):
        listed.append(os.fspath(path))
        return real_scandir(path)

    monkeypatch.setattr(os, "scandir", scandir)
    scan(conn, photos)

    assert len(listed) == 7
    assert all(path.startswith("\\\\?\\") for path in listed)


def test_scanner_and_hashing_agree_on_size_and_mtime(conn, tmp_path):
    # Scanner and hashing must see identical values, or every run would
    # consider every file changed and hash 10 TB again.
    base = tmp_path / "Bilder"
    base.mkdir()
    (base / "IMG_0001.JPG").write_bytes(os.urandom(5000))  # real, fine-grained mtime

    scan(conn, base)

    row = file_row(conn, "IMG_0001.JPG")
    hashed = hashing.hash_file(config.filesystem_path(base / "IMG_0001.JPG"))
    assert (row["size_bytes"], row["mtime_ns"]) == (hashed.size_bytes, hashed.mtime_ns)


# --- Roots --------------------------------------------------------------------


def test_add_root_is_idempotent(conn, photos):
    root = scanner.add_root(conn, photos)
    assert scanner.add_root(conn, str(photos) + os.sep) == root
    if sys.platform == "win32":
        assert scanner.add_root(conn, str(photos).upper()) == root


def test_overlapping_roots_are_refused(conn, photos):
    scanner.add_root(conn, photos / "2026")
    with pytest.raises(scanner.OverlappingRootError):
        scanner.add_root(conn, photos)  # around the existing root
    with pytest.raises(scanner.OverlappingRootError):
        scanner.add_root(conn, photos / "2026" / "2026-06 Portugal")  # inside it


def test_root_with_a_shared_name_prefix_is_not_overlapping(conn, tmp_path):
    (tmp_path / "Bilder").mkdir()
    (tmp_path / "Bilder2").mkdir()
    assert scanner.add_root(conn, tmp_path / "Bilder") != scanner.add_root(
        conn, tmp_path / "Bilder2"
    )


def test_missing_root_folder_is_refused(conn, tmp_path):
    with pytest.raises(scanner.RootUnavailableError):
        scanner.add_root(conn, tmp_path / "does not exist")


def test_relocated_root_keeps_everything(conn, photos):
    root, _ = scan(conn, photos)
    before = conn.execute("SELECT id, rel_path FROM folders ORDER BY id").fetchall()
    moved = photos.parent / "E_Laufwerk" / "Bilder"
    moved.parent.mkdir()
    photos.rename(moved)  # like the drive coming back as E: instead of D:

    scanner.relocate_root(conn, root, moved)
    report = scanner.scan_root(conn, root)

    assert (report.new_files, report.changed_files, report.missing_files) == (0, 0, 0)
    after = conn.execute("SELECT id, rel_path FROM folders ORDER BY id").fetchall()
    assert [tuple(r) for r in after] == [tuple(r) for r in before]


def test_relocate_checks_its_target(conn, photos, tmp_path):
    root = scanner.add_root(conn, photos / "2026")
    (tmp_path / "Andere").mkdir()
    (tmp_path / "Frei").mkdir()
    scanner.add_root(conn, tmp_path / "Andere")

    with pytest.raises(db.RecordNotFoundError):
        scanner.relocate_root(conn, 999, tmp_path / "Frei")
    with pytest.raises(scanner.RootUnavailableError):
        scanner.relocate_root(conn, root, tmp_path / "does not exist")
    with pytest.raises(scanner.OverlappingRootError):
        scanner.relocate_root(conn, root, tmp_path / "Andere")  # same as the other root
    with pytest.raises(scanner.OverlappingRootError):
        scanner.relocate_root(conn, root, tmp_path)  # around the other root


# --- Summaries ----------------------------------------------------------------


def test_folder_summaries(conn, photos):
    root, _ = scan(conn, photos)
    summaries = {s.rel_path: s for s in scanner.folder_summaries(conn, root)}

    jpegs = summaries["2026/2026-06 Portugal/Jpegs"]
    assert jpegs.own == jpegs.total == scanner.Totals(files=1, bytes=100, skipped=0)
    portugal = summaries["2026/2026-06 Portugal"]
    assert portugal.own == scanner.Totals(files=1, bytes=50, skipped=1)  # the HEIC
    assert portugal.total == scanner.Totals(files=4, bytes=650, skipped=2)
    assert summaries[""].total == portugal.total
    assert summaries["2026/leer"].total == scanner.Totals()


def test_folder_summaries_leave_out_missing_things(conn, photos):
    root, _ = scan(conn, photos)
    shutil.rmtree(photos / "2026" / "2026-06 Portugal" / "Videos")
    scanner.scan_root(conn, root)
    summaries = {s.rel_path: s for s in scanner.folder_summaries(conn, root)}
    assert "2026/2026-06 Portugal/Videos" not in summaries
    assert summaries[""].total == scanner.Totals(files=3, bytes=350, skipped=2)
