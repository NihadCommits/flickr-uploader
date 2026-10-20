"""Synthetic MP4/MOV files for tests: the box structure of one, no real video."""

import os
import struct


def box(kind: bytes, payload: bytes) -> bytes:
    return struct.pack(">I4s", 8 + len(payload), kind) + payload


def _dated(kind: bytes, version: int, created: int, modified: int) -> bytes:
    times = struct.pack(">II" if version == 0 else ">QQ", created, modified)
    return box(kind, bytes([version, 0, 0, 0]) + times + b"\x00" * 80)


def quicktime(created: int = 0) -> bytes:
    """ftyp, mdat with a 64-bit size, moov at the end (as many cameras write it).

    created=0 means no date, like Flickr's converted versions.
    """
    video = os.urandom(3000)
    mdat = struct.pack(">I4sQ", 1, b"mdat", 16 + len(video)) + video
    track = _dated(b"tkhd", 0, 0, 0) + box(
        b"mdia", _dated(b"mdhd", 1, 0, 0) + box(b"hdlr", b"x" * 20)
    )
    moov = box(
        b"moov", _dated(b"mvhd", 0, created, created) + box(b"trak", track) + box(b"udta", b"y")
    )
    return box(b"ftyp", b"isom\x00\x00\x02\x00isomiso2mp41") + mdat + moov


def movie_dates(data: bytes) -> tuple[int, int]:
    """(created, modified) from the movie header (mvhd)."""
    at = data.index(b"mvhd") + 4
    return struct.unpack(">II", data[at + 4 : at + 12])


def media_dates(data: bytes) -> tuple[int, int]:
    """(created, modified) from the media header (mdhd, 64-bit here)."""
    at = data.index(b"mdhd") + 4
    return struct.unpack(">QQ", data[at + 4 : at + 20])
