"""Pre-check: find photos of a folder that are already on Flickr.

Photos uploaded before this tool carry no hash tag, so they can't be
recognized by content. The pre-check compares a ticked folder with the
user's Flickr photos taken in the same period:

- certain:  same capture time (to the second) + Flickr title = file name
            without extension + same pixel size (either orientation)
- probable: same capture time + (title or pixel size), or, for files without
            a capture date (videos, screenshots), the title alone within the
            folder's period

Matches are only suggestions. The user decides per file: "is this photo"
(the Flickr photo is taken over instead of uploaded again) or "upload
anyway". Until then the file is not uploaded.
"""

import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta

from PIL import ExifTags, Image, UnidentifiedImageError

from flickr_uploader import config, db, flickr_client, scanner
from flickr_uploader.config import MediaKind
from flickr_uploader.hashing import CancelFlag

FLICKR_TIME = "%Y-%m-%d %H:%M:%S"
_EXIF_TIME = re.compile(r"(\d{4}):(\d{2}):(\d{2}) (\d{2}):(\d{2}):(\d{2})")
# Undated files are matched by title within the folder's period plus this margin.
SEARCH_MARGIN = timedelta(days=1)
_ELIGIBLE = tuple(sorted(scanner.CHECKABLE))


@dataclass(frozen=True)
class Metadata:
    taken: str | None  # "YYYY-MM-DD HH:MM:SS", camera local time like Flickr's
    width: int | None
    height: int | None


@dataclass
class PrecheckReport:
    files: int = 0
    without_date: int = 0
    unreadable: int = 0
    certain: int = 0  # files with at least one certain match
    probable: int = 0  # files with probable matches only
    searched: tuple[str, str] | None = None
    cancelled: bool = False


# --- Reading metadata ---------------------------------------------------------


def read_metadata(path: str | os.PathLike) -> Metadata:
    """Capture time and pixel size of a photo; only the file header is read.

    Videos are not opened (Pillow can't read them). Raises OSError for files
    that can't be read as images.
    """
    if config.media_kind(os.path.basename(path)) is not MediaKind.PHOTO:
        return Metadata(None, None, None)
    try:
        with Image.open(config.filesystem_path(path)) as image:
            exif = image.getexif()
            taken = exif.get_ifd(ExifTags.IFD.Exif).get(ExifTags.Base.DateTimeOriginal)
            taken = taken or exif.get(ExifTags.Base.DateTime)
            width, height = image.size
    except (UnidentifiedImageError, Image.DecompressionBombError, SyntaxError, ValueError) as e:
        # Broken or absurdly large files; SyntaxError is what Pillow raises
        # for some malformed headers.
        raise OSError(f"no readable image header: {type(e).__name__}") from None
    return Metadata(normalize_exif_time(taken), width, height)


def normalize_exif_time(value) -> str | None:
    """'2026:06:01 10:00:00' -> '2026-06-01 10:00:00'; None for missing or bogus values."""
    if not isinstance(value, str):
        return None
    match = _EXIF_TIME.fullmatch(value.strip().rstrip("\x00").strip())
    if not match:
        return None
    text = "{}-{}-{} {}:{}:{}".format(*match.groups())
    try:
        datetime.strptime(text, FLICKR_TIME)  # rejects 0000-00-00 and the like
    except ValueError:
        return None
    return text


# --- The check ----------------------------------------------------------------


def precheck_folder(
    conn: sqlite3.Connection,
    client: flickr_client.FlickrClient,
    folder_id: int,
    *,
    cancel: CancelFlag | None = None,
) -> PrecheckReport:
    """Compare the files directly in a folder with the user's Flickr photos.

    Metadata that was read is kept even if the check is cancelled or Flickr
    can't be reached; the folder only counts as checked once matches are
    stored. Flickr errors are passed on.
    """
    row = conn.execute(
        "SELECT r.path, d.rel_path FROM folders d JOIN roots r ON r.id = d.root_id WHERE d.id = ?",
        (folder_id,),
    ).fetchone()
    if row is None:
        raise db.RecordNotFoundError(f"folder {folder_id} does not exist")
    directory = scanner.folder_path(row["path"], row["rel_path"])
    files = conn.execute(
        f"SELECT id, name, taken_at, width, height FROM files "
        f"WHERE folder_id = ? AND status IN ({', '.join('?' * len(_ELIGIBLE))}) ORDER BY name",
        (folder_id, *_ELIGIBLE),
    ).fetchall()

    report = PrecheckReport(files=len(files))
    # Read everything first and write it in one short transaction afterwards:
    # holding the write lock while reading a thousand files from a slow disk
    # would make other writers (uploader, UI) give up with "database is locked".
    metadata, new_metadata, problems = {}, {}, {}
    for file in files:
        if cancel is not None and cancel.is_set():
            report.cancelled = True
            break
        if file["taken_at"] is not None or file["width"] is not None:
            metadata[file["id"]] = Metadata(file["taken_at"], file["width"], file["height"])
            continue
        path = os.path.join(directory, file["name"])
        try:
            metadata[file["id"]] = new_metadata[file["id"]] = read_metadata(path)
        except OSError as error:
            metadata[file["id"]] = Metadata(None, None, None)
            problems[file["id"]] = f"Capture date not readable: {path} ({error})"
    report.unreadable = len(problems)
    _save_metadata(conn, new_metadata, problems)
    if report.cancelled:
        return report

    dated = [meta.taken for meta in metadata.values() if meta.taken]
    report.without_date = len(files) - len(dated)
    matches: dict[int, dict[str, str]] = {}
    if dated:
        start = datetime.strptime(min(dated), FLICKR_TIME) - SEARCH_MARGIN
        end = datetime.strptime(max(dated), FLICKR_TIME) + SEARCH_MARGIN
        report.searched = (start.strftime(FLICKR_TIME), end.strftime(FLICKR_TIME))
        found = _search(client, start, end)
        known = {
            r[0] for r in conn.execute("SELECT flickr_id FROM photos WHERE flickr_id NOT NULL")
        }
        candidates = [photo for photo in found if photo.id not in known]
        for file in files:
            file_matches = _match(file["name"], metadata[file["id"]], candidates)
            if file_matches:
                matches[file["id"]] = file_matches
                if "certain" in file_matches.values():
                    report.certain += 1
                else:
                    report.probable += 1

    _store(conn, folder_id, [file["id"] for file in files], matches, report)
    return report


def _save_metadata(conn, new_metadata: dict[int, Metadata], problems: dict[int, str]) -> None:
    now = db.utc_now()
    with db.transaction(conn):
        conn.executemany(
            "UPDATE files SET taken_at = ?, width = ?, height = ?, updated_at = ? WHERE id = ?",
            [
                (meta.taken, meta.width, meta.height, now, file_id)
                for file_id, meta in new_metadata.items()
            ],
        )
        for file_id, message in problems.items():
            db.log_event(conn, "warning", message, file_id=file_id)


def _search(client, start: datetime, end: datetime) -> list[flickr_client.FoundPhoto]:
    """All photos taken in [start, end]; halves the period while Flickr would cut it off."""
    try:
        found = client.photos_taken_between(start.strftime(FLICKR_TIME), end.strftime(FLICKR_TIME))
    except flickr_client.SearchTooBroadError:
        if end - start < timedelta(seconds=2):
            raise
        middle = (start + (end - start) / 2).replace(microsecond=0)
        # Both limits are inclusive at Flickr, so the halves must not overlap.
        found = _search(client, start, middle) + _search(client, middle + timedelta(seconds=1), end)
    unique = {photo.id: photo for photo in found}
    return list(unique.values())


def _match(name: str, meta: Metadata, candidates) -> dict[str, str]:
    """flickr_id -> "certain" | "probable" for one file."""
    stem = os.path.splitext(name)[0].casefold()
    matches = {}
    for photo in candidates:
        same_title = photo.title.casefold() == stem
        if meta.taken:
            if photo.taken != meta.taken:
                continue
            same_size = _same_size(meta, photo)
            if same_title and same_size:
                matches[photo.id] = "certain"
            elif same_title or same_size:
                matches[photo.id] = "probable"
        elif same_title:
            matches[photo.id] = "probable"
    return matches


def _same_size(meta: Metadata, photo: flickr_client.FoundPhoto) -> bool:
    if None in (meta.width, meta.height, photo.width, photo.height):
        return False
    # Portrait photos may be reported rotated, depending on the EXIF orientation.
    return (meta.width, meta.height) in (
        (photo.width, photo.height),
        (photo.height, photo.width),
    )


def _store(conn, folder_id: int, file_ids: list[int], matches, report: PrecheckReport) -> None:
    now = db.utc_now()
    with db.transaction(conn):
        # Open suggestions are replaced; the user's decisions are kept.
        conn.executemany(
            "DELETE FROM precheck_matches WHERE file_id = ? AND decision = 'open'",
            [(file_id,) for file_id in file_ids],
        )
        conn.executemany(
            "INSERT OR IGNORE INTO precheck_matches (file_id, flickr_id, confidence, checked_at) "
            "VALUES (?, ?, ?, ?)",
            [
                (file_id, flickr_id, confidence, now)
                for file_id, file_matches in matches.items()
                for flickr_id, confidence in file_matches.items()
            ],
        )
        conn.execute("UPDATE folders SET prechecked_at = ? WHERE id = ?", (now, folder_id))
        db.log_event(
            conn,
            "info",
            f"Pre-check: {report.files} file(s), {report.certain} certainly and "
            f"{report.probable} probably already on Flickr, {report.without_date} without "
            f"capture date, {report.unreadable} unreadable",
        )


# --- Decisions ----------------------------------------------------------------


def choose_existing(conn: sqlite3.Connection, file_id: int, flickr_id: str) -> None:
    """The user says: this file IS that Flickr photo (take it over, don't upload)."""
    with db.transaction(conn):
        if not conn.execute(
            "SELECT 1 FROM precheck_matches WHERE file_id = ? AND flickr_id = ?",
            (file_id, flickr_id),
        ).fetchone():
            raise db.RecordNotFoundError(f"{flickr_id} is not a match of file {file_id}")
        now = db.utc_now()
        # One file corresponds to exactly one Flickr photo.
        conn.execute(
            "UPDATE precheck_matches SET decision = CASE WHEN flickr_id = ? THEN 'same' "
            "ELSE 'different' END, decided_at = ? WHERE file_id = ?",
            (flickr_id, now, file_id),
        )


def choose_upload(conn: sqlite3.Connection, file_id: int) -> None:
    """The user says: none of the matches is this file, upload it."""
    conn.execute(
        "UPDATE precheck_matches SET decision = 'different', decided_at = ? WHERE file_id = ?",
        (db.utc_now(), file_id),
    )


def reset_choice(conn: sqlite3.Connection, file_id: int) -> None:
    conn.execute(
        "UPDATE precheck_matches SET decision = 'open', decided_at = NULL WHERE file_id = ?",
        (file_id,),
    )


def is_cleared(conn: sqlite3.Connection, file_id: int) -> bool:
    """The pre-check lets this file through: its folder was checked, nothing is open."""
    row = conn.execute(
        "SELECT d.prechecked_at IS NOT NULL AS checked, "
        "EXISTS (SELECT 1 FROM precheck_matches m "
        "        WHERE m.file_id = f.id AND m.decision = 'open') AS open_matches "
        "FROM files f JOIN folders d ON d.id = f.folder_id WHERE f.id = ?",
        (file_id,),
    ).fetchone()
    if row is None:
        raise db.RecordNotFoundError(f"file {file_id} does not exist")
    return bool(row["checked"]) and not row["open_matches"]


def chosen_flickr_photo(conn: sqlite3.Connection, file_id: int) -> str | None:
    """The Flickr photo the user said this file is, if any."""
    row = conn.execute(
        "SELECT flickr_id FROM precheck_matches WHERE file_id = ? AND decision = 'same'",
        (file_id,),
    ).fetchone()
    return row[0] if row else None
