"""Albums: the user's planned albums, the mirror of the Flickr album list, and
creating albums on Flickr.

An album folder points to a row in `albums`: either an album that exists on
Flickr (mirrored by sync_with_flickr) or one the user planned, which is created
on Flickr together with its first photo (Flickr needs a cover photo).

Planned albums are compared with all others by normalized title (Unicode NFC,
whitespace collapsed, case ignored):

- green:  exactly one Flickr album with exactly this title; it is used
- yellow: a match only after normalizing, or several albums with this title;
          the user decides, until then nothing is uploaded into the album
- blue:   no match, or the user confirmed a new album; created on upload

Creating an album is serialized by one lock, and the intent (state
'creating' plus the cover photo) is committed first. If Flickr gives no
answer, the album stays 'creating' until a sync finds it by title and cover,
or finds that it was not created. Nothing is created twice blindly.
"""

import os
import sqlite3
import threading
import unicodedata
from collections import defaultdict
from dataclasses import dataclass

from flickr_uploader import db
from flickr_uploader.flickr_client import (
    Album,
    FlickrClientError,
    FlickrRejectedError,
    OutcomeUnknownError,
)

GREEN, YELLOW, BLUE = "green", "yellow", "blue"

# Only one album is ever being created at a time: two at once could duplicate it.
_CREATION_LOCK = threading.Lock()


class AlbumError(Exception):
    """Base class for errors raised by this module."""


class NotInAlbumFolderError(AlbumError):
    """Only folders inside an album folder can be ticked for upload."""


@dataclass
class SyncReport:
    flickr_albums: int = 0
    new: int = 0  # Flickr albums seen for the first time
    deleted: int = 0  # gone from Flickr (confirmed)
    reconciled: int = 0  # creations without answer, now settled
    renamed: int = 0
    merged: int = 0  # planned albums that exist on Flickr (green)


def normalize_title(title: str) -> str:
    return " ".join(unicodedata.normalize("NFC", title).split()).casefold()


# --- Album folders and the user's decisions -------------------------------------


def album_folder(conn: sqlite3.Connection, folder_id: int) -> sqlite3.Row | None:
    """The album folder a folder belongs to (the folder itself or an ancestor)."""
    folder = conn.execute(
        "SELECT root_id, rel_path FROM folders WHERE id = ?", (folder_id,)
    ).fetchone()
    if folder is None:
        raise db.RecordNotFoundError(f"folder {folder_id} does not exist")
    rel_path = folder["rel_path"]
    for candidate in conn.execute(
        "SELECT id, rel_path, album_id FROM folders WHERE root_id = ? AND album_id IS NOT NULL",
        (folder["root_id"],),
    ):
        ancestor = candidate["rel_path"]
        # The same test as the triggers that forbid nested album folders.
        if ancestor == "" or rel_path == ancestor or rel_path.startswith(ancestor + "/"):
            return candidate
    return None


def assign_album(conn: sqlite3.Connection, folder_id: int, title: str | None = None) -> int:
    """Make a folder an album folder and return the album id.

    The title defaults to the folder's name. If exactly one album (planned or
    on Flickr) has exactly this title, the folder joins it; otherwise a new
    album is planned. Raises sqlite3.IntegrityError for a folder above or
    below another album folder.
    """
    folder = conn.execute(
        "SELECT d.rel_path, r.path FROM folders d JOIN roots r ON r.id = d.root_id WHERE d.id = ?",
        (folder_id,),
    ).fetchone()
    if folder is None:
        raise db.RecordNotFoundError(f"folder {folder_id} does not exist")
    if title is None:
        title = folder["rel_path"].rsplit("/", 1)[-1] or _last_part(folder["path"])
    title = _clean(title)
    with db.transaction(conn):
        same = conn.execute("SELECT id FROM albums WHERE title = ?", (title,)).fetchall()
        if len(same) == 1:
            album_id = same[0]["id"]
        else:
            album_id = conn.execute(
                "INSERT INTO albums (title, state, updated_at) VALUES (?, 'planned', ?)",
                (title, db.utc_now()),
            ).lastrowid
        _link(conn, folder_id, album_id)
    return album_id


def use_album(conn: sqlite3.Connection, folder_id: int, album_id: int) -> None:
    """Point an album folder at another album, e.g. a Flickr album named differently."""
    with db.transaction(conn):
        if conn.execute("SELECT 1 FROM albums WHERE id = ?", (album_id,)).fetchone() is None:
            raise db.RecordNotFoundError(f"album {album_id} does not exist")
        folder = conn.execute("SELECT album_id FROM folders WHERE id = ?", (folder_id,)).fetchone()
        if folder is None:
            raise db.RecordNotFoundError(f"folder {folder_id} does not exist")
        if folder["album_id"] is None:
            raise AlbumError(f"folder {folder_id} is no album folder")
        _link(conn, folder_id, album_id)


def unassign_album(conn: sqlite3.Connection, folder_id: int) -> None:
    """The folder is no album folder any more, and nothing in it stays ticked.

    A planned album that no other folder uses is forgotten. Nothing changes on
    Flickr: photos already in the album stay there.
    """
    with db.transaction(conn):
        folder = conn.execute(
            "SELECT root_id, rel_path, album_id FROM folders WHERE id = ?", (folder_id,)
        ).fetchone()
        if folder is None:
            raise db.RecordNotFoundError(f"folder {folder_id} does not exist")
        if folder["album_id"] is None:
            return
        conn.execute("UPDATE folders SET album_id = NULL WHERE id = ?", (folder_id,))
        # Ticking only works inside an album folder (same test as the triggers).
        rel_path = folder["rel_path"]
        conn.execute(
            "UPDATE folders SET included = 0 WHERE root_id = ? AND (? = '' OR rel_path = ? "
            "OR substr(rel_path, 1, length(?) + 1) = ? || '/')",
            (folder["root_id"], rel_path, rel_path, rel_path, rel_path),
        )
        _drop_if_unused(conn, folder["album_id"])


def retitle_planned(conn: sqlite3.Connection, album_id: int, title: str) -> None:
    """Change the title of an album that is not on Flickr yet (purely local)."""
    cursor = conn.execute(
        "UPDATE albums SET title = ?, title_confirmed = 0, updated_at = ? "
        "WHERE id = ? AND state = 'planned'",
        (_clean(title), db.utc_now(), album_id),
    )
    if cursor.rowcount != 1:
        raise AlbumError(f"album {album_id} is not a planned album; rename it on Flickr instead")


def confirm_new_album(conn: sqlite3.Connection, album_id: int) -> None:
    """The user wants a new album despite similar titles on Flickr (resolves yellow)."""
    cursor = conn.execute(
        "UPDATE albums SET title_confirmed = 1, updated_at = ? WHERE id = ? AND state = 'planned'",
        (db.utc_now(), album_id),
    )
    if cursor.rowcount != 1:
        raise AlbumError(f"album {album_id} is not a planned album")


def set_included(conn: sqlite3.Connection, folder_id: int, included: bool) -> None:
    """Tick or untick a folder for upload. Ticking only works inside an album folder."""
    if included and album_folder(conn, folder_id) is None:
        raise NotInAlbumFolderError(f"folder {folder_id} is not inside an album folder")
    conn.execute("UPDATE folders SET included = ? WHERE id = ?", (int(included), folder_id))


def _link(conn: sqlite3.Connection, folder_id: int, album_id: int) -> None:
    row = conn.execute("SELECT album_id FROM folders WHERE id = ?", (folder_id,)).fetchone()
    if row is None:
        raise db.RecordNotFoundError(f"folder {folder_id} does not exist")
    conn.execute("UPDATE folders SET album_id = ? WHERE id = ?", (album_id, folder_id))
    if row["album_id"] is not None and row["album_id"] != album_id:
        _drop_if_unused(conn, row["album_id"])


def _drop_if_unused(conn: sqlite3.Connection, album_id: int) -> None:
    """Forget a planned album that no folder points to any more."""
    state = conn.execute("SELECT state FROM albums WHERE id = ?", (album_id,)).fetchone()[0]
    in_use = conn.execute("SELECT 1 FROM folders WHERE album_id = ?", (album_id,)).fetchone()
    if state == "planned" and in_use is None:
        conn.execute("DELETE FROM album_photos WHERE album_id = ?", (album_id,))
        conn.execute("DELETE FROM albums WHERE id = ?", (album_id,))


def _clean(title: str) -> str:
    title = title.strip()
    if not title:
        raise ValueError("an album title must not be empty")
    return title


def _last_part(path: str) -> str:
    return os.path.basename(path.rstrip("\\/")) or path


# --- Traffic light ----------------------------------------------------------------


def lights(conn: sqlite3.Connection) -> dict[int, str]:
    """The traffic light of every album: green, yellow or blue (module docstring).

    Albums on Flickr are green; one being created is blue.
    """
    rows = conn.execute("SELECT id, title, state, title_confirmed FROM albums").fetchall()
    by_title = defaultdict(list)
    for row in rows:
        by_title[normalize_title(row["title"])].append(row)
    result = {}
    for row in rows:
        if row["state"] == "exists":
            result[row["id"]] = GREEN
        elif row["state"] == "creating" or row["title_confirmed"]:
            result[row["id"]] = BLUE
        else:
            result[row["id"]] = _planned_light(row, by_title[normalize_title(row["title"])])
    return result


def _planned_light(album: sqlite3.Row, same_title: list[sqlite3.Row]) -> str:
    others = [other for other in same_title if other["id"] != album["id"]]
    if not others:
        return BLUE
    (only, *more) = others
    if not more and only["title"] == album["title"] and only["state"] == "exists":
        return GREEN
    return YELLOW


# --- Flickr -------------------------------------------------------------------------


def sync_with_flickr(conn: sqlite3.Connection, client) -> SyncReport:
    """Mirror the Flickr album list and settle everything that depends on it.

    In this order: albums whose creation got no answer are looked up by title
    and cover photo, the mirror is updated, albums gone from Flickr are planned
    again (only once Flickr confirms they are gone), pending renames are sent,
    and planned albums that exist on Flickr (green) are merged into them.
    """
    remote = client.albums()
    report = SyncReport(flickr_albums=len(remote))
    report.reconciled = _reconcile_creating(conn, remote)

    known = {
        row["flickr_id"]: row["id"]
        for row in conn.execute("SELECT id, flickr_id FROM albums WHERE flickr_id IS NOT NULL")
    }
    listed = {album.id for album in remote}
    # A short list must never be read as "deleted": ask about each one.
    gone = [
        album_id
        for flickr_id, album_id in known.items()
        if flickr_id not in listed and not client.album_exists(flickr_id)
    ]
    now = db.utc_now()
    with db.transaction(conn):
        for album in remote:
            if album.id in known:
                conn.execute(
                    "UPDATE albums SET title = ?, description = ?, photo_count = ?, "
                    "video_count = ?, last_synced = ?, updated_at = ?, "
                    "pending_title = CASE WHEN pending_title = ? THEN NULL ELSE pending_title END "
                    "WHERE id = ?",
                    (
                        album.title,
                        album.description,
                        album.photo_count,
                        album.video_count,
                        now,
                        now,
                        album.title,
                        known[album.id],
                    ),
                )
            else:
                conn.execute(
                    "INSERT INTO albums (title, description, flickr_id, state, photo_count, "
                    "video_count, last_synced, updated_at) VALUES (?, ?, ?, 'exists', ?, ?, ?, ?)",
                    (
                        album.title,
                        album.description,
                        album.id,
                        album.photo_count,
                        album.video_count,
                        now,
                        now,
                    ),
                )
                report.new += 1
        for album_id in gone:
            _forget_deleted(conn, album_id)
        report.deleted = len(gone)
    report.renamed = apply_pending_renames(conn, client)
    report.merged = _merge_green(conn)
    return report


def create_on_flickr(
    conn: sqlite3.Connection, client, album_id: int, cover_hash: str
) -> str | None:
    """Create a planned album on Flickr with this verified photo as its cover.

    Returns the Flickr id, or None if the album can't be created now: an
    earlier attempt got no answer (the next sync settles it), or the user has
    yet to decide about similar titles. Flickr's refusals are raised, with the
    album planned again.
    """
    with _CREATION_LOCK:
        album = conn.execute(
            "SELECT title, state, flickr_id FROM albums WHERE id = ?", (album_id,)
        ).fetchone()
        if album is None:
            raise db.RecordNotFoundError(f"album {album_id} does not exist")
        if album["state"] == "exists":
            return album["flickr_id"]
        if album["state"] == "creating" or lights(conn)[album_id] == YELLOW:
            return None
        cover = conn.execute(
            "SELECT flickr_id FROM photos WHERE hash = ? AND state = 'verified'", (cover_hash,)
        ).fetchone()
        if cover is None:
            raise AlbumError("the cover photo has to be on Flickr (verified) first")

        db.set_state(conn, db.ALBUMS, album_id, "creating", primary_photo_hash=cover_hash)
        try:
            flickr_id = client.create_album(album["title"], cover["flickr_id"])
        except OutcomeUnknownError as error:
            db.log_event(
                conn,
                "warning",
                f"Creating album '{album['title']}' got no answer; it is looked up on Flickr "
                f"before another attempt ({error})",
            )
            return None
        except FlickrClientError:
            # Refused, login rejected or cancelled before sending: nothing was created.
            db.set_state(conn, db.ALBUMS, album_id, "planned")
            raise
        with db.transaction(conn):
            db.set_state(
                conn,
                db.ALBUMS,
                album_id,
                "exists",
                flickr_id=flickr_id,
                created_by_tool=1,
                last_synced=db.utc_now(),
            )
            # Created without one; known, so a rename can send it along unchanged.
            conn.execute("UPDATE albums SET description = '' WHERE id = ?", (album_id,))
            db.log_event(conn, "info", f"Album '{album['title']}' created on Flickr")
        return flickr_id


def rename_on_flickr(conn: sqlite3.Connection, client, album_id: int, title: str) -> None:
    """Rename an album on Flickr, after the user confirmed it.

    The new title is stored as pending first; an interrupted rename is simply
    sent again by the next sync.
    """
    cursor = conn.execute(
        "UPDATE albums SET pending_title = ?, updated_at = ? WHERE id = ? AND state = 'exists'",
        (_clean(title), db.utc_now(), album_id),
    )
    if cursor.rowcount != 1:
        raise AlbumError(f"album {album_id} is not on Flickr; change its planned title instead")
    apply_pending_renames(conn, client)


def apply_pending_renames(conn: sqlite3.Connection, client) -> int:
    """Send the renames that are stored as pending; returns how many went through."""
    renamed = 0
    for album in conn.execute(
        "SELECT id, flickr_id, title, pending_title, description FROM albums "
        "WHERE pending_title IS NOT NULL AND state = 'exists'"
    ).fetchall():
        if album["description"] is None:
            continue  # unknown yet; sending "" could clear it. The next sync reads it.
        try:
            # The description goes along: the API docs leave open whether
            # leaving it out clears it.
            client.rename_album(album["flickr_id"], album["pending_title"], album["description"])
        except FlickrRejectedError as error:
            conn.execute("UPDATE albums SET pending_title = NULL WHERE id = ?", (album["id"],))
            db.log_event(
                conn,
                "error",
                f"Flickr refused to rename album '{album['title']}' to "
                f"'{album['pending_title']}': {error}",
            )
            continue
        conn.execute(
            "UPDATE albums SET title = pending_title, pending_title = NULL, updated_at = ? "
            "WHERE id = ?",
            (db.utc_now(), album["id"]),
        )
        renamed += 1
    return renamed


def _reconcile_creating(conn: sqlite3.Connection, remote: list[Album]) -> int:
    """Settle albums whose creation got no answer: created after all, or not."""
    creating = conn.execute(
        "SELECT a.id, a.title, p.flickr_id AS cover FROM albums a "
        "LEFT JOIN photos p ON p.hash = a.primary_photo_hash WHERE a.state = 'creating'"
    ).fetchall()
    taken = {r[0] for r in conn.execute("SELECT flickr_id FROM albums WHERE flickr_id NOT NULL")}
    for album in creating:
        # Title and cover photo together identify the album, even if other
        # albums have the same title.
        found = [
            candidate
            for candidate in remote
            if candidate.primary_photo_id == album["cover"]
            and normalize_title(candidate.title) == normalize_title(album["title"])
            and candidate.id not in taken
        ]
        if found:
            db.set_state(
                conn,
                db.ALBUMS,
                album["id"],
                "exists",
                flickr_id=found[0].id,
                created_by_tool=1,
                last_synced=db.utc_now(),
            )
            taken.add(found[0].id)
            message = f"Album '{album['title']}' was created on Flickr after all"
        else:
            db.set_state(conn, db.ALBUMS, album["id"], "planned")
            message = f"Album '{album['title']}' was not created on Flickr; next upload creates it"
        db.log_event(conn, "info", message)
    return len(creating)


def _forget_deleted(conn: sqlite3.Connection, album_id: int) -> None:
    title = conn.execute("SELECT title FROM albums WHERE id = ?", (album_id,)).fetchone()[0]
    if conn.execute("SELECT 1 FROM folders WHERE album_id = ?", (album_id,)).fetchone() is None:
        # Nobody plans with it: just drop it from the mirror.
        conn.execute("DELETE FROM album_photos WHERE album_id = ?", (album_id,))
        conn.execute("DELETE FROM albums WHERE id = ?", (album_id,))
        return
    db.set_state(
        conn,
        db.ALBUMS,
        album_id,
        "planned",
        flickr_id=None,
        primary_photo_hash=None,
        created_by_tool=0,
    )
    # added -> pending and verified -> pending are both allowed transitions.
    conn.execute(
        "UPDATE album_photos SET state = 'pending', updated_at = ? "
        "WHERE album_id = ? AND state != 'pending'",
        (db.utc_now(), album_id),
    )
    db.log_event(
        conn,
        "warning",
        f"Album '{title}' no longer exists on Flickr; the next upload of its folders "
        "creates it again",
    )


def _merge_green(conn: sqlite3.Connection) -> int:
    """Planned albums with exactly one Flickr album of the same title use that album."""
    current = lights(conn)
    planned = conn.execute("SELECT id, title FROM albums WHERE state = 'planned'").fetchall()
    merged = 0
    for album in planned:
        if current[album["id"]] != GREEN:
            continue
        target = conn.execute(
            "SELECT id FROM albums WHERE title = ? AND state = 'exists'", (album["title"],)
        ).fetchone()[0]
        with db.transaction(conn):
            conn.execute(
                "UPDATE folders SET album_id = ? WHERE album_id = ?", (target, album["id"])
            )
            conn.execute(
                "INSERT OR IGNORE INTO album_photos (album_id, photo_hash, state, updated_at) "
                "SELECT ?, photo_hash, 'pending', ? FROM album_photos WHERE album_id = ?",
                (target, db.utc_now(), album["id"]),
            )
            conn.execute("DELETE FROM album_photos WHERE album_id = ?", (album["id"],))
            conn.execute("DELETE FROM albums WHERE id = ?", (album["id"],))
            db.log_event(conn, "info", f"Album '{album['title']}' exists on Flickr and is used")
        merged += 1
    return merged
