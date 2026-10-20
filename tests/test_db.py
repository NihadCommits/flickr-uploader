"""Tests for the persistence layer (flickr_uploader.db)."""

import os
import re
import sqlite3
import subprocess
import sys
import textwrap
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

import pytest

from flickr_uploader import db

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = REPO_ROOT / "src"
SPEC = REPO_ROOT / "docs" / "PROJECT_SPEC.md"

HASH_A = "a" * 32
HASH_B = "b" * 32
HASH_C = "c" * 32


def add_photo(conn, photo_hash=HASH_A, state="pending", flickr_id=None):
    conn.execute(
        "INSERT INTO photos (hash, state, flickr_id, updated_at) VALUES (?, ?, ?, ?)",
        (photo_hash, state, flickr_id, db.utc_now()),
    )
    return photo_hash


def add_album(conn, title="2026-06 Portugal", state="planned", flickr_id=None, primary=None):
    return conn.execute(
        "INSERT INTO albums (title, state, flickr_id, primary_photo_hash, updated_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (title, state, flickr_id, primary, db.utc_now()),
    ).lastrowid


def add_root(conn, path=r"D:\Bilder"):
    return conn.execute("INSERT INTO roots (path) VALUES (?)", (path,)).lastrowid


def add_folder(conn, root_id, rel_path, album_id=None):
    return conn.execute(
        "INSERT INTO folders (root_id, rel_path, album_id) VALUES (?, ?, ?)",
        (root_id, rel_path, album_id),
    ).lastrowid


def photo_state(conn, photo_hash=HASH_A):
    return conn.execute("SELECT state FROM photos WHERE hash = ?", (photo_hash,)).fetchone()[0]


def run_python(code, *args):
    """Run code in a separate Python process that can import flickr_uploader."""
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(code), *args],
        env={**os.environ, "PYTHONPATH": str(SRC_DIR)},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


# --- Opening, schema and migrations -------------------------------------------


def test_open_creates_current_schema_in_wal_mode(conn):
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert conn.execute("PRAGMA synchronous").fetchone()[0] == 2  # FULL

    tables = conn.execute(
        "SELECT name, strict FROM pragma_table_list "
        "WHERE schema = 'main' AND type = 'table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    assert {row["name"] for row in tables} == {
        "roots",
        "photos",
        "albums",
        "folders",
        "files",
        "precheck_matches",
        "album_photos",
        "runs",
        "events",
        "settings",
    }
    assert all(row["strict"] for row in tables)


def test_reopening_keeps_data_and_does_not_migrate_again(tmp_path):
    path = tmp_path / "state.db"
    with db.Database(path) as database, database.connect() as conn:
        add_photo(conn)
    with db.Database(path) as database, database.connect() as conn:
        assert conn.execute("SELECT count(*) FROM photos").fetchone()[0] == 1
    assert not (tmp_path / "backups").exists()


def test_refuses_database_written_by_newer_program(tmp_path):
    path = tmp_path / "state.db"
    with closing(sqlite3.connect(path)) as raw:
        raw.execute(f"PRAGMA user_version = {db.SCHEMA_VERSION + 1}")

    with pytest.raises(db.SchemaTooNewError):
        db.Database(path).open()
    # Still SchemaTooNewError, not AlreadyRunningError: the lock was released.
    with pytest.raises(db.SchemaTooNewError):
        db.Database(path).open()


def test_migration_backs_up_the_old_version_first(tmp_path, monkeypatch):
    path = tmp_path / "state.db"
    with db.Database(path) as database, database.connect() as conn:
        add_photo(conn)

    monkeypatch.setattr(db, "SCHEMA_VERSION", db.SCHEMA_VERSION + 1)
    monkeypatch.setitem(
        db._MIGRATIONS, db.SCHEMA_VERSION, "ALTER TABLE photos ADD COLUMN note TEXT;"
    )
    with db.Database(path) as database, database.connect() as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(photos)")}
        assert "note" in columns

    (backup,) = (tmp_path / "backups").glob("state-*.db")
    with closing(sqlite3.connect(backup)) as copy:
        assert copy.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION - 1
        assert copy.execute("SELECT count(*) FROM photos").fetchone()[0] == 1


def test_version_1_database_is_migrated_to_2(tmp_path):
    """The first real migration: an existing v1 database gets the settings table."""
    path = tmp_path / "state.db"
    with closing(sqlite3.connect(path, isolation_level=None)) as raw:
        raw.executescript(f"BEGIN;{db._SCHEMA_V1}PRAGMA user_version = 1;COMMIT;")
        add_photo(raw)

    with db.Database(path) as database, database.connect() as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
        assert conn.execute("SELECT count(*) FROM photos").fetchone()[0] == 1
        conn.execute(
            "INSERT INTO settings (key, value, updated_at) VALUES ('a', '1', ?)", (db.utc_now(),)
        )
    (backup,) = (tmp_path / "backups").glob("state-*.db")
    with closing(sqlite3.connect(backup)) as copy:
        assert copy.execute("PRAGMA user_version").fetchone()[0] == 1


def test_failed_migration_leaves_no_trace(tmp_path, monkeypatch):
    path = tmp_path / "state.db"
    with db.Database(path) as database, database.connect() as conn:
        add_photo(conn)

    monkeypatch.setattr(db, "SCHEMA_VERSION", db.SCHEMA_VERSION + 1)
    monkeypatch.setitem(
        db._MIGRATIONS,
        db.SCHEMA_VERSION,
        "CREATE TABLE half_done (id INTEGER PRIMARY KEY) STRICT;\nTHIS IS NOT SQL;",
    )
    with pytest.raises(sqlite3.OperationalError):
        db.Database(path).open()

    with closing(sqlite3.connect(path)) as raw:
        assert raw.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION - 1
        leftovers = raw.execute("SELECT count(*) FROM sqlite_master WHERE name = 'half_done'")
        assert leftovers.fetchone()[0] == 0
        assert raw.execute("SELECT count(*) FROM photos").fetchone()[0] == 1


def test_connect_requires_open(tmp_path):
    with pytest.raises(db.DatabaseError), db.Database(tmp_path / "state.db").connect():
        pass


def test_schema_in_spec_matches_code(conn):
    """docs/PROJECT_SPEC.md shows a copy of the schema and its migrations; no drift."""

    def normalize(sql):
        return " ".join(re.sub(r"--[^\n]*", "", sql).split())

    def schema_objects(connection):
        rows = connection.execute("SELECT type, name, sql FROM sqlite_master WHERE sql NOT NULL")
        return {(row[0], row[1]): normalize(row[2]) for row in rows}

    blocks = re.findall(r"```sql\n(.*?)```", SPEC.read_text(encoding="utf-8"), re.S)
    spec_sql = "\n".join(blocks)
    with closing(sqlite3.connect(":memory:")) as spec_db:
        spec_db.executescript(spec_sql)
        assert schema_objects(spec_db) == schema_objects(conn)


# --- Instance lock ------------------------------------------------------------


def test_second_instance_in_same_process_is_rejected(database):
    with pytest.raises(db.AlreadyRunningError):
        db.Database(database.path).open()


def test_second_process_is_rejected(database):
    result = run_python(
        """
        import sys
        from flickr_uploader import db
        try:
            db.Database(sys.argv[1]).open()
        except db.AlreadyRunningError:
            sys.exit(3)
        """,
        str(database.path),
    )
    assert result.returncode == 3, result.stderr


def test_lock_is_released_on_close(tmp_path):
    path = tmp_path / "state.db"
    with db.Database(path):
        pass
    with db.Database(path):
        pass


# --- Crash safety -------------------------------------------------------------


def test_committed_changes_and_lock_survive_a_hard_crash(tmp_path):
    path = tmp_path / "state.db"
    result = run_python(
        """
        import os, sys
        from flickr_uploader import db
        database = db.Database(sys.argv[1])
        database.open()
        with database.connect() as conn:
            conn.execute(
                "INSERT INTO photos (hash, state, updated_at) VALUES (?, 'pending', ?)",
                (sys.argv[2], db.utc_now()),
            )
            db.set_state(conn, db.PHOTOS, sys.argv[2], "uploading")
            os._exit(9)  # crash: no close(), no cleanup, lock still held
        """,
        str(path),
        HASH_A,
    )
    assert result.returncode == 9, result.stderr

    # The OS freed the lock, and the committed "uploading" state is still there.
    with db.Database(path) as database, database.connect() as conn:
        report = db.recover_interrupted(conn)
        assert photo_state(conn) == "unclear"
    assert report.unclear_photos == 1


# --- State machines -----------------------------------------------------------


def test_set_state_writes_state_fields_and_timestamp(conn):
    add_photo(conn)
    db.set_state(conn, db.PHOTOS, HASH_A, "uploading", upload_started_at="2026-09-30T12:00Z")
    db.set_state(conn, db.PHOTOS, HASH_A, "uploaded", flickr_id="54321")

    row = conn.execute("SELECT * FROM photos").fetchone()
    assert row["state"] == "uploaded"
    assert row["flickr_id"] == "54321"
    assert row["upload_started_at"] == "2026-09-30T12:00Z"
    assert datetime.fromisoformat(row["updated_at"]).tzinfo is not None


def test_set_state_rejects_forbidden_transition(conn):
    add_photo(conn)
    with pytest.raises(db.InvalidTransitionError, match="pending -> uploaded"):
        db.set_state(conn, db.PHOTOS, HASH_A, "uploaded", flickr_id="1")
    assert photo_state(conn) == "pending"


def test_verified_is_final(conn):
    add_photo(conn, state="verified", flickr_id="1")
    with pytest.raises(db.InvalidTransitionError):
        db.set_state(conn, db.PHOTOS, HASH_A, "pending", flickr_id=None)


def test_set_state_on_missing_row(conn):
    with pytest.raises(db.RecordNotFoundError):
        db.set_state(conn, db.PHOTOS, HASH_A, "uploading")


def test_set_state_only_accepts_known_states_and_columns(conn):
    add_photo(conn)
    with pytest.raises(ValueError, match="unknown"):
        db.set_state(conn, db.PHOTOS, HASH_A, "done")
    with pytest.raises(ValueError, match="can't write"):
        db.set_state(conn, db.PHOTOS, HASH_A, "uploading", hash=HASH_B)
    with pytest.raises(ValueError, match="can't write"):
        db.set_state(conn, db.PHOTOS, HASH_A, "uploading", **{"state = 'verified' --": 1})
    assert photo_state(conn) == "pending"


def test_set_state_with_composite_key(conn):
    add_photo(conn)
    album = add_album(conn)
    conn.execute(
        "INSERT INTO album_photos VALUES (?, ?, 'pending', ?)", (album, HASH_A, db.utc_now())
    )
    db.set_state(conn, db.ALBUM_PHOTOS, (album, HASH_A), "added")
    state = conn.execute("SELECT state FROM album_photos").fetchone()[0]
    assert state == "added"
    with pytest.raises(ValueError, match="keyed by"):
        db.set_state(conn, db.ALBUM_PHOTOS, album, "verified")


def test_set_state_cannot_break_check_constraints(conn):
    add_photo(conn, state="uploading")
    with pytest.raises(sqlite3.IntegrityError):
        db.set_state(conn, db.PHOTOS, HASH_A, "uploaded")  # without flickr_id
    assert photo_state(conn) == "uploading"


@pytest.mark.parametrize(
    "machine",
    [db.PHOTOS, db.ALBUMS, db.ALBUM_PHOTOS, db.RUNS],
    ids=lambda machine: machine.table,
)
def test_state_machine_matches_check_constraint(conn, machine):
    sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (machine.table,)
    ).fetchone()[0]
    allowed = re.search(rf"CHECK \({machine.state_column} IN\s*\(([^)]*)\)\)", sql).group(1)
    assert set(re.findall(r"'(\w+)'", allowed)) == machine.states
    assert all(targets <= machine.states for targets in machine.transitions.values())


# --- Rules enforced by SQLite itself ------------------------------------------


@pytest.mark.parametrize(
    ("state", "flickr_id"),
    [
        ("uploaded", None),
        ("verified", None),
        ("pending", "1"),
        ("uploading", "1"),
        ("unclear", "1"),
    ],
)
def test_photo_state_and_flickr_id_must_match(conn, state, flickr_id):
    with pytest.raises(sqlite3.IntegrityError):
        add_photo(conn, state=state, flickr_id=flickr_id)


def test_album_rules(conn):
    with pytest.raises(sqlite3.IntegrityError):
        add_album(conn, state="exists")  # exists needs a Flickr ID
    with pytest.raises(sqlite3.IntegrityError):
        add_album(conn, state="planned", flickr_id="1")  # planned can't have one
    with pytest.raises(sqlite3.IntegrityError):
        add_album(conn, state="creating")  # creating needs its title photo


def test_run_has_finish_time_exactly_when_not_running(conn):
    run = conn.execute(
        "INSERT INTO runs (kind, started_at, status) VALUES ('upload', ?, 'running')",
        (db.utc_now(),),
    ).lastrowid
    with pytest.raises(sqlite3.IntegrityError):
        db.set_state(conn, db.RUNS, run, "completed")
    db.set_state(conn, db.RUNS, run, "completed", finished_at=db.utc_now())


def test_hashed_file_needs_a_hash(conn):
    folder = add_folder(conn, add_root(conn), "2026")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO files (folder_id, name, size_bytes, mtime_ns, status, updated_at) "
            "VALUES (?, 'a.jpg', 1, 1, 'hashed', ?)",
            (folder, db.utc_now()),
        )


def test_foreign_keys_are_enforced(conn):
    with pytest.raises(sqlite3.IntegrityError):
        add_folder(conn, root_id=999, rel_path="2026")


def test_column_types_are_enforced(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO roots (id, path) VALUES ('not a number', 'x')")


# --- The parent folder leads --------------------------------------------------


def test_album_folder_cannot_be_nested(conn):
    root = add_root(conn)
    album = add_album(conn)
    other = add_album(conn, "Andere")
    add_folder(conn, root, "2026/2026-06 Portugal", album)

    with pytest.raises(sqlite3.IntegrityError, match="nested album folder"):
        add_folder(conn, root, "2026/2026-06 Portugal/Jpegs", other)  # below
    with pytest.raises(sqlite3.IntegrityError, match="nested album folder"):
        add_folder(conn, root, "2026", other)  # above

    videos = add_folder(conn, root, "2026/2026-06 Portugal/Videos")  # no album: fine
    with pytest.raises(sqlite3.IntegrityError, match="nested album folder"):
        conn.execute("UPDATE folders SET album_id = ? WHERE id = ?", (other, videos))


def test_sibling_folders_with_the_same_prefix_are_not_nested(conn):
    root = add_root(conn)
    album = add_album(conn)
    for rel_path in ["2026/Portugal", "2026/Portugal 2", "2026/Portugal_Videos", "2026/Spanien"]:
        add_folder(conn, root, rel_path, album)


def test_root_as_album_folder_blocks_all_others(conn):
    root = add_root(conn)
    album = add_album(conn)
    add_folder(conn, root, "", album)
    with pytest.raises(sqlite3.IntegrityError, match="nested album folder"):
        add_folder(conn, root, "2026", album)


def test_album_folders_in_different_roots_are_independent(conn):
    album = add_album(conn)
    add_folder(conn, add_root(conn, r"D:\Bilder"), "", album)
    add_folder(conn, add_root(conn, r"E:\Backup"), "", album)


# --- Transactions -------------------------------------------------------------


def test_transaction_rolls_back_everything_on_error(conn):
    with pytest.raises(RuntimeError), db.transaction(conn):
        add_photo(conn, HASH_A)
        add_photo(conn, HASH_B)
        raise RuntimeError("boom")
    assert conn.execute("SELECT count(*) FROM photos").fetchone()[0] == 0
    assert not conn.in_transaction


def test_single_statements_are_committed_immediately(database, conn):
    add_photo(conn)
    with database.connect() as other:
        assert other.execute("SELECT count(*) FROM photos").fetchone()[0] == 1


# --- Recovery -----------------------------------------------------------------


def test_recover_interrupted_marks_and_reports(conn):
    add_photo(conn, HASH_A, "uploading")
    add_photo(conn, HASH_B, "pending")
    add_photo(conn, HASH_C, "verified", flickr_id="7")
    conn.execute(
        "INSERT INTO runs (kind, started_at, status) VALUES ('upload', ?, 'running')",
        (db.utc_now(),),
    )
    add_album(conn, "Neu", state="creating", primary=HASH_C)
    renamed = add_album(conn, "Alt", state="exists", flickr_id="99")
    conn.execute("UPDATE albums SET pending_title = 'Alt (neu)' WHERE id = ?", (renamed,))

    report = db.recover_interrupted(conn)

    assert report == db.RecoveryReport(
        unclear_photos=1, crashed_runs=1, albums_to_reconcile=1, renames_to_retry=1
    )
    states = {row[0]: row[1] for row in conn.execute("SELECT hash, state FROM photos")}
    assert states == {HASH_A: "unclear", HASH_B: "pending", HASH_C: "verified"}
    run = conn.execute("SELECT status, finished_at FROM runs").fetchone()
    assert run["status"] == "crashed"
    assert run["finished_at"] is not None
    assert conn.execute("SELECT level FROM events").fetchone()[0] == "warning"


def test_recover_on_clean_database_does_nothing(conn):
    assert db.recover_interrupted(conn).clean
    assert conn.execute("SELECT count(*) FROM events").fetchone()[0] == 0


def test_recovery_only_uses_allowed_transitions():
    assert "unclear" in db.PHOTOS.transitions["uploading"]
    assert "crashed" in db.RUNS.transitions["running"]


# --- Backups ------------------------------------------------------------------


def test_backup_is_a_verified_standalone_copy(database, conn):
    add_photo(conn)
    target = database.backup()
    assert target.parent == database.backup_dir
    with closing(sqlite3.connect(target)) as copy:
        assert copy.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        assert copy.execute("SELECT count(*) FROM photos").fetchone()[0] == 1


def test_backup_rotation_keeps_newest_and_ignores_other_files(database):
    database.backup_dir.mkdir(parents=True)
    unrelated = database.backup_dir / "notes.txt"
    unrelated.write_text("keep me", encoding="utf-8")

    created = [database.backup(keep=3) for _ in range(5)]

    assert sorted(database.backup_dir.glob("state-*.db")) == created[-3:]
    assert unrelated.exists()


# --- Helpers ------------------------------------------------------------------


def test_utc_now_is_iso_8601_in_utc():
    parsed = datetime.fromisoformat(db.utc_now())
    assert parsed.utcoffset().total_seconds() == 0
    assert abs((datetime.now(UTC) - parsed).total_seconds()) < 5


def test_log_event(conn):
    event_id = db.log_event(conn, "error", "upload failed", photo_hash=None)
    row = conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
    assert (row["level"], row["message"]) == ("error", "upload failed")
