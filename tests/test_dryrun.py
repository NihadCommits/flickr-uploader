"""Tests for the dry run (flickr_uploader.dryrun): it predicts and changes nothing."""

from fake_flickr import FakeFlickr
from flickr_uploader import albums, dryrun, precheck, settings, videoplans
from flickr_uploader.flickr_client import FoundPhoto
from test_uploader import FakeClock, run, tick, trip  # noqa: F401 - fixtures
from test_videos import big_video, holiday, limits, video_file_id  # noqa: F401 - fixtures


def snapshot(conn):
    return {
        table: conn.execute(f"SELECT * FROM {table}").fetchall()  # noqa: S608 - fixed names
        for table in ("photos", "files", "albums", "album_photos", "video_plans", "events")
    }


def actions(result):
    return {f.name: f.action for album in result.albums for f in album.files} | {
        f.name: f.action for f in result.outside
    }


def test_dry_run_predicts_the_run_and_changes_nothing(database, conn, trip):  # noqa: F811
    flickr, clock = FakeFlickr(), FakeClock()
    before = snapshot(conn)
    result = dryrun.dry_run(conn)
    assert snapshot(conn) == before
    assert flickr.photos == {}
    (album,) = result.albums
    assert (album.title, album.state) == ("2026-06 Portugal", "create")
    assert actions(result) == {
        "IMG_0001.JPG": dryrun.CHECK,
        "IMG_0002.JPG": dryrun.CHECK,
        "IMG_0003.JPG": dryrun.CHECK,
        "MVI_0004.MP4": dryrun.CHECK,
        "IMG_0005.CR3": dryrun.SKIPPED,
        "IMG_0006.HEIC": dryrun.SKIPPED,
    }
    assert (result.totals["uploads"], result.totals["new_albums"]) == (4, 1)

    report = run(database, flickr, clock)
    assert report.uploaded == result.totals["uploads"]
    after = dryrun.dry_run(conn)
    assert after.albums[0].state == "exists"
    assert after.totals["uploads"] == 0
    assert after.totals[dryrun.DONE] == 4


def test_dry_run_shows_what_waits(database, conn, trip):  # noqa: F811
    flickr = FakeFlickr()
    old = flickr.add_photo("IMG_0001")
    flickr.old_photos.append(FoundPhoto(old, "IMG_0001", "2026-06-01 10:00:00", 32, 24))
    folder_id = conn.execute(
        "SELECT id FROM folders WHERE rel_path = '2026-06 Portugal'"
    ).fetchone()[0]
    precheck.precheck_folder(conn, flickr, folder_id)
    result = actions(dryrun.dry_run(conn))
    assert result["IMG_0001.JPG"] == dryrun.WAIT_MATCH
    assert result["IMG_0002.JPG"] == dryrun.UPLOAD

    file_id = conn.execute("SELECT id FROM files WHERE name = 'IMG_0001.JPG'").fetchone()[0]
    precheck.choose_existing(conn, file_id, old)
    assert actions(dryrun.dry_run(conn))["IMG_0001.JPG"] == dryrun.TAKE_OVER


def test_dry_run_with_a_yellow_album(conn, trip):  # noqa: F811
    flickr = FakeFlickr()
    flickr.add_album("2026-06 portugal")
    albums.sync_with_flickr(conn, flickr)
    result = dryrun.dry_run(conn)
    assert result.albums[0].state == "waits"
    assert set(actions(result).values()) == {dryrun.WAIT_ALBUM, dryrun.SKIPPED}
    assert result.totals["uploads"] == 0


def test_dry_run_with_videos(database, conn, holiday):  # noqa: F811
    assert actions(dryrun.dry_run(conn)) == {"MVI_0001.MP4": dryrun.WAIT_VIDEO}
    file_id = video_file_id(conn)
    conn.execute("UPDATE files SET duration_seconds = 8")
    videoplans.decide(conn, file_id, videoplans.SPLIT)
    (planned,) = dryrun.dry_run(conn).albums[0].files
    assert (planned.action, planned.detail, planned.uploads) == (dryrun.SPLIT, "3 Teile", 3)
    settings.put(conn, settings.VIDEO_TOO_LARGE, "compress")
    videoplans.decide(conn, file_id, None)
    (planned,) = dryrun.dry_run(conn).albums[0].files
    assert (planned.action, planned.uploads) == (dryrun.COMPRESS, 1)


def test_ticked_folder_outside_album_folders(conn, tmp_path):
    from flickr_uploader import scanner

    folder = tmp_path / "Bilder" / "Lose"
    folder.mkdir(parents=True)
    (folder / "x.jpg").write_bytes(b"x")
    scanner.scan_root(conn, scanner.add_root(conn, tmp_path / "Bilder"))
    tick(conn, "Lose")
    conn.execute("UPDATE folders SET album_id = NULL")
    result = dryrun.dry_run(conn)
    assert [f.action for f in result.outside] == [dryrun.NO_ALBUM]
