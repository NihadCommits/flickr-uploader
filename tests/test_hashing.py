"""Tests for content hashing (flickr_uploader.hashing)."""

import os
import threading
from types import SimpleNamespace

import pytest
import xxhash

from flickr_uploader import hashing

# Official XXH3-128 test vector: empty input, seed 0.
EMPTY_DIGEST = "99aa06d3014798d86001c324468d497f"
# Pins the digest format for good: 1 MiB + 256 bytes, so it spans two default
# chunks. If this ever changes, every stored hash and Flickr tag is wrong.
GOLDEN_CONTENT = bytes(range(256)) * 4097
GOLDEN_DIGEST = "3898db5d5b4a87468b544a01c6d30e1c"

OLD_MTIME_NS = 1_600_000_000 * 10**9  # 2020-09-13, far from "now"


def write(tmp_path, content, name="IMG_0001.JPG"):
    path = tmp_path / name
    path.write_bytes(content)
    # A fixed old mtime: a change during hashing then always gets a different
    # one, no matter how coarse the clock is.
    os.utime(path, ns=(OLD_MTIME_NS, OLD_MTIME_NS))
    return path


class ChangeFileOnce:
    """Cancel flag stand-in that modifies the file right before chunk `when`."""

    def __init__(self, change, when=2):
        self.change = change
        self.when = when
        self.calls = 0

    def is_set(self):
        self.calls += 1
        if self.calls == self.when:
            self.change()
        return False


# --- The digest itself --------------------------------------------------------


def test_empty_file_matches_official_test_vector(tmp_path):
    assert hashing.hash_file(write(tmp_path, b"")).digest == EMPTY_DIGEST


def test_digest_is_pinned(tmp_path):
    assert hashing.hash_file(write(tmp_path, GOLDEN_CONTENT)).digest == GOLDEN_DIGEST


@pytest.mark.parametrize("size", [0, 1, 63, 64, 65, 3 * 64 + 17])
def test_chunked_reading_equals_hashing_at_once(tmp_path, size):
    content = os.urandom(size)
    result = hashing.hash_file(write(tmp_path, content), chunk_size=64)
    assert result.digest == xxhash.xxh3_128(content).hexdigest()


def test_digest_format(tmp_path):
    digest = hashing.hash_file(write(tmp_path, b"photo")).digest
    assert hashing.is_valid_digest(digest)


# --- Size, mtime and the read-only rule ---------------------------------------


def test_reports_size_and_mtime_of_the_hashed_content(tmp_path):
    result = hashing.hash_file(write(tmp_path, GOLDEN_CONTENT))
    assert result.size_bytes == len(GOLDEN_CONTENT)
    assert result.mtime_ns == OLD_MTIME_NS


def test_file_is_left_untouched(tmp_path):
    path = write(tmp_path, GOLDEN_CONTENT)
    hashing.hash_file(path)
    assert path.read_bytes() == GOLDEN_CONTENT
    assert path.stat().st_mtime_ns == OLD_MTIME_NS


# --- Files that change while being read ---------------------------------------


def test_growing_file_is_detected(tmp_path):
    path = write(tmp_path, b"x" * 1000)

    def append():
        with open(path, "ab") as file:
            file.write(b"more")

    with pytest.raises(hashing.FileChangedError):
        hashing.hash_file(path, chunk_size=100, cancel=ChangeFileOnce(append))


def test_shrinking_file_is_detected(tmp_path):
    path = write(tmp_path, b"x" * 1000)

    def truncate():
        with open(path, "r+b") as file:
            file.truncate(500)

    with pytest.raises(hashing.FileChangedError):
        hashing.hash_file(path, chunk_size=100, cancel=ChangeFileOnce(truncate))


def test_same_size_rewrite_is_detected(tmp_path):
    path = write(tmp_path, b"x" * 1000)

    def rewrite():
        with open(path, "r+b") as file:
            file.write(b"y" * 1000)

    with pytest.raises(hashing.FileChangedError):
        hashing.hash_file(path, chunk_size=100, cancel=ChangeFileOnce(rewrite))


def test_size_that_does_not_match_the_content_is_detected(tmp_path, monkeypatch):
    # The file system claims more bytes than can actually be read.
    path = write(tmp_path, b"x" * 1000)
    claimed = SimpleNamespace(st_size=2000, st_mtime_ns=OLD_MTIME_NS)
    monkeypatch.setattr(hashing.os, "fstat", lambda fd: claimed)
    with pytest.raises(hashing.FileChangedError):
        hashing.hash_file(path)


# --- Cancelling and errors ----------------------------------------------------


def test_cancel_before_start(tmp_path):
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(hashing.HashingCancelledError):
        hashing.hash_file(write(tmp_path, b"x" * 1000), cancel=cancel)


def test_cancel_in_the_middle_stops_reading(tmp_path):
    class CancelAtThirdChunk:
        calls = 0

        def is_set(self):
            self.calls += 1
            return self.calls == 3

    cancel = CancelAtThirdChunk()
    with pytest.raises(hashing.HashingCancelledError):
        hashing.hash_file(write(tmp_path, b"x" * 1000), chunk_size=100, cancel=cancel)
    assert cancel.calls == 3  # did not read the remaining 7 chunks


def test_missing_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        hashing.hash_file(tmp_path / "gone.jpg")


@pytest.mark.parametrize(
    ("value", "valid"),
    [
        (GOLDEN_DIGEST, True),
        (GOLDEN_DIGEST.upper(), False),
        (GOLDEN_DIGEST[:-1], False),
        (GOLDEN_DIGEST + "0", False),
        ("g" * 32, False),
        ("", False),
        (f"{GOLDEN_DIGEST}\n", False),
    ],
)
def test_is_valid_digest(value, valid):
    assert hashing.is_valid_digest(value) is valid
