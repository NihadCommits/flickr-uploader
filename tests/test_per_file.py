"""Tests for per-file folders: one export folder, its photos spread over several albums."""

import sqlite3

import pytest

from fake_flickr import FakeFlickr
from flickr_uploader import albums, scanner
from test_uploader import COLORS, FakeClock, image, run

DAYS = ["2024:06:03 10:00:00", "2024:06:03 15:00:00", "2024:06:06 11:00:00", "2024:06:09 09:00:00"]


@pytest.fixture
def flickr():
    flickr = FakeFlickr()
    flickr.add_album("Porto")
    flickr.add_album("Lissabon")
    return flickr


@pytest.fixture
def export(conn, tmp_path, flickr):
    """A camera export of a whole trip, the albums of its phone photos on Flickr."""
    folder = tmp_path / "Bilder" / "Export"
    for number, taken in enumerate(DAYS, 1):
        image(folder / f"DSC_000{number}.JPG", COLORS[number % 3], taken)
    root_id = scanner.add_root(conn, tmp_path / "Bilder")
    scanner.scan_root(conn, root_id)
    albums.sync_with_flickr(conn, flickr)
    folder_id = conn.execute("SELECT id FROM folders WHERE rel_path = 'Export'").fetchone()[0]
    albums.set_per_file(conn, folder_id, True)
    return folder_id


def file_ids(conn):
    return {
        row["name"]: row["id"] for row in conn.execute("SELECT id, name FROM files ORDER BY name")
    }


def album_id(conn, title):
    return conn.execute("SELECT id FROM albums WHERE title = ?", (title,)).fetchone()[0]


def test_photos_go_into_the_albums_they_were_given(database, conn, export, flickr):
    ids = file_ids(conn)
    albums.assign_files(conn, [ids["DSC_0001.JPG"], ids["DSC_0002.JPG"]], album_id(conn, "Porto"))
    albums.assign_files(conn, [ids["DSC_0003.JPG"]], album_id(conn, "Lissabon"))
    # DSC_0004 stays without album: not uploaded.
    report = run(database, flickr, FakeClock())
    assert (report.uploaded, report.unassigned) == (3, 1)
    by_title = {album.title: album for album in flickr.sets.values()}
    # Without the cover photo the fake gives every album on Flickr.
    titles = {p.id: p.title for p in flickr.photos.values() if p.title != "old"}
    porto = sorted(titles[p] for p in by_title["Porto"].photos if p in titles)
    assert porto == ["DSC_0001", "DSC_0002"]
    assert [titles[p] for p in by_title["Lissabon"].photos if p in titles] == ["DSC_0003"]
    assert "DSC_0004" not in titles.values()


def test_a_new_album_for_some_photos(database, conn, export, flickr):
    ids = file_ids(conn)
    algarve = albums.plan_album(conn, "Algarve")
    assert albums.plan_album(conn, "Algarve") == algarve  # the same, not a second one
    albums.assign_files(conn, [ids["DSC_0004.JPG"]], algarve)
    run(database, flickr, FakeClock())
    assert sorted(album.title for album in flickr.sets.values()) == ["Algarve", "Lissabon", "Porto"]


def test_unused_planned_album_is_forgotten(conn, export):
    ids = file_ids(conn)
    algarve = albums.plan_album(conn, "Algarve")
    albums.assign_files(conn, [ids["DSC_0004.JPG"]], algarve)
    albums.assign_files(conn, [ids["DSC_0004.JPG"]], None)
    assert conn.execute("SELECT count(*) FROM albums WHERE title = 'Algarve'").fetchone()[0] == 0


def test_uploaded_photo_keeps_its_album(database, conn, export, flickr):
    ids = file_ids(conn)
    porto, lissabon = album_id(conn, "Porto"), album_id(conn, "Lissabon")
    albums.assign_files(conn, [ids["DSC_0001.JPG"]], porto)
    run(database, flickr, FakeClock())
    assert albums.assign_files(conn, [ids["DSC_0001.JPG"]], lissabon) == 0
    assert albums.album_of_file(conn, ids["DSC_0001.JPG"], export) == porto


def test_switching_off_forgets_the_assignments(conn, export):
    ids = file_ids(conn)
    algarve = albums.plan_album(conn, "Algarve")
    albums.assign_files(conn, list(ids.values()), algarve)
    albums.set_per_file(conn, export, False)
    assert conn.execute("SELECT count(*) FROM file_albums").fetchone()[0] == 0
    assert conn.execute(
        "SELECT per_file, included FROM folders WHERE id = ?", (export,)
    ).fetchone()[:] == (0, 0)
    assert conn.execute("SELECT count(*) FROM albums WHERE title = 'Algarve'").fetchone()[0] == 0


def test_per_file_and_album_folders_never_overlap(conn, export, tmp_path):
    with pytest.raises(sqlite3.IntegrityError):
        albums.assign_album(conn, export)  # the folder itself
    root = conn.execute("SELECT id FROM folders WHERE rel_path = ''").fetchone()[0]
    with pytest.raises(sqlite3.IntegrityError):
        albums.assign_album(conn, root)  # above it
    albums.set_per_file(conn, export, False)
    albums.assign_album(conn, root)
    with pytest.raises(sqlite3.IntegrityError):
        albums.set_per_file(conn, export, True)  # below an album folder


def test_files_of_other_folders_cannot_be_assigned(conn, export, tmp_path):
    image(tmp_path / "Bilder" / "Anderes" / "x.jpg", COLORS[0])
    scanner.scan_root(conn, conn.execute("SELECT id FROM roots").fetchone()[0])
    other = conn.execute("SELECT id FROM files WHERE name = 'x.jpg'").fetchone()[0]
    with pytest.raises(albums.AlbumError):
        albums.assign_files(conn, [other], album_id(conn, "Porto"))


def test_planned_album_merges_into_flickr_album_with_its_files(conn, export, flickr):
    ids = file_ids(conn)
    algarve = albums.plan_album(conn, "Algarve")
    albums.assign_files(conn, [ids["DSC_0004.JPG"]], algarve)
    flickr.add_album("Algarve")  # created on Flickr meanwhile, e.g. by the phone
    albums.sync_with_flickr(conn, flickr)
    target = conn.execute("SELECT id FROM albums WHERE title = 'Algarve'").fetchone()[0]
    assert albums.album_of_file(conn, ids["DSC_0004.JPG"], export) == target


# --- In the window ------------------------------------------------------------------


def window(database, flickr):
    from flickr_uploader.gui.api import Api

    return Api(database, client_factory=lambda **kwargs: flickr, background=lambda work: work())


def test_sorting_photos_in_the_window(database, conn, tmp_path, flickr):
    folder = tmp_path / "Bilder" / "Export"
    for number, taken in enumerate(DAYS, 1):
        image(folder / f"DSC_000{number}.JPG", COLORS[number % 3], taken)
    api = window(database, flickr)
    api._attach(
        type("W", (), {"create_file_dialog": lambda self, kind: [str(tmp_path / "Bilder")]})()
    )
    root_id = api.add_root()["root_id"]
    api.refresh_albums()
    folder_id = next(f["id"] for f in api.tree(root_id)["folders"] if f["path"] == "Export")

    assert "angehakt" in api.folder_files(folder_id)["error"]
    assert api.set_per_file(folder_id, True) == {}
    files = api.folder_files(folder_id)["files"]
    assert [(f["name"], f["taken"]) for f in files] == [
        ("DSC_0001.JPG", "2024-06-03 10:00:00"),
        ("DSC_0002.JPG", "2024-06-03 15:00:00"),
        ("DSC_0003.JPG", "2024-06-06 11:00:00"),
        ("DSC_0004.JPG", "2024-06-09 09:00:00"),
    ]
    assert api.thumbnail(files[0]["id"])["url"].startswith("data:image/jpeg;base64,")

    porto = next(a["id"] for a in api.album_choices() if a["title"] == "Porto")
    assert api.assign_files([files[0]["id"], files[1]["id"]], porto) == {"changed": 2, "kept": 0}
    made = api.assign_files_to_new_album([files[3]["id"]], "Algarve")
    assert made["changed"] == 1
    assert "leer" in api.assign_files_to_new_album([files[2]["id"]], " ")["error"]
    files = api.folder_files(folder_id)["files"]
    assert [f["album"] for f in files] == ["Porto", "Porto", None, "Algarve"]

    summary = next(f for f in api.tree(root_id)["folders"] if f["path"] == "Export")["per_file"]
    assert summary == {"files": 4, "assigned": 3, "albums": 2}
    plan = api.upload_plan()
    assert (plan["ready"], plan["unassigned"]) == (3, 1)

    root = next(f["id"] for f in api.tree(root_id)["folders"] if f["path"] == "")
    assert "einzeln" in api.make_album_folder(root)["error"]
    assert api.set_per_file(folder_id, False) == {}
    assert api.upload_plan()["files"] == 0


def test_dry_run_with_a_per_file_folder(conn, export):
    from flickr_uploader import dryrun

    ids = file_ids(conn)
    albums.assign_files(conn, [ids["DSC_0001.JPG"]], album_id(conn, "Porto"))
    result = dryrun.dry_run(conn)
    assert [(a.title, [f.name for f in a.files]) for a in result.albums] == [
        ("Porto", ["DSC_0001.JPG"])
    ]
    assert {f.action for f in result.outside} == {dryrun.UNASSIGNED}
    assert result.totals["uploads"] == 1
