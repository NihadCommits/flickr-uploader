"""Tests for writing recording dates into downloaded files (flickr_uploader.filedates)."""

import os
from datetime import UTC, datetime

import pytest

from flickr_uploader import filedates
from mp4 import media_dates, movie_dates, quicktime

TAKEN = "2024-07-07 14:03:22"
QUICKTIME_EPOCH = datetime(1904, 1, 1, tzinfo=UTC)


def seconds(when):
    return int((when - QUICKTIME_EPOCH).total_seconds())


def assert_file_times(path, when):
    info = os.stat(path)
    assert abs(info.st_mtime - when.timestamp()) < 1
    if hasattr(info, "st_birthtime"):  # Windows: the created date is set too
        assert abs(info.st_birthtime - when.timestamp()) < 1


def test_taken_time_is_read_as_local_time():
    when = filedates.taken_time(TAKEN)
    assert when.tzinfo is UTC
    # Explorer shows local time, so it shows exactly what Flickr shows.
    assert when.astimezone().replace(tzinfo=None) == datetime(2024, 7, 7, 14, 3, 22)


@pytest.mark.parametrize("value", [None, "", "yesterday", "0000-00-00 00:00:00"])
def test_unusable_taken_time(value):
    assert filedates.taken_time(value) is None


def test_video_without_date_gets_it_in_its_header(tmp_path):
    data = quicktime()
    path = tmp_path / "MVI_0001.mp4"
    path.write_bytes(data)
    when = filedates.taken_time(TAKEN)

    assert filedates.apply_date(path, when) is True
    after = path.read_bytes()
    assert movie_dates(after) == (seconds(when), seconds(when))
    assert media_dates(after) == (seconds(when), seconds(when))
    # Only header bytes changed: same size, the media data before moov untouched.
    assert len(after) == len(data)
    assert after[: data.index(b"moov")] == data[: data.index(b"moov")]
    assert_file_times(path, when)

    # A second time there is nothing left to fill in.
    assert filedates.apply_date(path, when) is False
    assert path.read_bytes() == after


def test_a_date_of_its_own_is_never_overwritten(tmp_path):
    data = quicktime(created=3_000_000_000)  # an original with its camera date
    path = tmp_path / "MVI_0001.mov"
    path.write_bytes(data)
    when = filedates.taken_time(TAKEN)
    assert filedates.apply_date(path, when) is False
    assert path.read_bytes() == data
    assert_file_times(path, when)


@pytest.mark.parametrize(
    "name, data",
    [("clip.avi", b"RIFF" + bytes(100)), ("broken.mp4", b"\x00\x00\x00\x02xxxx" + bytes(50))],
)
def test_other_or_damaged_files_only_get_the_file_date(tmp_path, name, data):
    path = tmp_path / name
    path.write_bytes(data)
    when = filedates.taken_time(TAKEN)
    assert filedates.apply_date(path, when) is False
    assert path.read_bytes() == data
    assert_file_times(path, when)
