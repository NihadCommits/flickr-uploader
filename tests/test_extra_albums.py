"""Tests for extra albums: one photo uploaded once, in several albums."""

import pytest

from fake_flickr import FakeFlickr
from flickr_uploader import albums, db, scanner
from test_uploader import COLORS, FakeClock, image, run, tick


@pytest.fixture
def flickr():
    return FakeFlickr()


@pytest.fixture
def porto(conn, tmp_path):
    """An album folder with three photos, ticked."""
    folder = tmp_path / "Bilder" / "Porto"
    for number in range(1, 4):
        image(folder / f"IMG_000{number}.JPG", COLORS[number - 1], f"2024:06:0{number} 10:00:00")
    scanner.scan_root(conn, scanner.add_root(conn, tmp_path / "Bilder"))
    tick(conn, "Porto")
    return {r["name"]: r["id"] for r in conn.execute("SELECT id, name FROM files")}


def photos_in(flickr, title):
    (album,) = [a for a in flickr.sets.values() if a.title == title]
    return sorted(flickr.photos[p].title for p in album.photos)


def test_chosen_beforehand_uploaded_once_in_both_albums(database, conn, porto, flickr):
    instagram = albums.plan_album(conn, "Instagram")
    assert albums.add_extra(conn, [porto["IMG_0001.JPG"]], instagram) == 1
    report = run(database, flickr, FakeClock())
    assert report.uploaded == 3  # not 4
    assert photos_in(flickr, "Porto") == ["IMG_0001", "IMG_0002", "IMG_0003"]
    assert photos_in(flickr, "Instagram") == ["IMG_0001"]


def test_chosen_afterwards_only_added(database, conn, porto, flickr):
    run(database, flickr, FakeClock())
    instagram = albums.plan_album(conn, "Instagram")
    albums.add_extra(conn, [porto["IMG_0002.JPG"], porto["IMG_0003.JPG"]], instagram)
    report = run(database, flickr, FakeClock())
    assert (report.uploaded, report.added_to_albums, report.albums_created) == (0, 2, 1)
    assert len(flickr.photos) == 3
    assert photos_in(flickr, "Instagram") == ["IMG_0002", "IMG_0003"]


def test_existing_flickr_album_as_extra(database, conn, porto, flickr):
    flickr.add_album("Best of")
    albums.sync_with_flickr(conn, flickr)
    best = conn.execute("SELECT id FROM albums WHERE title = 'Best of'").fetchone()[0]
    albums.add_extra(conn, [porto["IMG_0003.JPG"]], best)
    run(database, flickr, FakeClock())
    assert "IMG_0003" in photos_in(flickr, "Best of")


def test_own_album_is_no_extra(conn, porto):
    own = conn.execute("SELECT id FROM albums WHERE title = 'Porto'").fetchone()[0]
    assert albums.add_extra(conn, [porto["IMG_0001.JPG"]], own) == 0


def test_removing_an_unused_extra_forgets_the_planned_album(conn, porto):
    instagram = albums.plan_album(conn, "Instagram")
    albums.add_extra(conn, [porto["IMG_0001.JPG"]], instagram)
    assert albums.remove_extra(conn, [porto["IMG_0001.JPG"]], instagram) == 1
    assert conn.execute("SELECT count(*) FROM albums WHERE title = 'Instagram'").fetchone()[0] == 0


def test_yellow_extra_album_does_not_hold_back_the_own_one(database, conn, porto, flickr):
    flickr.add_album("instagram")
    instagram = albums.plan_album(conn, "Instagram")  # only differs in case: yellow
    albums.add_extra(conn, [porto["IMG_0001.JPG"]], instagram)
    report = run(database, flickr, FakeClock())
    assert report.uploaded == 3
    assert photos_in(flickr, "Porto") == ["IMG_0001", "IMG_0002", "IMG_0003"]
    assert photos_in(flickr, "instagram") == ["old"]  # nothing added before the decision
    events = [r[0] for r in conn.execute("SELECT message FROM events WHERE level = 'warning'")]
    assert any("nothing is added to it yet" in m for m in events)


def test_planned_extra_merges_into_the_flickr_album(conn, porto, flickr):
    instagram = albums.plan_album(conn, "Instagram")
    albums.add_extra(conn, [porto["IMG_0001.JPG"]], instagram)
    flickr.add_album("Instagram")
    albums.sync_with_flickr(conn, flickr)
    target = conn.execute("SELECT id FROM albums WHERE title = 'Instagram'").fetchone()[0]
    assert albums.extra_albums(conn, porto["IMG_0001.JPG"]) == [target]


def test_unknown_album_or_file(conn, porto):
    with pytest.raises(db.RecordNotFoundError):
        albums.add_extra(conn, [porto["IMG_0001.JPG"]], 999)
    instagram = albums.plan_album(conn, "Instagram")
    with pytest.raises(db.RecordNotFoundError):
        albums.add_extra(conn, [999], instagram)


def test_in_the_window(database, conn, porto, flickr):
    from flickr_uploader.gui.api import Api

    api = Api(database, client_factory=lambda **kwargs: flickr, background=lambda work: work())
    folder_id = conn.execute("SELECT id FROM folders WHERE rel_path = 'Porto'").fetchone()[0]
    listing = api.folder_files(folder_id)
    assert listing["per_file"] is False
    assert [(f["name"], f["album"], f["extras"]) for f in listing["files"]][0] == (
        "IMG_0001.JPG",
        "Porto",
        [],
    )
    run(database, flickr, FakeClock())
    assert api.upload_plan()["adds"] == 0
    made = api.add_to_new_album([porto["IMG_0001.JPG"], porto["IMG_0002.JPG"]], "Instagram")
    assert made["added"] == 2
    assert api.upload_plan()["adds"] == 2
    files = api.folder_files(folder_id)["files"]
    assert [f["extras"] for f in files] == [["Instagram"], ["Instagram"], []]
    assert api.remove_from_album([porto["IMG_0002.JPG"]], made["album_id"]) == {"removed": 1}
    assert api.upload_plan()["adds"] == 1
    run(database, flickr, FakeClock())
    assert api.upload_plan()["adds"] == 0
    assert photos_in(flickr, "Instagram") == ["IMG_0001"]


def test_dry_run_shows_extra_albums(database, conn, porto, flickr):
    from flickr_uploader import dryrun

    instagram = albums.plan_album(conn, "Instagram")
    albums.add_extra(conn, [porto["IMG_0001.JPG"]], instagram)
    first = {f.name: f for a in dryrun.dry_run(conn).albums for f in a.files}
    assert first["IMG_0001.JPG"].detail == "auch in Instagram"
    run(database, flickr, FakeClock())
    albums.add_extra(conn, [porto["IMG_0002.JPG"]], instagram)
    later = {f.name: f for a in dryrun.dry_run(conn).albums for f in a.files}
    assert (later["IMG_0002.JPG"].action, later["IMG_0002.JPG"].detail) == (dryrun.ADD, "Instagram")
    assert later["IMG_0001.JPG"].action == dryrun.DONE
