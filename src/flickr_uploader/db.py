"""SQLite persistence: schema, migrations, connections, backups and state machines.

The database is the single source of truth of the application (see
docs/PROJECT_SPEC.md). This module is built so that a committed change is never
lost and a write that would put the data into an impossible state is rejected,
by SQLite itself wherever possible.

Typical use:

    database = Database(path)
    database.open()                      # lock, integrity check, migrations
    with database.connect() as conn:     # one short-lived connection per thread
        set_state(conn, PHOTOS, photo_hash, "uploading", upload_started_at=utc_now())
    database.close()
"""

import sqlite3
import sys
from collections.abc import Iterator, Mapping
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import IO

SCHEMA_VERSION = 2
MIN_SQLITE_VERSION = (3, 37, 0)  # first version with STRICT tables
BUSY_TIMEOUT_SECONDS = 5.0
DEFAULT_KEEP_BACKUPS = 10
BACKUP_PREFIX = "state-"

# Keep in sync with the copies in docs/PROJECT_SPEC.md (enforced by tests/test_db.py).
_SCHEMA_V1 = """
-- Source folder, e.g. D:\\Bilder. All folder paths are relative to it, so a
-- changed drive letter only needs to be fixed here.
CREATE TABLE roots (
    id          INTEGER PRIMARY KEY,
    path        TEXT NOT NULL UNIQUE,
    scanned_at  TEXT                               -- last complete scan
) STRICT;

-- A photo on Flickr, identified by file content.
-- The same file in several folders = one photo, one upload.
CREATE TABLE photos (
    hash               TEXT NOT NULL PRIMARY KEY,  -- XXH3-128, hex
    state              TEXT NOT NULL CHECK (state IN
                         ('pending', 'uploading', 'uploaded', 'verified',
                          'unclear', 'failed')),
    origin             TEXT NOT NULL DEFAULT 'upload'
                         CHECK (origin IN ('upload', 'adopted')),  -- adopted = taken over
    flickr_id          TEXT UNIQUE,
    hash_tagged        INTEGER NOT NULL DEFAULT 0 CHECK (hash_tagged IN (0, 1)),
    attempts           INTEGER NOT NULL DEFAULT 0,
    upload_started_at  TEXT,
    verified_at        TEXT,
    last_error         TEXT,
    updated_at         TEXT NOT NULL,
    -- uploaded/verified always means the Flickr ID is known; before that it never is
    CHECK (
        (state IN ('uploaded', 'verified') AND flickr_id IS NOT NULL)
        OR (state IN ('pending', 'uploading', 'unclear') AND flickr_id IS NULL)
        OR state = 'failed'
    )
) STRICT;
CREATE INDEX idx_photos_state ON photos(state);

-- All albums: existing ones from Flickr and the ones planned by the user.
CREATE TABLE albums (
    id                  INTEGER PRIMARY KEY,
    title               TEXT NOT NULL,             -- not UNIQUE: Flickr allows duplicates
    description         TEXT,                      -- from Flickr, sent along when renaming
    pending_title       TEXT,                      -- rename requested, not yet confirmed
    title_confirmed     INTEGER NOT NULL DEFAULT 0 CHECK (title_confirmed IN (0, 1)),
    flickr_id           TEXT UNIQUE,               -- NULL while not on Flickr
    state               TEXT NOT NULL CHECK (state IN ('planned', 'creating', 'exists')),
    primary_photo_hash  TEXT REFERENCES photos(hash),  -- set before photosets.create
    created_by_tool     INTEGER NOT NULL DEFAULT 0 CHECK (created_by_tool IN (0, 1)),
    last_synced         TEXT,
    photo_count         INTEGER,                   -- from Flickr's album list, last sync
    video_count         INTEGER,
    updated_at          TEXT NOT NULL,
    CHECK ((state = 'exists') = (flickr_id IS NOT NULL)),
    CHECK (state != 'creating' OR primary_photo_hash IS NOT NULL)
) STRICT;

CREATE TABLE folders (
    id             INTEGER PRIMARY KEY,
    root_id        INTEGER NOT NULL REFERENCES roots(id),
    rel_path       TEXT NOT NULL,                  -- '' = the root itself, separator '/'
    album_id       INTEGER REFERENCES albums(id),  -- set = album folder
    included       INTEGER NOT NULL DEFAULT 0 CHECK (included IN (0, 1)),
    missing        INTEGER NOT NULL DEFAULT 0 CHECK (missing IN (0, 1)),  -- gone from disk
    prechecked_at  TEXT,                           -- last check against existing photos
    UNIQUE (root_id, rel_path)
) STRICT;
CREATE INDEX idx_folders_album ON folders(album_id);

-- The parent folder leads: no album folder above or below another one.
CREATE TRIGGER folders_no_nested_album_insert
BEFORE INSERT ON folders
WHEN NEW.album_id IS NOT NULL
BEGIN
    SELECT RAISE(ABORT, 'nested album folder')
    WHERE EXISTS (
        SELECT 1 FROM folders f
        WHERE f.root_id = NEW.root_id
          AND f.album_id IS NOT NULL
          AND (f.rel_path = '' OR NEW.rel_path = ''
               OR substr(NEW.rel_path, 1, length(f.rel_path) + 1) = f.rel_path || '/'
               OR substr(f.rel_path, 1, length(NEW.rel_path) + 1) = NEW.rel_path || '/')
    );
END;

CREATE TRIGGER folders_no_nested_album_update
BEFORE UPDATE OF album_id ON folders
WHEN NEW.album_id IS NOT NULL
BEGIN
    SELECT RAISE(ABORT, 'nested album folder')
    WHERE EXISTS (
        SELECT 1 FROM folders f
        WHERE f.root_id = NEW.root_id
          AND f.id != NEW.id
          AND f.album_id IS NOT NULL
          AND (f.rel_path = '' OR NEW.rel_path = ''
               OR substr(NEW.rel_path, 1, length(f.rel_path) + 1) = f.rel_path || '/'
               OR substr(f.rel_path, 1, length(NEW.rel_path) + 1) = NEW.rel_path || '/')
    );
END;

-- A local file. The upload state lives in photos, not here.
CREATE TABLE files (
    id          INTEGER PRIMARY KEY,
    folder_id   INTEGER NOT NULL REFERENCES folders(id),
    name        TEXT NOT NULL,
    size_bytes  INTEGER NOT NULL,
    mtime_ns    INTEGER NOT NULL,                  -- size/mtime changed -> hash again
    hash        TEXT REFERENCES photos(hash),      -- NULL until hashed
    status      TEXT NOT NULL CHECK (status IN
                  ('new', 'hashed', 'skipped_raw', 'skipped_too_large',
                   'skipped_unsupported', 'unreadable', 'missing')),
    taken_at    TEXT,                              -- EXIF DateTimeOriginal, local time
    width       INTEGER,
    height      INTEGER,
    last_error  TEXT,
    updated_at  TEXT NOT NULL,
    UNIQUE (folder_id, name),
    CHECK (status != 'hashed' OR hash IS NOT NULL)
) STRICT;
CREATE INDEX idx_files_hash ON files(hash);

-- Pre-check: possible matches among photos uploaded before this tool.
CREATE TABLE precheck_matches (
    file_id     INTEGER NOT NULL REFERENCES files(id),
    flickr_id   TEXT NOT NULL,
    confidence  TEXT NOT NULL CHECK (confidence IN ('certain', 'probable')),
    decision    TEXT NOT NULL DEFAULT 'open'
                  CHECK (decision IN ('open', 'same', 'different')),
    checked_at  TEXT NOT NULL,
    decided_at  TEXT,
    PRIMARY KEY (file_id, flickr_id)
) STRICT;

-- Which photo belongs to which album (n:m).
CREATE TABLE album_photos (
    album_id    INTEGER NOT NULL REFERENCES albums(id),
    photo_hash  TEXT NOT NULL REFERENCES photos(hash),
    state       TEXT NOT NULL CHECK (state IN ('pending', 'added', 'verified')),
    updated_at  TEXT NOT NULL,
    PRIMARY KEY (album_id, photo_hash)
) STRICT;
CREATE INDEX idx_album_photos_photo ON album_photos(photo_hash);

-- An upload or download job (one at a time); the UI follows it from here.
CREATE TABLE runs (
    id           INTEGER PRIMARY KEY,
    kind         TEXT NOT NULL CHECK (kind IN ('upload', 'download')),
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    status       TEXT NOT NULL
                   CHECK (status IN ('running', 'completed', 'aborted', 'crashed')),
    items_total  INTEGER,                          -- known once the work list is ready
    items_done   INTEGER NOT NULL DEFAULT 0,
    CHECK ((status = 'running') = (finished_at IS NULL))
) STRICT;

-- Live log and final report (the UI keeps no state of its own).
CREATE TABLE events (
    id          INTEGER PRIMARY KEY,
    run_id      INTEGER REFERENCES runs(id),
    ts          TEXT NOT NULL,
    level       TEXT NOT NULL CHECK (level IN ('info', 'warning', 'error')),
    file_id     INTEGER REFERENCES files(id),
    photo_hash  TEXT REFERENCES photos(hash),
    message     TEXT NOT NULL
) STRICT;
CREATE INDEX idx_events_run ON events(run_id);
"""

_SCHEMA_V2 = """
-- Settings of the user interface (key -> value as JSON). Which keys exist and
-- what they mean is defined in settings.py. Never credentials.
CREATE TABLE settings (
    key         TEXT NOT NULL PRIMARY KEY,
    value       TEXT NOT NULL,                     -- JSON
    updated_at  TEXT NOT NULL
) STRICT;
"""

# Schema version -> SQL that upgrades from the previous version.
_MIGRATIONS: dict[int, str] = {1: _SCHEMA_V1, 2: _SCHEMA_V2}


class DatabaseError(Exception):
    """Base class for all errors raised by this module."""


class AlreadyRunningError(DatabaseError):
    """Another instance of the application is using the database."""


class SchemaTooNewError(DatabaseError):
    """The database was written by a newer version of the application."""


class IntegrityCheckError(DatabaseError):
    """SQLite reports the database (or a backup) as damaged."""


class SQLiteTooOldError(DatabaseError):
    """The SQLite library bundled with Python lacks required features."""


class RecordNotFoundError(DatabaseError):
    """The row a state change refers to does not exist."""


class InvalidTransitionError(DatabaseError):
    """The requested state change is not allowed from the current state."""


def utc_now() -> str:
    """Return the current time as ISO-8601 string in UTC.

    Stored as text on purpose: the default datetime adapters of sqlite3 are
    deprecated since Python 3.12, and ISO strings in UTC sort chronologically.
    """
    return datetime.now(UTC).isoformat(timespec="milliseconds")


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Run a block of statements atomically: either all are committed or none.

    BEGIN IMMEDIATE takes the write lock up front, so two threads can't both
    read first and then fail when trying to upgrade to a write lock.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


@dataclass(frozen=True)
class StateMachine:
    """Allowed state transitions of one table, enforced by set_state()."""

    table: str
    key_columns: tuple[str, ...]
    state_column: str
    transitions: Mapping[str, frozenset[str]]
    # Columns set_state() may write together with the new state.
    columns: frozenset[str]
    # Column that receives the time of every state change, if any.
    touch_column: str | None = "updated_at"

    @property
    def states(self) -> frozenset[str]:
        return frozenset(self.transitions)

    def sources(self, target: str) -> frozenset[str]:
        """Return all states from which `target` may be reached."""
        return frozenset(state for state, targets in self.transitions.items() if target in targets)


PHOTOS = StateMachine(
    table="photos",
    key_columns=("hash",),
    state_column="state",
    transitions={
        # pending -> verified only happens when the user adopts an existing photo.
        "pending": frozenset({"uploading", "verified"}),
        # uploading -> pending only when nothing can have reached Flickr (file
        # not readable, cancelled before sending, login refused).
        "uploading": frozenset({"uploaded", "unclear", "failed", "pending"}),
        "uploaded": frozenset({"verified", "failed"}),
        "unclear": frozenset({"uploaded", "pending", "failed"}),
        "verified": frozenset(),
        "failed": frozenset({"pending"}),
    },
    columns=frozenset(
        {
            "origin",
            "flickr_id",
            "hash_tagged",
            "attempts",
            "upload_started_at",
            "verified_at",
            "last_error",
        }
    ),
)

ALBUMS = StateMachine(
    table="albums",
    key_columns=("id",),
    state_column="state",
    transitions={
        "planned": frozenset({"creating"}),
        "creating": frozenset({"exists", "planned"}),
        # exists -> planned: the album was deleted on Flickr.
        "exists": frozenset({"planned"}),
    },
    columns=frozenset({"flickr_id", "primary_photo_hash", "created_by_tool", "last_synced"}),
)

ALBUM_PHOTOS = StateMachine(
    table="album_photos",
    key_columns=("album_id", "photo_hash"),
    state_column="state",
    transitions={
        "pending": frozenset({"added"}),
        "added": frozenset({"verified", "pending"}),
        # verified -> pending: the album had to be created again.
        "verified": frozenset({"pending"}),
    },
    columns=frozenset(),
)

RUNS = StateMachine(
    table="runs",
    key_columns=("id",),
    state_column="status",
    transitions={
        "running": frozenset({"completed", "aborted", "crashed"}),
        "completed": frozenset(),
        "aborted": frozenset(),
        "crashed": frozenset(),
    },
    columns=frozenset({"finished_at"}),
    touch_column=None,
)


def set_state(
    conn: sqlite3.Connection,
    machine: StateMachine,
    key: object,
    new_state: str,
    **fields: object,
) -> None:
    """Move one row to `new_state`, if the state machine allows it.

    The check and the write are a single UPDATE statement, so two threads can
    never both perform the same transition. Extra columns (e.g. flickr_id) are
    written in the same statement, which keeps state and data consistent.

    Raises RecordNotFoundError or InvalidTransitionError, and lets
    sqlite3.IntegrityError through if the new row violates a CHECK constraint.
    """
    if new_state not in machine.states:
        raise ValueError(f"unknown {machine.table} state: {new_state!r}")
    unknown = set(fields) - machine.columns
    if unknown:
        # Column names end up in the SQL text, so only whitelisted ones are allowed.
        raise ValueError(f"set_state can't write {sorted(unknown)} on {machine.table}")
    key_values = key if isinstance(key, tuple) else (key,)
    if len(key_values) != len(machine.key_columns):
        raise ValueError(f"{machine.table} is keyed by {machine.key_columns}, got {key!r}")

    values = {machine.state_column: new_state, **fields}
    if machine.touch_column:
        values[machine.touch_column] = utc_now()
    where = " AND ".join(f"{column} = ?" for column in machine.key_columns)
    sources = sorted(machine.sources(new_state))

    if sources:
        assignments = ", ".join(f"{column} = ?" for column in values)
        placeholders = ", ".join(["?"] * len(sources))
        cursor = conn.execute(
            f"UPDATE {machine.table} SET {assignments} "
            f"WHERE {where} AND {machine.state_column} IN ({placeholders})",
            [*values.values(), *key_values, *sources],
        )
        if cursor.rowcount == 1:
            return

    row = conn.execute(
        f"SELECT {machine.state_column} FROM {machine.table} WHERE {where}", key_values
    ).fetchone()
    if row is None:
        raise RecordNotFoundError(f"{machine.table} {key_values!r} does not exist")
    raise InvalidTransitionError(
        f"{machine.table} {key_values!r}: {row[0]} -> {new_state} is not allowed"
    )


def log_event(
    conn: sqlite3.Connection,
    level: str,
    message: str,
    *,
    run_id: int | None = None,
    file_id: int | None = None,
    photo_hash: str | None = None,
) -> int:
    """Append an entry to the event log and return its id."""
    cursor = conn.execute(
        "INSERT INTO events (run_id, ts, level, file_id, photo_hash, message) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (run_id, utc_now(), level, file_id, photo_hash, message),
    )
    return cursor.lastrowid


@dataclass(frozen=True)
class RecoveryReport:
    """What recover_interrupted() found after the previous session ended."""

    unclear_photos: int
    crashed_runs: int
    albums_to_reconcile: int
    renames_to_retry: int

    @property
    def clean(self) -> bool:
        return not any(
            (
                self.unclear_photos,
                self.crashed_runs,
                self.albums_to_reconcile,
                self.renames_to_retry,
            )
        )


def recover_interrupted(conn: sqlite3.Connection) -> RecoveryReport:
    """Mark work interrupted by a crash, so nothing is retried blindly.

    Call once right after Database.open(), before any upload starts. The
    instance lock guarantees that no other process is working on the data.
    Albums in 'creating' and pending renames are only counted here: resolving
    them needs Flickr, which is the uploader's job.
    """
    now = utc_now()
    with transaction(conn):
        unclear = conn.execute(
            "UPDATE photos SET state = 'unclear', last_error = ?, updated_at = ? "
            "WHERE state = 'uploading'",
            ("interrupted while uploading", now),
        ).rowcount
        crashed = conn.execute(
            "UPDATE runs SET status = 'crashed', finished_at = ? WHERE status = 'running'",
            (now,),
        ).rowcount
        creating = conn.execute("SELECT count(*) FROM albums WHERE state = 'creating'").fetchone()
        renames = conn.execute(
            "SELECT count(*) FROM albums WHERE pending_title IS NOT NULL"
        ).fetchone()
        report = RecoveryReport(unclear, crashed, creating[0], renames[0])
        if not report.clean:
            log_event(
                conn,
                "warning",
                f"Recovery after an unexpected end: {report.unclear_photos} upload(s) unclear, "
                f"{report.crashed_runs} run(s) crashed, {report.albums_to_reconcile} album(s) "
                f"to reconcile, {report.renames_to_retry} rename(s) to retry",
            )
    return report


class Database:
    """The application database file, guarded by an instance lock."""

    def __init__(self, path: str | Path, *, backup_dir: str | Path | None = None) -> None:
        self.path = Path(path)
        self.backup_dir = Path(backup_dir) if backup_dir else self.path.parent / "backups"
        self._lock = _InstanceLock(self.path.with_name(self.path.name + ".lock"))
        self._is_open = False

    def open(self) -> None:
        """Lock the database, check its integrity and bring the schema up to date."""
        if self._is_open:
            return
        if sqlite3.sqlite_version_info < MIN_SQLITE_VERSION:
            raise SQLiteTooOldError(
                f"SQLite {sqlite3.sqlite_version} is too old, need "
                f"{'.'.join(map(str, MIN_SQLITE_VERSION))} or newer"
            )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock.acquire()
        try:
            self._prepare()
        except BaseException:
            self._lock.release()
            raise
        self._is_open = True

    def close(self) -> None:
        self._is_open = False
        self._lock.release()

    def __enter__(self) -> "Database":
        self.open()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        """Open a short-lived connection for the current thread.

        Connections run in autocommit mode: every statement outside
        transaction() is committed immediately. Use one connection per thread
        and unit of work; sqlite3 connections must not be shared across threads.
        """
        if not self._is_open:
            raise DatabaseError("call Database.open() before connecting")
        with self._connection() as conn:
            yield conn

    def backup(self, keep: int = DEFAULT_KEEP_BACKUPS) -> Path:
        """Copy the database into backup_dir, verify it and keep the newest `keep` copies."""
        if not self._is_open:
            raise DatabaseError("call Database.open() before creating a backup")
        return self._write_backup(keep)

    def _prepare(self) -> None:
        with self._connection() as conn:
            mode = conn.execute("PRAGMA journal_mode = WAL").fetchone()[0]
            if mode != "wal":
                raise DatabaseError(f"could not enable WAL mode (got {mode!r})")
            _check_integrity(conn, self.path)
            version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise SchemaTooNewError(
                f"{self.path} has schema version {version}, this program only knows "
                f"up to {SCHEMA_VERSION}; please update the program"
            )
        if version == SCHEMA_VERSION:
            return
        if version > 0:
            # An existing database is about to change shape: keep a way back.
            self._write_backup(DEFAULT_KEEP_BACKUPS)
        with self._connection() as conn:
            _migrate(conn, version)

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=BUSY_TIMEOUT_SECONDS, isolation_level=None)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            # FULL: a commit also survives a power loss, not just a program crash.
            conn.execute("PRAGMA synchronous = FULL")
            yield conn
        finally:
            conn.close()

    def _write_backup(self, keep: int) -> Path:
        if keep < 1:
            raise ValueError("keep must be at least 1")
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        moment = datetime.now(UTC)
        while True:
            target = self.backup_dir / f"{BACKUP_PREFIX}{moment:%Y%m%dT%H%M%S%fZ}.db"
            if not target.exists():
                break
            # Coarse clock: never overwrite an older backup, keep names in age order.
            moment += timedelta(microseconds=1)
        with (
            self._connection() as source,
            closing(sqlite3.connect(target, isolation_level=None)) as copy,
        ):
            source.backup(copy)
            # A backup should be a single self-contained file, not WAL + -shm.
            copy.execute("PRAGMA journal_mode = DELETE")
            result = copy.execute("PRAGMA quick_check").fetchone()[0]
        if result != "ok":
            target.unlink(missing_ok=True)
            raise IntegrityCheckError(f"backup {target} failed the integrity check: {result}")
        # Timestamps have a fixed width, so name order is age order.
        for old in sorted(self.backup_dir.glob(f"{BACKUP_PREFIX}*.db"))[:-keep]:
            old.unlink()
        return target


def _check_integrity(conn: sqlite3.Connection, path: Path) -> None:
    result = conn.execute("PRAGMA quick_check").fetchone()[0]
    if result != "ok":
        raise IntegrityCheckError(f"{path} is damaged ({result}); restore a backup")


def _migrate(conn: sqlite3.Connection, from_version: int) -> None:
    """Apply all migrations after `from_version`, each one atomically."""
    for version in range(from_version + 1, SCHEMA_VERSION + 1):
        # executescript() commits any open transaction first, so the
        # transaction has to be part of the script itself. DDL and
        # user_version are transactional in SQLite: a failing migration
        # leaves no trace.
        script = (
            f"BEGIN IMMEDIATE;\n{_MIGRATIONS[version]}\nPRAGMA user_version = {version};\nCOMMIT;"
        )
        try:
            conn.executescript(script)
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise


class _InstanceLock:
    """Exclusive lock on a file next to the database.

    The operating system releases the lock when the process ends, even after a
    crash, so a stale lock can't block the next start.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._file: IO[bytes] | None = None

    def acquire(self) -> None:
        # Kept open for as long as the lock is held, hence no `with`.
        handle = open(self.path, "a+b")
        try:
            _lock_file(handle)
        except OSError:
            handle.close()
            raise AlreadyRunningError(
                f"the database is already in use by another instance ({self.path})"
            ) from None
        self._file = handle

    def release(self) -> None:
        if self._file is None:
            return
        try:
            _unlock_file(self._file)
        finally:
            self._file.close()
            self._file = None


if sys.platform == "win32":
    import msvcrt

    def _lock_file(handle: IO[bytes]) -> None:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)

    def _unlock_file(handle: IO[bytes]) -> None:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _lock_file(handle: IO[bytes]) -> None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock_file(handle: IO[bytes]) -> None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
