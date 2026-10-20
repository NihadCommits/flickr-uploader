"""Content hashing of local files with XXH3-128.

The digest identifies a photo for its whole life: it is stored in the database
and in the machine tag on Flickr. Algorithm and output format must therefore
never change; tests/test_hashing.py pins both.
"""

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import xxhash

ALGORITHM = "xxh3_128"
CHUNK_SIZE = 1024 * 1024
_DIGEST_PATTERN = re.compile(r"[0-9a-f]{32}")


class HashingError(Exception):
    """Base class for errors raised by this module (besides plain OSError)."""


class FileChangedError(HashingError):
    """The file was modified while it was being read; the digest is unusable."""


class HashingCancelledError(HashingError):
    """The caller asked to stop, e.g. because the user aborted the run."""


class CancelFlag(Protocol):
    """Anything with is_set(), typically a threading.Event."""

    def is_set(self) -> bool: ...


@dataclass(frozen=True)
class FileHash:
    """A digest together with the file size and mtime it belongs to."""

    digest: str  # 32 lowercase hex characters
    size_bytes: int
    mtime_ns: int


def hash_file(
    path: str | Path,
    *,
    cancel: CancelFlag | None = None,
    chunk_size: int = CHUNK_SIZE,
) -> FileHash:
    """Hash the content of a file, reading it in chunks.

    Size and modification time are taken from the open file handle before
    reading and compared again afterwards. Store them together with the digest
    (not values from an earlier scan): only then does "size and mtime
    unchanged" reliably mean "digest still valid".

    The file is only ever opened for reading. Raises FileChangedError if it
    was modified meanwhile, HashingCancelledError if `cancel` is set, and lets
    OSError (missing file, no permission, read error) through.
    """
    hasher = xxhash.xxh3_128()
    buffer = bytearray(chunk_size)
    view = memoryview(buffer)
    total = 0
    # Unbuffered: the chunks go straight into our buffer, no second copy.
    with open(path, "rb", buffering=0) as file:
        before = os.fstat(file.fileno())
        while True:
            # Checked per chunk, so aborting a run doesn't wait for a 1 GB video.
            if cancel is not None and cancel.is_set():
                raise HashingCancelledError(f"hashing {path} was cancelled")
            count = file.readinto(buffer)
            if not count:
                break
            hasher.update(view[:count])
            total += count
        after = os.fstat(file.fileno())

    unchanged = (after.st_size, after.st_mtime_ns) == (before.st_size, before.st_mtime_ns)
    if not unchanged or total != before.st_size:
        raise FileChangedError(f"{path} changed while it was being hashed")
    return FileHash(hasher.hexdigest(), before.st_size, before.st_mtime_ns)


def is_valid_digest(value: str) -> bool:
    """Tell whether `value` has the format of a digest from hash_file().

    Meant for digests read back from outside, e.g. Flickr machine tags.
    """
    return _DIGEST_PATTERN.fullmatch(value) is not None
