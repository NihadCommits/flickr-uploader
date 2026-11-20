"""Tests for videos too large or too long for Flickr: compressed or split on upload.

Uses real ffmpeg on small generated videos; Flickr's limits are scaled down
to match (a few MB instead of 1 GB, seconds instead of 10 minutes).
"""

import subprocess

import pytest

from fake_flickr import FakeFlickr
from flickr_uploader import albums, config, db, scanner, settings, uploader, videoplans, videotools
from test_uploader import FakeClock, messages, run, tick

MB = config.MB


def make_video(path, seconds=8, bitrate="4M"):
    """A video of about `seconds` * `bitrate`, with sound and a recording date."""
    path.parent.mkdir(parents=True, exist_ok=True)
    command = [videotools.ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-y"]
    command += ["-f", "lavfi", "-i", f"testsrc2=duration={seconds}:size=640x480:rate=25"]
    command += ["-f", "lavfi", "-i", f"sine=duration={seconds}", "-c:a", "aac"]
    command += ["-c:v", "libx264", "-b:v", bitrate, "-g", "25", "-pix_fmt", "yuv420p"]
    command += ["-metadata", "creation_time=2024-07-07T14:03:22Z", str(path)]
    subprocess.run(command, check=True, creationflags=videotools._NO_WINDOW)
    return path


@pytest.fixture(scope="module")
def big_video(tmp_path_factory):
    return make_video(tmp_path_factory.mktemp("source") / "MVI_0001.MP4")


@pytest.fixture
def limits(monkeypatch, big_video):
    """Flickr's limits shrunk so the test video is 'too large' (and 'long' over 3 s)."""
    size = big_video.stat().st_size
    monkeypatch.setattr(config, "MAX_VIDEO_BYTES", int(size * 0.75))
    monkeypatch.setattr(config, "VIDEO_COMPRESS_BYTES", int(size * 0.3))
    monkeypatch.setattr(config, "VIDEO_PART_BYTES", int(size * 0.4))  # 3 pieces by size
    monkeypatch.setattr(config, "LONG_VIDEO_SECONDS", 3)
    return size


@pytest.fixture
def flickr():
    return FakeFlickr()


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def work(tmp_path):
    return tmp_path / "work"


@pytest.fixture
def holiday(conn, tmp_path, big_video, limits):
    """A ticked album folder with the test video in it."""
    folder = tmp_path / "Bilder" / "2026-06 Portugal"
    folder.mkdir(parents=True)
    (folder / big_video.name).write_bytes(big_video.read_bytes())
    scanner.scan_root(conn, scanner.add_root(conn, tmp_path / "Bilder"))
    tick(conn, "2026-06 Portugal")
    return folder


def video_file_id(conn):
    return conn.execute("SELECT id FROM files WHERE name = 'MVI_0001.MP4'").fetchone()[0]


def upload(database, flickr, clock, work):
    return run(database, flickr, clock, work_dir=work)


def titles(flickr):
    return sorted(photo.title for photo in flickr.photos.values())


def test_too_large_video_waits_for_a_decision(database, conn, holiday, flickr, clock, work):
    assert conn.execute("SELECT status FROM files").fetchone()[0] == "skipped_too_large"
    report = upload(database, flickr, clock, work)
    assert (report.uploaded, report.waiting, report.skipped) == (0, 1, 0)
    assert any("choose to compress or to split" in m for m in messages(conn))


def test_split_video_goes_up_in_pieces(database, conn, holiday, flickr, clock, work):
    videoplans.decide(conn, video_file_id(conn), videoplans.SPLIT)
    report = upload(database, flickr, clock, work)
    assert report.uploaded == 3
    assert titles(flickr) == [f"MVI_0001 (Teil {n} von 3)" for n in (1, 2, 3)]
    (album,) = flickr.sets.values()
    assert len(album.photos) == 3
    assert list(work.iterdir()) == []  # pieces are deleted after the upload
    states = conn.execute(
        "SELECT p.state FROM video_parts v JOIN photos p ON p.hash = v.photo_hash"
    )
    assert [row[0] for row in states] == ["verified"] * 3
    # The original was only read.
    assert (holiday / "MVI_0001.MP4").stat().st_size > config.MAX_VIDEO_BYTES

    again = upload(database, flickr, clock, work)
    assert (again.uploaded, again.already_done) == (0, 3)


def test_compressed_video_goes_up_once(database, conn, holiday, flickr, clock, work):
    videoplans.decide(conn, video_file_id(conn), videoplans.COMPRESS)
    report = upload(database, flickr, clock, work)
    assert report.uploaded == 1
    assert titles(flickr) == ["MVI_0001"]
    assert list(work.iterdir()) == []
    assert upload(database, flickr, clock, work).uploaded == 0


def test_default_from_the_settings(database, conn, holiday, flickr, clock, work):
    settings.put(conn, settings.VIDEO_TOO_LARGE, "split")
    assert upload(database, flickr, clock, work).uploaded == 3


def test_pieces_have_fixed_digests(database, conn, holiday, flickr, clock, work):
    """The same plan gives the same hash tags, so an unclear piece is found again."""
    videoplans.decide(conn, video_file_id(conn), videoplans.SPLIT)
    upload(database, flickr, clock, work)
    tags = sorted(digest for photo in flickr.photos.values() for digest in photo.digests)
    (source,) = conn.execute("SELECT source_hash FROM video_plans").fetchone()
    expected = sorted(videoplans.part_digest(source, "split", n, 3) for n in (1, 2, 3))
    assert tags == expected


@pytest.fixture
def long_video(conn, tmp_path, monkeypatch):
    """A ticked folder with a video under the size limit but 'long' (over 3 s)."""
    monkeypatch.setattr(config, "LONG_VIDEO_SECONDS", 3)
    make_video(tmp_path / "Bilder" / "Lang" / "MVI_0002.MP4", seconds=8, bitrate="500k")
    scanner.scan_root(conn, scanner.add_root(conn, tmp_path / "Bilder"))
    tick(conn, "Lang")
    return conn.execute("SELECT id FROM files").fetchone()[0]


def test_long_video_goes_up_whole_by_default(database, conn, long_video, flickr, clock, work):
    upload(database, flickr, clock, work)
    assert titles(flickr) == ["MVI_0002"]
    duration = conn.execute("SELECT duration_seconds FROM files").fetchone()[0]
    assert duration == pytest.approx(8, abs=0.2)


def test_long_video_split_by_length(database, conn, long_video, flickr, clock, work):
    upload_nothing_yet = conn.execute("SELECT count(*) FROM photos").fetchone()[0]
    assert upload_nothing_yet == 0
    conn.execute("UPDATE files SET duration_seconds = 8")  # read by the run otherwise
    videoplans.decide(conn, long_video, videoplans.SPLIT)
    upload(database, flickr, clock, work)
    assert titles(flickr) == [f"MVI_0002 (Teil {n} von 3)" for n in (1, 2, 3)]  # under 3 s each


def test_long_video_split_by_the_settings(database, conn, long_video, flickr, clock, work):
    settings.put(conn, settings.VIDEO_TOO_LONG, "split")
    upload(database, flickr, clock, work)
    assert len(flickr.photos) == 3


def test_choices_that_make_no_sense_are_refused(conn, holiday):
    with pytest.raises(videoplans.VideoPlanError):
        videoplans.decide(conn, video_file_id(conn), videoplans.ORIGINAL)  # too large for that


def test_the_choice_is_fixed_once_a_piece_is_up(database, conn, holiday, flickr, clock, work):
    file_id = video_file_id(conn)
    videoplans.decide(conn, file_id, videoplans.SPLIT)
    upload(database, flickr, clock, work)
    with pytest.raises(videoplans.VideoPlanError):
        videoplans.decide(conn, file_id, videoplans.COMPRESS)


def test_a_video_ffmpeg_cannot_read_fails_loudly(
    database, conn, tmp_path, flickr, clock, work, limits
):
    folder = tmp_path / "Bilder" / "Kaputt"
    folder.mkdir(parents=True)
    (folder / "MVI_0003.MP4").write_bytes(b"x" * (config.MAX_VIDEO_BYTES + 1))
    scanner.scan_root(conn, scanner.add_root(conn, tmp_path / "Bilder"))
    tick(conn, "Kaputt")
    settings.put(conn, settings.VIDEO_TOO_LARGE, "compress")
    report = upload(database, flickr, clock, work)
    assert (report.uploaded, report.failed) == (0, 1)
    assert any("Video piece not made" in m for m in messages(conn, "error"))
    assert list(work.iterdir()) == []


def test_changed_video_gets_a_new_plan(database, conn, holiday, flickr, clock, work, big_video):
    file_id = video_file_id(conn)
    videoplans.decide(conn, file_id, videoplans.SPLIT)
    with open(holiday / "MVI_0001.MP4", "ab") as file:
        file.write(b"\0")  # new content
    scanner.scan_root(conn, conn.execute("SELECT id FROM roots").fetchone()[0])
    assert conn.execute("SELECT count(*) FROM video_plans").fetchone()[0] == 0


def test_yellow_album_holds_the_video_back(database, conn, holiday, flickr, clock, work):
    flickr.add_album("2026-06 portugal")
    videoplans.decide(conn, video_file_id(conn), videoplans.SPLIT)
    report = upload(database, flickr, clock, work)
    assert report.uploaded == 0
    assert albums.lights(conn)  # still waits for the album decision


def test_pausing_while_ffmpeg_works_loses_nothing(database, conn, holiday, flickr, clock, work):
    videoplans.decide(conn, video_file_id(conn), videoplans.COMPRESS)

    class Stop:
        calls = 0

        def is_set(self):
            # Set once compressing has begun (after the checks before it).
            Stop.calls += 1
            return any("Compressing video" in m for m in messages(conn))

    report = uploader.run_upload(
        database, flickr, cancel=Stop(), now=clock.now, sleep=clock.sleep, work_dir=work
    )
    assert report.status == "aborted"
    assert flickr.photos == {}
    assert list(work.iterdir()) == []
    states = conn.execute(
        "SELECT p.state FROM video_parts v JOIN photos p ON p.hash = v.photo_hash"
    )
    assert [row[0] for row in states] == ["pending"]
    db.recover_interrupted(conn)
    assert upload(database, flickr, clock, work).uploaded == 1  # resumed


# --- In the window ------------------------------------------------------------------


def test_choosing_in_the_window(database, conn, holiday, flickr, clock, work):
    from flickr_uploader.gui.api import Api

    api = Api(database, client_factory=lambda **kwargs: flickr, background=lambda work: work())
    folder_id = conn.execute(
        "SELECT id FROM folders WHERE rel_path = '2026-06 Portugal'"
    ).fetchone()[0]
    listing = api.videos(folder_id)
    (video,) = listing["videos"]
    assert listing["open"] == 1
    assert (video["name"], video["too_large"], video["choice"]) == ("MVI_0001.MP4", True, None)
    assert video["allowed"] == ["compress", "split"]
    assert api.upload_plan()["video"] == 1
    assert api.overview()["open_videos"] == 1

    assert api.choose_video(video["file_id"], "split") == {}
    (video,) = api.videos(folder_id)["videos"]
    assert (video["choice"], video["own_choice"], video["locked"]) == ("split", True, False)
    plan = api.upload_plan()
    assert (plan["video"], plan["ready"]) == (0, 1)

    upload(database, flickr, clock, work)
    (video,) = api.videos(folder_id)["videos"]
    assert (video["locked"], video["parts"]) == (True, 3)
    assert api.upload_plan()["done"] == 1
    assert "schon auf Flickr" in api.choose_video(video["file_id"], "compress")["error"]


def test_back_to_the_default(database, conn, holiday):
    from flickr_uploader.gui.api import Api

    api = Api(database, client_factory=lambda **kwargs: None)
    file_id = video_file_id(conn)
    api.choose_video(file_id, "compress")
    assert api.choose_video(file_id, None) == {}
    assert conn.execute("SELECT count(*) FROM video_plans").fetchone()[0] == 0
    assert "passt nicht" in api.choose_video(file_id, "original")["error"]
