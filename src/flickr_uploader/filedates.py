"""Write the recording date into downloaded files.

Flickr's converted videos come without any date, and every download carries
the time it was downloaded. This writes the date Flickr knows instead:

- into the MP4/MOV header (mvhd, tkhd, mdhd), only if the file has none: a
  few bytes change in place, the video itself is not re-encoded. Explorer
  shows it as "Medium erstellt"
- as the file's created and modified date

Meant for the tool's own downloads only, never for files in the photo archive.
"""

import os
import struct
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from flickr_uploader import config

FLICKR_TIME = "%Y-%m-%d %H:%M:%S"
QUICKTIME_EXTENSIONS = frozenset({".mp4", ".mov", ".m4v", ".3gp"})
MAX_MOOV_BYTES = 64 * 1024 * 1024  # the header box; far larger means something is off

# MP4/MOV keep their dates in these boxes, as seconds since 1904 (UTC).
_DATED_BOXES = frozenset({b"mvhd", b"tkhd", b"mdhd"})
_PARENT_BOXES = frozenset({b"trak", b"mdia"})
_QUICKTIME_EPOCH = datetime(1904, 1, 1, tzinfo=UTC)
_WINDOWS_EPOCH = datetime(1601, 1, 1, tzinfo=UTC)


def taken_time(taken: str | None) -> datetime | None:
    """Flickr's taken time ("YYYY-MM-DD HH:MM:SS") as UTC, None if not usable.

    Flickr keeps camera local time without a time zone. It is read as this PC's
    local time, so Explorer shows exactly the time Flickr shows.
    """
    if not taken:
        return None
    try:
        return datetime.strptime(taken, FLICKR_TIME).astimezone(UTC)
    except (ValueError, OSError, OverflowError):
        return None


def apply_date(path: str | os.PathLike, when: datetime) -> bool:
    """Give a downloaded file its recording date; True if it went into the header.

    The header is only filled in where the file has no date of its own; the
    file's created and modified dates are always set.
    """
    path = Path(path)
    filled = path.suffix.lower() in QUICKTIME_EXTENSIONS and fill_container_date(path, when)
    set_file_times(path, when)
    return filled


def fill_container_date(path: str | os.PathLike, when: datetime) -> bool:
    """Write `when` into the MP4/MOV header if the file has no date.

    A file whose movie header (mvhd) is dated stays completely untouched.
    Returns True if something was written.
    """
    seconds = int((when - _QUICKTIME_EPOCH).total_seconds())
    with open(config.filesystem_path(path), "r+b") as file:

        def read_file(offset: int, size: int) -> bytes:
            file.seek(offset)
            return file.read(size)

        file_size = os.fstat(file.fileno()).st_size
        moov = next(
            (
                (start, end)
                for kind, start, end in _boxes(read_file, 0, file_size)
                if kind == b"moov"
            ),
            None,
        )
        if moov is None or moov[1] - moov[0] > MAX_MOOV_BYTES:
            return False
        start, end = moov
        data = read_file(start, end - start)

        def read_moov(offset: int, size: int) -> bytes:
            return data[offset - start : offset - start + size]

        fields = list(_empty_date_fields(read_moov, start, end))
        if not any(kind == b"mvhd" for kind, _, _ in fields):
            return False  # the file has its own date (or no movie header)
        fields = [
            (offset, width)
            for _, offset, width in fields
            if seconds < 2 ** (8 * width)  # 32-bit fields end in 2040
        ]
        for offset, width in fields:
            file.seek(offset)
            file.write(seconds.to_bytes(width, "big"))
    return bool(fields)


def set_file_times(path: str | os.PathLike, when: datetime) -> None:
    """Set the file's modified and (on Windows) created date."""
    timestamp = when.timestamp()
    os.utime(config.filesystem_path(path), (timestamp, timestamp))
    if sys.platform == "win32":
        _set_creation_time(path, when)


def _boxes(read, start: int, end: int):
    """(type, payload start, end) of each box in [start, end) of an MP4/MOV file."""
    offset = start
    while offset + 8 <= end:
        header = read(offset, 16)
        size, kind = struct.unpack(">I4s", header[:8])
        payload = offset + 8
        if size == 1 and len(header) == 16:  # 64-bit size follows
            size, payload = struct.unpack(">Q", header[8:])[0], offset + 16
        elif size == 0:  # box runs to the end
            size = end - offset
        if size < payload - offset or offset + size > end:
            return  # damaged or unknown layout: leave the rest alone
        yield kind, payload, offset + size
        offset += size


def _empty_date_fields(read, start: int, end: int):
    """(box type, offset, width) of unset creation/modification times in moov."""
    for kind, payload, box_end in _boxes(read, start, end):
        if kind in _PARENT_BOXES:
            yield from _empty_date_fields(read, payload, box_end)
            continue
        version = read(payload, 1)
        if kind not in _DATED_BOXES or version not in (b"\x00", b"\x01"):
            continue
        width = 8 if version == b"\x01" else 4
        created = payload + 4  # after version and flags
        modified = created + width
        if modified + width > box_end or any(read(created, width)):
            continue  # already dated: keep it
        yield kind, created, width
        if not any(read(modified, width)):
            yield kind, modified, width


def _set_creation_time(path: str | os.PathLike, when: datetime) -> None:
    import ctypes
    from ctypes import wintypes

    file_write_attributes, share_all, open_existing = 0x100, 0x7, 3
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.CreateFileW.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    filetime_pointer = ctypes.POINTER(wintypes.FILETIME)
    kernel32.SetFileTime.argtypes = (wintypes.HANDLE, *(filetime_pointer,) * 3)
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)

    handle = kernel32.CreateFileW(
        config.filesystem_path(path), file_write_attributes, share_all, None, open_existing, 0, None
    )
    if handle in (None, ctypes.c_void_p(-1).value):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        ticks = (when - _WINDOWS_EPOCH) // timedelta(microseconds=1) * 10  # 100 ns steps
        created = wintypes.FILETIME(ticks & 0xFFFFFFFF, ticks >> 32)
        if not kernel32.SetFileTime(handle, ctypes.byref(created), None, None):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        kernel32.CloseHandle(handle)
