"""Tests for ffmpeg on videos (flickr_uploader.videotools), with generated test videos."""

import subprocess
import threading

import pytest

from flickr_uploader import videotools


def make_video(path, seconds=6, size="320x240", audio=True):
    """A small test video (moving test picture, a tone), made by ffmpeg itself."""
    command = [videotools.ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-y"]
    command += ["-f", "lavfi", "-i", f"testsrc=duration={seconds}:size={size}:rate=25"]
    if audio:
        command += ["-f", "lavfi", "-i", f"sine=duration={seconds}"]
        command += ["-c:a", "aac"]
    command += ["-c:v", "libx264", "-g", "25", "-pix_fmt", "yuv420p"]
    command += ["-metadata", "creation_time=2024-07-07T14:03:22Z", str(path)]
    subprocess.run(command, check=True, creationflags=videotools._NO_WINDOW)
    return path


@pytest.fixture(scope="module")
def video(tmp_path_factory):
    return make_video(tmp_path_factory.mktemp("videos") / "MVI_0001.mp4")


def test_probe(video):
    info = videotools.probe(video)
    assert info.duration == pytest.approx(6, abs=0.1)
    assert (info.width, info.height, info.fps, info.has_audio) == (320, 240, 25, True)


def test_probe_without_sound(tmp_path):
    info = videotools.probe(make_video(tmp_path / "silent.mp4", seconds=2, audio=False))
    assert info.has_audio is False


def test_probe_refuses_what_is_no_video(tmp_path):
    path = tmp_path / "broken.mp4"
    path.write_bytes(b"not a video")
    with pytest.raises(videotools.VideoToolError):
        videotools.probe(path)


@pytest.mark.parametrize(
    "size, duration, parts",
    [
        (500, 60, 1),
        (2500, 60, 4),  # by size: 800 per part
        (500, 1500, 3),  # by length: 600 s per part
        (2500, 1500, 4),
    ],
)
def test_split_count(size, duration, parts):
    assert videotools.split_count(size, duration, 800, 600) == parts


def test_cut_copies_pieces_without_re_encoding(video, tmp_path):
    first, second = tmp_path / "part1.mp4", tmp_path / "part2.mp4"
    videotools.cut(video, first, 0, 3)
    videotools.cut(video, second, 3, None)
    lengths = [videotools.probe(part).duration for part in (first, second)]
    assert sum(lengths) == pytest.approx(6, abs=1.1)  # cut at key frames
    assert all(length > 2 for length in lengths)
    assert not list(tmp_path.glob("*.part*"))
    tags = subprocess.run(
        [videotools.ffmpeg_exe(), "-hide_banner", "-i", str(first)],
        capture_output=True,
        text=True,
        check=False,
        creationflags=videotools._NO_WINDOW,
    ).stderr
    assert "creation_time   : 2024-07-07T14:03:22" in tags  # the recording date goes along


def test_compress_hits_the_target_size(tmp_path):
    source = make_video(tmp_path / "big.mp4", seconds=8, size="640x480")
    target = tmp_path / "small.mp4"
    info = videotools.probe(source)
    videotools.compress(source, target, info, 400_000)
    assert target.stat().st_size < 400_000 * 1.3
    assert videotools.probe(target).duration == pytest.approx(8, abs=0.2)


def test_compress_refuses_a_hopeless_target(video, tmp_path):
    with pytest.raises(videotools.VideoToolError, match="split"):
        videotools.compress(video, tmp_path / "x.mp4", videotools.probe(video), 10_000)


def test_pausing_stops_ffmpeg_and_leaves_nothing(tmp_path):
    source = make_video(tmp_path / "long.mp4", seconds=30, size="1280x720")
    cancel = threading.Event()
    threading.Timer(0.3, cancel.set).start()
    target = tmp_path / "out.mp4"
    with pytest.raises(videotools.VideoCancelledError):
        videotools.compress(source, target, videotools.probe(source), 2_000_000, cancel=cancel)
    assert not target.exists()
    assert not list(tmp_path.glob("*.part*"))


def test_failure_is_reported_and_leaves_nothing(tmp_path):
    source = tmp_path / "broken.mp4"
    source.write_bytes(b"not a video")
    with pytest.raises(videotools.VideoToolError, match="ffmpeg failed"):
        videotools.cut(source, tmp_path / "out.mp4", 0, 1)
    assert list(tmp_path.iterdir()) == [source]
