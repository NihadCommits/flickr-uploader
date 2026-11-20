"""What becomes of videos too large or too long for Flickr (spec, "Zu große und lange Videos").

A video over Flickr's size limit goes up compressed or split, as the user
chose (or the default in the settings); one longer than Flickr plays may be
split. Each piece is a photo of its own. Its digest is not a hash of its
bytes, which differ with every encoding, but derived from the original's
hash and the plan, so it is the same in every run and the hash-tag search
protects pieces against duplicates like any photo.
"""

import os
import sqlite3

import xxhash

from flickr_uploader import config, db, scanner, settings, videotools
from flickr_uploader.config import MediaKind
from flickr_uploader.hashing import CancelFlag

COMPRESS, SPLIT, ORIGINAL = "compress", "split", "original"
# Part of every derived digest: a new way of compressing or cutting gets new pieces.
METHOD_VERSION = 1


class VideoPlanError(Exception):
    """The choice can't be made or changed (the message says why)."""


def is_video(name: str) -> bool:
    return config.media_kind(name) is MediaKind.VIDEO


def too_large(file: sqlite3.Row) -> bool:
    return file["status"] == "skipped_too_large" and is_video(file["name"])


def too_long(file: sqlite3.Row) -> bool:
    duration = file["duration_seconds"]
    return duration is not None and duration > config.LONG_VIDEO_SECONDS


def choice(conn: sqlite3.Connection, file: sqlite3.Row) -> str | None:
    """How this file goes up: ORIGINAL, COMPRESS, SPLIT, or None (the user must choose).

    `file` needs id, name, status and duration_seconds.
    """
    planned = conn.execute(
        "SELECT choice FROM video_plans WHERE file_id = ?", (file["id"],)
    ).fetchone()
    if planned is not None:
        return planned["choice"]
    if too_large(file):
        default = settings.get(conn, settings.VIDEO_TOO_LARGE)
        return None if default == "ask" else default
    if is_video(file["name"]) and too_long(file):
        return SPLIT if settings.get(conn, settings.VIDEO_TOO_LONG) == "split" else ORIGINAL
    return ORIGINAL


def allowed(file: sqlite3.Row) -> tuple[str, ...]:
    """The choices that make sense for this file (empty: it just goes up)."""
    if too_large(file):
        return (COMPRESS, SPLIT)
    if is_video(file["name"]) and too_long(file):
        return (ORIGINAL, SPLIT)
    return ()


def decide(conn: sqlite3.Connection, file_id: int, chosen: str | None) -> None:
    """Store the user's choice (None: back to the default); fixed once a piece is up."""
    file = conn.execute(
        "SELECT id, name, status, duration_seconds FROM files WHERE id = ?", (file_id,)
    ).fetchone()
    if file is None:
        raise db.RecordNotFoundError(f"file {file_id} does not exist")
    if chosen is not None and chosen not in allowed(file):
        raise VideoPlanError(f"{chosen!r} is not possible for {file['name']}")
    if started(conn, file_id):
        raise VideoPlanError("pieces of this video are on Flickr already")
    with db.transaction(conn):
        conn.execute("DELETE FROM video_parts WHERE file_id = ?", (file_id,))
        conn.execute("DELETE FROM video_plans WHERE file_id = ?", (file_id,))
        if chosen is not None:
            conn.execute(
                "INSERT INTO video_plans (file_id, choice, decided_at) VALUES (?, ?, ?)",
                (file_id, chosen, db.utc_now()),
            )


def started(conn: sqlite3.Connection, file_id: int) -> bool:
    """Has any piece of this video got further than 'pending'?"""
    return bool(
        conn.execute(
            "SELECT EXISTS (SELECT 1 FROM video_parts v JOIN photos p ON p.hash = v.photo_hash "
            "WHERE v.file_id = ? AND p.state != 'pending')",
            (file_id,),
        ).fetchone()[0]
    )


def part_digest(source_hash: str, chosen: str, part: int, parts: int) -> str:
    """The fixed digest (and hash tag) of one piece, 32 hex digits like a file hash."""
    key = f"{source_hash}|{chosen}|{part}/{parts}|v{METHOD_VERSION}"
    return xxhash.xxh3_128(key.encode()).hexdigest()


def plan_parts(
    conn: sqlite3.Connection, file_id: int, chosen: str, source_hash: str, parts: int
) -> list[str]:
    """Commit the pieces (intent first: photos as 'pending') and return their digests."""
    digests = [part_digest(source_hash, chosen, part, parts) for part in range(1, parts + 1)]
    now = db.utc_now()
    with db.transaction(conn):
        conn.execute(
            "INSERT INTO video_plans (file_id, choice, decided_at) VALUES (?, ?, ?) "
            "ON CONFLICT (file_id) DO NOTHING",
            (file_id, chosen, now),
        )
        conn.execute(
            "UPDATE video_plans SET source_hash = ?, parts = ? WHERE file_id = ?",
            (source_hash, parts, file_id),
        )
        conn.execute("DELETE FROM video_parts WHERE file_id = ?", (file_id,))
        for part, digest in enumerate(digests, start=1):
            conn.execute(
                "INSERT OR IGNORE INTO photos (hash, state, updated_at) VALUES (?, 'pending', ?)",
                (digest, now),
            )
            conn.execute(
                "INSERT INTO video_parts (file_id, part, photo_hash) VALUES (?, ?, ?)",
                (file_id, part, digest),
            )
    return digests


def planned_parts(conn: sqlite3.Connection, file_id: int, source_hash: str) -> list[str] | None:
    """The committed pieces, if they belong to this content of the file."""
    plan = conn.execute(
        "SELECT source_hash, parts FROM video_plans WHERE file_id = ?", (file_id,)
    ).fetchone()
    if plan is None or plan["source_hash"] != source_hash or plan["parts"] is None:
        return None
    rows = conn.execute(
        "SELECT photo_hash FROM video_parts WHERE file_id = ? ORDER BY part", (file_id,)
    ).fetchall()
    return [row[0] for row in rows] if len(rows) == plan["parts"] else None


def read_durations(
    conn: sqlite3.Connection, folder_id: int, *, cancel: CancelFlag | None = None
) -> int:
    """Read the length of the folder's videos that lack one; returns how many were read.

    A video ffmpeg can't read keeps no length (it is then simply not "long").
    """
    rows = conn.execute(
        "SELECT f.id, f.name, d.rel_path, r.path AS root_path FROM files f "
        "JOIN folders d ON d.id = f.folder_id JOIN roots r ON r.id = d.root_id "
        "WHERE f.folder_id = ? AND f.duration_seconds IS NULL "
        "AND f.status IN ('new', 'hashed', 'skipped_too_large')",
        (folder_id,),
    ).fetchall()
    read = 0
    for row in rows:
        if not is_video(row["name"]):
            continue
        if cancel is not None and cancel.is_set():
            break
        path = os.path.join(scanner.folder_path(row["root_path"], row["rel_path"]), row["name"])
        try:
            info = videotools.probe(path)
        except (videotools.VideoToolError, OSError):
            continue
        conn.execute(
            "UPDATE files SET duration_seconds = ? WHERE id = ?", (info.duration, row["id"])
        )
        read += 1
    return read
