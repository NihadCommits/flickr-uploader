"""The dry run: what an upload run would do now, file by file (spec, "Probelauf").

Reads only the database: nothing is hashed, nothing is sent to Flickr, nothing
is written. It follows the same rules as uploader.py, so what it shows is what
a run would do, as far as the database knows. Content seen for the first time
is not hashed yet, so "upload" may still turn out as "already on Flickr" (the
same photo in another folder).
"""

import os
import sqlite3
from dataclasses import dataclass, field

from flickr_uploader import albums, config, precheck, scanner, videoplans, videotools

# What happens to a file.
UPLOAD = "upload"
COMPRESS = "compress"
SPLIT = "split"
TAKE_OVER = "take_over"  # the old Flickr photo the user picked
DONE = "done"  # on Flickr already
LOOK_UP = "look_up"  # upload without answer: searched by hash tag first
CHECK = "check"  # the pre-check runs first, then it goes up unless it finds a match
WAIT_MATCH = "wait_match"
WAIT_ALBUM = "wait_album"
WAIT_VIDEO = "wait_video"
NO_ALBUM = "no_album"
SKIPPED = "skipped"
FAILED = "failed"

_SKIP_REASONS = {
    "skipped_raw": "RAW-Datei",
    "skipped_too_large": "zu groß für Flickr",
    "skipped_unsupported": "Format nimmt Flickr nicht",
}


@dataclass
class PlannedFile:
    name: str
    path: str
    action: str
    detail: str = ""
    uploads: int = 0  # how many uploads to Flickr this causes


@dataclass
class PlannedAlbum:
    title: str
    state: str  # "exists", "create" or "waits"
    files: list[PlannedFile] = field(default_factory=list)


@dataclass
class DryRun:
    albums: list[PlannedAlbum]
    outside: list[PlannedFile]  # ticked, but not inside an album folder
    totals: dict[str, int]


def dry_run(conn: sqlite3.Connection) -> DryRun:
    lights = albums.lights(conn)
    rows = conn.execute(
        "SELECT f.id, f.name, f.size_bytes, f.status, f.duration_seconds, f.folder_id, "
        "p.state AS photo, d.rel_path, r.path AS root_path "
        "FROM files f JOIN folders d ON d.id = f.folder_id JOIN roots r ON r.id = d.root_id "
        "LEFT JOIN photos p ON p.hash = f.hash "
        "WHERE d.included = 1 AND d.missing = 0 AND f.status != 'missing' "
        "ORDER BY r.path, d.rel_path, f.name"
    ).fetchall()
    planned: dict[int, PlannedAlbum] = {}
    outside: list[PlannedFile] = []
    album_of: dict[int, int | None] = {}
    for row in rows:
        if row["folder_id"] not in album_of:
            folder = albums.album_folder(conn, row["folder_id"])
            album_of[row["folder_id"]] = folder["album_id"] if folder else None
        album_id = album_of[row["folder_id"]]
        path = os.path.join(scanner.folder_path(row["root_path"], row["rel_path"]), row["name"])
        if album_id is None:
            outside.append(PlannedFile(row["name"], path, NO_ALBUM))
            continue
        if album_id not in planned:
            album = conn.execute(
                "SELECT title, state FROM albums WHERE id = ?", (album_id,)
            ).fetchone()
            light = lights.get(album_id)
            state = (
                "waits"
                if light == albums.YELLOW
                else "exists"
                if album["state"] == "exists"
                else "create"
            )
            planned[album_id] = PlannedAlbum(album["title"], state)
        album = planned[album_id]
        album.files.append(_plan_file(conn, row, path, album.state == "waits"))

    everything = [f for album in planned.values() for f in album.files] + outside
    totals: dict[str, int] = {}
    for planned_file in everything:
        totals[planned_file.action] = totals.get(planned_file.action, 0) + 1
    totals["uploads"] = sum(f.uploads for f in everything)
    totals["new_albums"] = sum(
        album.state == "create" and any(f.uploads for f in album.files)
        for album in planned.values()
    )
    return DryRun(list(planned.values()), outside, totals)


def _plan_file(conn: sqlite3.Connection, row: sqlite3.Row, path: str, album_waits: bool):
    def planned(action: str, detail: str = "", uploads: int = 0) -> PlannedFile:
        return PlannedFile(row["name"], path, action, detail, uploads)

    is_too_large_video = videoplans.too_large(row)
    if row["status"] in _SKIP_REASONS and not is_too_large_video:
        return planned(SKIPPED, _SKIP_REASONS[row["status"]])
    if album_waits:
        return planned(WAIT_ALBUM)
    if row["photo"] == "verified":
        return planned(DONE)
    if row["photo"] == "failed":
        return planned(FAILED, "ein früherer Versuch ist fehlgeschlagen")
    checked = conn.execute(
        "SELECT prechecked_at IS NOT NULL FROM folders WHERE id = ?", (row["folder_id"],)
    ).fetchone()[0]
    if not checked:
        # The run checks the folder first; too large videos are not part of that.
        if not is_too_large_video:
            return planned(CHECK, uploads=1)
    elif not precheck.is_cleared(conn, row["id"]):
        return planned(WAIT_MATCH)
    if precheck.chosen_flickr_photo(conn, row["id"]):
        return planned(TAKE_OVER)
    how = videoplans.choice(conn, row)
    if how is None:
        return planned(WAIT_VIDEO)
    if how == videoplans.ORIGINAL:
        if row["photo"] == "unclear":
            return planned(LOOK_UP)
        return planned(UPLOAD, uploads=1)
    states = [
        r[0]
        for r in conn.execute(
            "SELECT p.state FROM video_parts v JOIN photos p ON p.hash = v.photo_hash "
            "WHERE v.file_id = ? ORDER BY v.part",
            (row["id"],),
        )
    ]
    if states and all(state == "verified" for state in states):
        return planned(DONE)
    if how == videoplans.COMPRESS:
        return planned(COMPRESS, uploads=0 if states == ["verified"] else 1)
    parts = len(states) or (
        videotools.split_count(
            row["size_bytes"],
            row["duration_seconds"],
            config.VIDEO_PART_BYTES,
            config.LONG_VIDEO_SECONDS,
        )
        if row["duration_seconds"]
        else 0
    )
    missing = sum(state != "verified" for state in states) if states else parts
    detail = f"{parts} Teile" if parts else "Anzahl der Teile nach dem Lesen des Videos"
    return planned(SPLIT, detail, uploads=missing or 1)
