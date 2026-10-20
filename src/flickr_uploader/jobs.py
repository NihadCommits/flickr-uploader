"""Background jobs: an upload or a download run, only one at a time.

Both kinds are recorded the same way, so the UI can follow either from the
database alone: a row in `runs` with progress counters, plus the event log.
One job at a time also keeps them from sharing the hourly API budget.
"""

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager

from flickr_uploader import db, hashing
from flickr_uploader.flickr_client import CancelledError, FlickrAuthError

UPLOAD, DOWNLOAD = "upload", "download"

_LOCK = threading.Lock()


class JobInProgressError(Exception):
    """An upload or download is already running."""


class StopJob(Exception):
    """Ends a job early: login rejected or Flickr out of reach."""


class Cancelled(Exception):
    """The user aborted the job."""


@contextmanager
def exclusive() -> Iterator[None]:
    """Hold the one job slot; raises JobInProgressError if it is taken."""
    if not _LOCK.acquire(blocking=False):
        raise JobInProgressError("an upload or download is already running")
    try:
        yield
    finally:
        _LOCK.release()


def start(conn: sqlite3.Connection, kind: str) -> int:
    """Record a new running job and return its run id."""
    return conn.execute(
        "INSERT INTO runs (kind, started_at, status) VALUES (?, ?, 'running')",
        (kind, db.utc_now()),
    ).lastrowid


def set_total(conn: sqlite3.Connection, run_id: int, total: int) -> None:
    conn.execute("UPDATE runs SET items_total = ? WHERE id = ?", (total, run_id))


def advance(conn: sqlite3.Connection, run_id: int) -> None:
    """One more item handled (done, skipped or failed alike)."""
    conn.execute("UPDATE runs SET items_done = items_done + 1 WHERE id = ?", (run_id,))


def finish(conn: sqlite3.Connection, run_id: int, status: str) -> None:
    db.set_state(conn, db.RUNS, run_id, status, finished_at=db.utc_now())


@contextmanager
def flickr_step() -> Iterator[None]:
    """Turn the client's job-ending errors into the job's own signals."""
    try:
        yield
    except FlickrAuthError as error:
        raise StopJob(f"Flickr rejected the login ({error})") from None
    except (CancelledError, hashing.HashingCancelledError):
        raise Cancelled from None
