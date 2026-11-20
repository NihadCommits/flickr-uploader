"""Write the recording date into downloaded files.

Flickr's converted videos come without any date, and every download carries
the time it was downloaded. This writes the date Flickr knows instead:

- into the MP4/MOV header (mvhd, tkhd, mdhd), only if the file has none: a
  few bytes change in place, the video itself is not re-encoded. Explorer
  shows it as "Medium erstellt"
- into JPEGs without a capture date (Flickr's sizes below the original): an
  EXIF block with DateTimeOriginal is put in front of the image data, which
  stays byte for byte the same. Explorer shows it as "Aufnahmedatum"
- as the file's created and modified date

Meant for the tool's own downloads only, never for files in the photo archive.
"""

import io
import os
import struct
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from PIL import ExifTags, Image

from flickr_uploader import config

FLICKR_TIME = "%Y-%m-%d %H:%M:%S"
QUICKTIME_EXTENSIONS = frozenset({".mp4", ".mov", ".m4v", ".3gp"})
JPEG_EXTENSIONS = frozenset({".jpg", ".jpeg"})
EXIF_TIME = "%Y:%m:%d %H:%M:%S"
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
    suffix = path.suffix.lower()
    filled = (suffix in QUICKTIME_EXTENSIONS and fill_container_date(path, when)) or (
        suffix in JPEG_EXTENSIONS and fill_jpeg_date(path, when)
    )
    set_file_times(path, when)
    return filled


def fill_jpeg_date(path: str | os.PathLike, when: datetime) -> bool:
    """Put `when` into a JPEG's EXIF if it has no capture date; True if written.

    Only the EXIF block (APP1) is replaced or added, the compressed image data
    is copied unchanged. Written to a new file first, then swapped in.
    """
    target = config.filesystem_path(path)
    with open(target, "rb") as file:
        data = file.read()
    if data[:2] != b"\xff\xd8":
        return False
    try:
        with Image.open(io.BytesIO(data)) as image:
            exif = image.getexif()
            details = exif.get_ifd(ExifTags.IFD.Exif)  # read while the image is open
    except (OSError, SyntaxError, ValueError):  # not a readable JPEG: leave it alone
        return False
    if details.get(ExifTags.Base.DateTimeOriginal):
        return False  # the camera's own date wins
    # Flickr's time is camera local time; `when` is that time read as local.
    stamp = when.astimezone().strftime(EXIF_TIME)
    details[ExifTags.Base.DateTimeOriginal] = stamp
    details[ExifTags.Base.DateTimeDigitized] = stamp
    exif[ExifTags.Base.DateTime] = stamp
    block = exif.tobytes()
    if len(block) + 2 > 0xFFFF:
        return False
    rest = _without_exif(data)
    if rest is None:
        return False
    partial = Path(str(target) + ".part")
    with open(partial, "wb") as file:
        file.write(b"\xff\xd8\xff\xe1" + struct.pack(">H", len(block) + 2) + block + rest)
    os.replace(partial, target)
    return True


def _without_exif(data: bytes) -> bytes | None:
    """The JPEG after its start marker, minus any EXIF block; None if unreadable."""
    position, kept = 2, []
    while position + 4 <= len(data) and data[position] == 0xFF:
        marker = data[position + 1]
        if marker == 0xDA:  # start of the image data: the rest stays as it is
            break
        (length,) = struct.unpack(">H", data[position + 2 : position + 4])
        segment = data[position : position + 2 + length]
        if not (marker == 0xE1 and segment[4:10] == b"Exif\x00\x00"):
            kept.append(segment)
        position += 2 + length
    else:
        return None
    return b"".join(kept) + data[position:]


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
