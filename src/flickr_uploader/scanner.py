"""Scanning of the local folder tree into the database.

Records every folder and every media file below a root folder with size and
modification time, and keeps these records in sync on later scans. Files are
only listed, never opened; hashing is a separate, later step.

Safety rules:
- A root that can't be reached (unplugged drive) changes nothing.
- A folder that can't be read keeps its previous records; only what was
  actually looked at and not found is marked missing.
- Missing folders keep their row, including the album assignment, so a
  folder that comes back (drive plugged in again) is recognized.
- Links (symlinks, junctions) are not followed: they can point back up the
  tree or outside of the root.
"""

import os
import sqlite3
import stat
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from flickr_uploader import config, db
from flickr_uploader.config import MediaKind
from flickr_uploader.hashing import CancelFlag

# Reparse tags of directory links (winnt.h). A junction reports is_dir() True
# and is_symlink() False, so it has to be recognized by its tag.
_IO_REPARSE_TAG_MOUNT_POINT = 0xA0000003
_IO_REPARSE_TAG_SYMLINK = 0xA000000C

# File statuses the pre-check compares. A file entering one of them (new, or
# no longer skipped) has not been compared yet, so its folder is checked again.
CHECKABLE = frozenset({"new", "hashed"})


class ScanError(Exception):
    """Base class for errors raised by this module."""


class RootUnavailableError(ScanError):
    """The root folder does not exist or is not reachable right now."""


class OverlappingRootError(ScanError):
    """The folder lies inside another root, or another root lies inside it."""


@dataclass
class ScanReport:
    folders: int = 0
    files: int = 0
    new_files: int = 0
    changed_files: int = 0
    missing_files: int = 0
    missing_folders: int = 0
    unreadable: list[str] = field(default_factory=list)
    skipped_links: list[str] = field(default_factory=list)
    cancelled: bool = False


@dataclass(frozen=True)
class Totals:
    files: int = 0
    bytes: int = 0
    skipped: int = 0

    def __add__(self, other: "Totals") -> "Totals":
        return Totals(
            self.files + other.files, self.bytes + other.bytes, self.skipped + other.skipped
        )


@dataclass(frozen=True)
class FolderSummary:
    folder_id: int
    rel_path: str
    own: Totals  # files directly in this folder
    total: Totals  # including all subfolders


# --- Roots --------------------------------------------------------------------


def add_root(conn: sqlite3.Connection, path: str | Path) -> int:
    """Register a folder as root and return its id (the existing one if known)."""
    absolute = _normalized_root(path)
    for root_id, existing in conn.execute("SELECT id, path FROM roots"):
        if _same_path(existing, absolute):
            return root_id
        _check_not_nested(existing, absolute)
    return conn.execute("INSERT INTO roots (path) VALUES (?)", (absolute,)).lastrowid


def relocate_root(conn: sqlite3.Connection, root_id: int, new_path: str | Path) -> None:
    """Point a root to a new location, e.g. after a drive letter changed.

    All folders and files keep their ids, album assignments and hashes; the
    next scan compares them against the new location.
    """
    absolute = _normalized_root(new_path)
    for other_id, existing in conn.execute("SELECT id, path FROM roots WHERE id != ?", (root_id,)):
        if _same_path(existing, absolute):
            raise OverlappingRootError(f"{absolute} is already root {other_id}")
        _check_not_nested(existing, absolute)
    if conn.execute("UPDATE roots SET path = ? WHERE id = ?", (absolute, root_id)).rowcount != 1:
        raise db.RecordNotFoundError(f"root {root_id} does not exist")


def _normalized_root(path: str | Path) -> str:
    absolute = os.path.abspath(path)
    if not os.path.isdir(config.filesystem_path(absolute)):
        raise RootUnavailableError(f"{absolute} is not a reachable folder")
    return absolute


def _same_path(a: str, b: str) -> bool:
    return os.path.normcase(a) == os.path.normcase(b)


def overlaps(a: str | Path, b: str | Path) -> bool:
    """The two folders are the same, or one lies inside the other (case ignored)."""
    a = os.path.normcase(os.path.abspath(a)).rstrip("\\/")
    b = os.path.normcase(os.path.abspath(b)).rstrip("\\/")
    return a == b or b.startswith(a + os.sep) or a.startswith(b + os.sep)


def _check_not_nested(existing: str, new: str) -> None:
    # Nested roots would list the same folders twice and let album folders
    # overlap without the nesting trigger noticing (it works per root).
    a, b = os.path.normcase(existing).rstrip("\\/"), os.path.normcase(new).rstrip("\\/")
    if b.startswith(a + os.sep) or a.startswith(b + os.sep):
        raise OverlappingRootError(f"{new} overlaps with the existing root {existing}")


# --- Scanning -----------------------------------------------------------------


def scan_root(
    conn: sqlite3.Connection, root_id: int, *, cancel: CancelFlag | None = None
) -> ScanReport:
    """Walk the root folder and bring folders and files in the database up to date.

    Every folder is written in its own transaction: an aborted scan keeps what
    it has done so far, and the UI can show progress by reading the database.
    Folders are only marked missing after a complete walk.
    """
    row = conn.execute("SELECT path FROM roots WHERE id = ?", (root_id,)).fetchone()
    if row is None:
        raise db.RecordNotFoundError(f"root {root_id} does not exist")
    root_path = row["path"]
    top = config.filesystem_path(root_path)
    if not os.path.isdir(top):
        # Never conclude "everything is gone" from an unplugged drive.
        raise RootUnavailableError(f"{root_path} is not reachable")

    known = {
        os.path.normcase(r["rel_path"]): r
        for r in conn.execute(
            "SELECT id, rel_path, missing FROM folders WHERE root_id = ?", (root_id,)
        )
    }
    seen: set[int] = set()
    unreadable: list[str] = []
    report = ScanReport()
    stack = [(top, "")]

    while stack:
        if cancel is not None and cancel.is_set():
            report.cancelled = True
            return report
        directory, rel_path = stack.pop()
        display = folder_path(root_path, rel_path)
        try:
            with os.scandir(directory) as iterator:
                entries = list(iterator)
        except OSError as error:
            unreadable.append(rel_path)
            report.unreadable.append(display)
            db.log_event(
                conn, "warning", f"Folder not readable, left as it was: {display} ({error})"
            )
            folder = known.get(os.path.normcase(rel_path))
            if folder is not None:
                seen.add(folder["id"])
            continue

        files, subfolders, failed = [], [], set()
        for entry in entries:
            try:
                if entry.is_dir(follow_symlinks=False):
                    if _is_link(entry):
                        link = folder_path(root_path, _child(rel_path, entry.name))
                        report.skipped_links.append(link)
                        db.log_event(conn, "info", f"Link not followed: {link}")
                    elif entry.name.casefold() not in config.SYSTEM_DIRNAMES:
                        subfolders.append(entry)
                elif entry.is_file(follow_symlinks=False):
                    files.append((entry.name, entry.stat(follow_symlinks=False)))
            except OSError as error:
                failed.add(os.path.normcase(entry.name))
                db.log_event(
                    conn,
                    "warning",
                    f"Entry not readable: {folder_path(root_path, _child(rel_path, entry.name))} "
                    f"({error})",
                )

        with db.transaction(conn):
            folder_id = _upsert_folder(conn, root_id, rel_path, known)
            _sync_files(conn, folder_id, files, failed, report)
        seen.add(folder_id)
        report.folders += 1

        # Reverse order on the stack, so folders are visited alphabetically.
        for entry in sorted(subfolders, key=lambda e: e.name.casefold(), reverse=True):
            stack.append((entry.path, _child(rel_path, entry.name)))

    with db.transaction(conn):
        gone = [
            folder["id"]
            for folder in known.values()
            if folder["id"] not in seen and not _below_any(folder["rel_path"], unreadable)
        ]
        report.missing_folders = _mark_folders_missing(conn, gone)
        report.missing_files += _mark_files_missing(conn, gone)
        conn.execute("UPDATE roots SET scanned_at = ? WHERE id = ?", (db.utc_now(), root_id))
    return report


def _is_link(entry: os.DirEntry) -> bool:
    if entry.is_symlink():
        return True
    info = entry.stat(follow_symlinks=False)
    if not getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
        return False
    # Other reparse points (e.g. OneDrive folders) are ordinary folders.
    tag = getattr(info, "st_reparse_tag", 0)
    return tag in (_IO_REPARSE_TAG_MOUNT_POINT, _IO_REPARSE_TAG_SYMLINK)


def _child(rel_path: str, name: str) -> str:
    return f"{rel_path}/{name}" if rel_path else name


def folder_path(root_path: str, rel_path: str) -> str:
    """The plain (display) path of a folder; use config.filesystem_path() to open it."""
    return os.path.join(root_path, *rel_path.split("/")) if rel_path else root_path


def _below_any(rel_path: str, prefixes: Iterable[str]) -> bool:
    folded = os.path.normcase(rel_path)
    for prefix in prefixes:
        folded_prefix = os.path.normcase(prefix)
        if folded_prefix == "" or folded == folded_prefix:
            return True
        if folded.startswith(folded_prefix + os.path.normcase("/")):
            return True
    return False


def _upsert_folder(
    conn: sqlite3.Connection, root_id: int, rel_path: str, known: dict[str, sqlite3.Row]
) -> int:
    # Matched case-insensitively like Windows does: renaming "portugal" to
    # "Portugal" must keep the folder, including its album assignment.
    folder = known.get(os.path.normcase(rel_path))
    if folder is None:
        return conn.execute(
            "INSERT INTO folders (root_id, rel_path) VALUES (?, ?)", (root_id, rel_path)
        ).lastrowid
    if folder["rel_path"] != rel_path or folder["missing"]:
        conn.execute(
            "UPDATE folders SET rel_path = ?, missing = 0 WHERE id = ?", (rel_path, folder["id"])
        )
    return folder["id"]


def _target_status(kind: MediaKind, size: int, has_hash: bool) -> str:
    if kind is MediaKind.RAW:
        return "skipped_raw"
    if kind is MediaKind.UNSUPPORTED:
        return "skipped_unsupported"
    if config.is_too_large(kind, size):
        return "skipped_too_large"
    return "hashed" if has_hash else "new"


def _sync_files(
    conn: sqlite3.Connection,
    folder_id: int,
    files: list[tuple[str, os.stat_result]],
    failed: set[str],
    report: ScanReport,
) -> None:
    existing = {
        os.path.normcase(r["name"]): r
        for r in conn.execute(
            "SELECT id, name, size_bytes, mtime_ns, hash, status FROM files WHERE folder_id = ?",
            (folder_id,),
        )
    }
    now = db.utc_now()
    unchecked = False
    for name, info in files:
        kind = config.media_kind(name)
        if kind is MediaKind.IGNORED:
            continue
        report.files += 1
        size, mtime = info.st_size, info.st_mtime_ns
        row = existing.pop(os.path.normcase(name), None)

        if row is None:
            status = _target_status(kind, size, False)
            conn.execute(
                "INSERT INTO files (folder_id, name, size_bytes, mtime_ns, status, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (folder_id, name, size, mtime, status, now),
            )
            unchecked |= status in CHECKABLE
            report.new_files += 1
        elif (row["size_bytes"], row["mtime_ns"]) != (size, mtime):
            # New content: everything derived from the old content is void.
            conn.execute(
                "UPDATE files SET name = ?, size_bytes = ?, mtime_ns = ?, hash = NULL, "
                "status = ?, taken_at = NULL, width = NULL, height = NULL, "
                "last_error = NULL, updated_at = ? WHERE id = ?",
                (name, size, mtime, _target_status(kind, size, False), now, row["id"]),
            )
            conn.execute("DELETE FROM precheck_matches WHERE file_id = ?", (row["id"],))
            conn.execute("UPDATE folders SET prechecked_at = NULL WHERE id = ?", (folder_id,))
            report.changed_files += 1
        else:
            # Same content. The status may still change: a file that was
            # missing is back, a read error may be gone, limits may differ.
            status = _target_status(kind, size, row["hash"] is not None)
            if row["name"] != name or row["status"] != status:
                conn.execute(
                    "UPDATE files SET name = ?, status = ?, last_error = NULL, updated_at = ? "
                    "WHERE id = ?",
                    (name, status, now, row["id"]),
                )
                # E.g. a format Flickr accepts now, or a file that is back.
                unchecked |= status in CHECKABLE and row["status"] not in CHECKABLE

    if unchecked:
        conn.execute("UPDATE folders SET prechecked_at = NULL WHERE id = ?", (folder_id,))

    for key, row in existing.items():
        if key not in failed and row["status"] != "missing":
            conn.execute(
                "UPDATE files SET status = 'missing', updated_at = ? WHERE id = ?", (now, row["id"])
            )
            report.missing_files += 1


def _mark_folders_missing(conn: sqlite3.Connection, folder_ids: list[int]) -> int:
    return sum(
        conn.execute(
            "UPDATE folders SET missing = 1 WHERE id = ? AND missing = 0", (folder_id,)
        ).rowcount
        for folder_id in folder_ids
    )


def _mark_files_missing(conn: sqlite3.Connection, folder_ids: list[int]) -> int:
    now = db.utc_now()
    return sum(
        conn.execute(
            "UPDATE files SET status = 'missing', updated_at = ? "
            "WHERE folder_id = ? AND status != 'missing'",
            (now, folder_id),
        ).rowcount
        for folder_id in folder_ids
    )


# --- Reading results ----------------------------------------------------------


def folder_summaries(conn: sqlite3.Connection, root_id: int) -> list[FolderSummary]:
    """File counts and sizes per folder, own and including subfolders.

    Computed from the file records every time instead of being stored, so the
    numbers can never disagree with the records. Missing folders and files
    are left out.
    """
    folders = conn.execute(
        "SELECT id, rel_path FROM folders WHERE root_id = ? AND missing = 0 ORDER BY rel_path",
        (root_id,),
    ).fetchall()
    own = {folder["id"]: Totals() for folder in folders}
    for row in conn.execute(
        "SELECT f.folder_id, count(*) AS files, sum(f.size_bytes) AS bytes, "
        "sum(f.status LIKE 'skipped_%') AS skipped "
        "FROM files f JOIN folders d ON d.id = f.folder_id "
        "WHERE d.root_id = ? AND d.missing = 0 AND f.status != 'missing' "
        "GROUP BY f.folder_id",
        (root_id,),
    ):
        own[row["folder_id"]] = Totals(row["files"], row["bytes"], row["skipped"])

    by_path = {folder["rel_path"]: folder["id"] for folder in folders}
    total = dict(own)
    for folder in folders:
        rel_path = folder["rel_path"]
        if not rel_path:
            continue
        # Add this folder's own files to every ancestor that is present.
        parts = rel_path.split("/")
        for depth in range(len(parts)):
            ancestor = by_path.get("/".join(parts[:depth]))
            if ancestor is not None:
                total[ancestor] = total[ancestor] + own[folder["id"]]

    return [
        FolderSummary(folder["id"], folder["rel_path"], own[folder["id"]], total[folder["id"]])
        for folder in folders
    ]
