"""The upload run: from ticked folders to verified photos in their albums.

One run at a time, normally in a background thread; the UI follows it by
reading runs and events from the database. A run:

1. backs up the database and marks what a crash interrupted
2. mirrors the Flickr album list (albums.sync_with_flickr)
3. runs the pre-check for ticked folders that lack one
4. handles every file directly in a ticked folder: hash, upload (or take over
   the old photo the user picked), verify by getInfo, put into the album,
   creating the album with its first photo
5. looks up uploads that got no answer, once Flickr's search index caught up
6. checks every album it touched against Flickr

The rules behind it (docs/PROJECT_SPEC.md):

- The intent is committed before every Flickr call with a side effect, and
  every id Flickr returns is committed at once.
- An upload without an answer becomes 'unclear' and is only repeated after a
  search for its hash tag found nothing, with the search index given time.
- A photo only counts as done once getInfo confirms it: own, tagged, private.
- Files with open pre-check matches, and albums waiting for the user's
  decision (yellow), are left out.
- A problem with one file is logged and the run goes on; a rejected login or a
  lasting loss of connection ends the run.
"""

import os
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from flickr_uploader import albums, config, db, hashing, jobs, precheck, scanner
from flickr_uploader.flickr_client import (
    CancelledError,
    FlickrAuthError,
    FlickrClientError,
    FlickrRejectedError,
    FlickrUnavailableError,
    OutcomeUnknownError,
)
from flickr_uploader.hashing import CancelFlag

# Connection trouble with this many files in a row ends the run. Each call was
# already retried by the client, so by then Flickr is out of reach.
MAX_CONNECTION_FAILURES = 3
SEARCH_INDEX_GRACE = timedelta(seconds=config.SEARCH_INDEX_GRACE_SECONDS)

_SKIPPED = {
    "skipped_raw": ("info", "RAW file, convert it first"),
    "skipped_too_large": ("warning", "too large for Flickr"),
    "skipped_unsupported": ("warning", "format not supported by Flickr"),
}


@dataclass
class RunReport:
    run_id: int | None = None
    status: str = "running"
    files: int = 0
    uploaded: int = 0
    adopted: int = 0  # old Flickr photos taken over instead of uploading
    already_done: int = 0  # verified before (earlier run or same content elsewhere)
    added_to_albums: int = 0
    albums_created: int = 0
    skipped: int = 0  # RAW, too large, unsupported
    waiting: int = 0  # for a decision, a pre-check or a new scan
    failed: int = 0
    unclear: int = 0  # still unclear at the end; looked up again next run


@dataclass(frozen=True)
class _File:
    id: int
    name: str
    path: str  # display path; config.filesystem_path() for access
    size_bytes: int
    mtime_ns: int
    hash: str | None
    status: str
    folder_id: int


def run_upload(
    database: db.Database,
    client,
    *,
    cancel: CancelFlag | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    sleep: Callable[[float], None] = time.sleep,
) -> RunReport:
    """Upload everything ticked that is not on Flickr yet and return the summary.

    Raises jobs.JobInProgressError if an upload or download is running.
    """
    with jobs.exclusive():
        database.backup()
        with database.connect() as conn:
            return _Run(conn, client, cancel, now, sleep).execute()


def reset_failed(conn: sqlite3.Connection, photo_hash: str) -> None:
    """The user wants another attempt at a failed photo (e.g. after deleting duplicates)."""
    db.set_state(conn, db.PHOTOS, photo_hash, "pending", flickr_id=None, last_error=None)


class _Run:
    def __init__(self, conn, client, cancel, now, sleep) -> None:
        self.conn, self.client, self.cancel = conn, client, cancel
        self.now, self.sleep = now, sleep
        self.report = RunReport()
        self.run_id: int | None = None
        self.lights: dict[int, str] = {}
        self.album_of_folder: dict[int, int | None] = {}
        self.reported_albums: set[int] = set()
        self.pairs: set[tuple[int, str]] = set()  # (album, photo) of files done this run
        self.deferred: list[_File] = []
        self.connection_failures = 0

    def execute(self) -> RunReport:
        db.recover_interrupted(self.conn)
        self.run_id = self.report.run_id = jobs.start(self.conn, jobs.UPLOAD)
        self.log("info", "Upload run started")
        status = "completed"
        try:
            self.prepare()
            work = self.files()
            jobs.set_total(self.conn, self.run_id, len(work))
            for file in work:
                self.check_cancel()
                self.report.files += 1
                self.handle(file)
                jobs.advance(self.conn, self.run_id)
            self.settle_unclear()
            self.finish_albums()
        except jobs.Cancelled:
            status = "aborted"
            self.log("warning", "Run aborted by the user")
        except jobs.StopJob as stop:
            status = "aborted"
            self.log("error", f"Run stopped: {stop}")
        except BaseException as error:
            self.log("error", f"Run crashed: {type(error).__name__}: {error}")
            self.end("crashed")
            raise
        self.end(status)
        return self.report

    def end(self, status: str) -> None:
        r = self.report
        r.status = status
        self.log(
            "info",
            f"Run {status}: {r.uploaded} uploaded, {r.adopted} taken over, {r.already_done} "
            f"already on Flickr, {r.added_to_albums} added to albums ({r.albums_created} new "
            f"albums), {r.waiting} waiting, {r.skipped} skipped, {r.failed} failed, "
            f"{r.unclear} unclear",
        )
        jobs.finish(self.conn, self.run_id, status)

    # --- Steps ------------------------------------------------------------------

    def prepare(self) -> None:
        try:
            with jobs.flickr_step():
                albums.sync_with_flickr(self.conn, self.client)
        except FlickrClientError as error:
            raise jobs.StopJob(f"the album list could not be read from Flickr ({error})") from None
        self.lights = albums.lights(self.conn)
        for folder in self.conn.execute(
            "SELECT d.id, d.rel_path, r.path FROM folders d JOIN roots r ON r.id = d.root_id "
            "WHERE d.included = 1 AND d.missing = 0 AND d.prechecked_at IS NULL"
        ).fetchall():
            self.check_cancel()
            try:
                with jobs.flickr_step():
                    precheck.precheck_folder(
                        self.conn, self.client, folder["id"], cancel=self.cancel
                    )
            except FlickrClientError as error:
                path = scanner.folder_path(folder["path"], folder["rel_path"])
                self.log("warning", f"Pre-check of {path} failed ({error}); its files wait")

    def files(self) -> list[_File]:
        rows = self.conn.execute(
            "SELECT f.id, f.name, f.size_bytes, f.mtime_ns, f.hash, f.status, f.folder_id, "
            "d.rel_path, r.path AS root_path "
            "FROM files f JOIN folders d ON d.id = f.folder_id JOIN roots r ON r.id = d.root_id "
            "WHERE d.included = 1 AND d.missing = 0 AND f.status != 'missing' "
            "ORDER BY r.path, d.rel_path, f.name"
        ).fetchall()
        return [
            _File(
                id=row["id"],
                name=row["name"],
                path=os.path.join(
                    scanner.folder_path(row["root_path"], row["rel_path"]), row["name"]
                ),
                size_bytes=row["size_bytes"],
                mtime_ns=row["mtime_ns"],
                hash=row["hash"],
                status=row["status"],
                folder_id=row["folder_id"],
            )
            for row in rows
        ]

    def handle(self, file: _File, *, final: bool = False) -> None:
        if file.status in _SKIPPED:
            level, reason = _SKIPPED[file.status]
            self.log(level, f"Skipped ({reason}): {file.path}", file_id=file.id)
            self.report.skipped += 1
            return
        album_id = self.album_of(file.folder_id)
        if album_id is None:
            self.wait(file, "not inside an album folder")
            return
        if self.lights.get(album_id) == albums.YELLOW:
            if album_id not in self.reported_albums:
                self.reported_albums.add(album_id)
                title = self.album(album_id)["title"]
                self.log(
                    "warning",
                    f"Album '{title}' waits for your decision (similar or duplicate titles "
                    "on Flickr); its files are not uploaded yet",
                )
            self.report.waiting += 1
            return
        if not precheck.is_cleared(self.conn, file.id):
            self.wait(file, "pre-check missing or a match waits for your decision")
            return

        failures = self.connection_failures
        try:
            with jobs.flickr_step():
                self.advance(file, album_id, final=final)
        except FlickrUnavailableError as error:
            self.log("warning", f"No connection to Flickr ({error}): {file.path}", file_id=file.id)
            self.connection_failed()
        except FlickrClientError as error:
            self.log("error", f"Flickr refused a step ({error}): {file.path}", file_id=file.id)
            self.report.failed += 1
        else:
            if self.connection_failures == failures:
                self.connection_failures = 0

    def advance(self, file: _File, album_id: int, *, final: bool) -> None:
        """Move one file's photo as far along its state machine as possible."""
        digest = self.digest(file)
        if digest is None:
            return
        self.conn.execute(
            "INSERT OR IGNORE INTO album_photos (album_id, photo_hash, state, updated_at) "
            "VALUES (?, ?, 'pending', ?)",
            (album_id, digest, db.utc_now()),
        )
        photo = self.photo(digest)
        state = photo["state"]
        if state == "verified":
            self.report.already_done += 1
        elif state == "failed":
            self.log(
                "warning",
                f"Not uploaded, an earlier attempt failed ({photo['last_error']}); reset it to "
                f"try again: {file.path}",
                file_id=file.id,
                photo_hash=digest,
            )
            self.report.failed += 1
            return
        if state == "unclear":
            state = self.look_up(digest)
        if state == "pending":
            chosen = precheck.chosen_flickr_photo(self.conn, file.id)
            state = self.adopt(file, digest, chosen) if chosen else self.upload(file, digest)
        if state == "uploaded":
            state = self.verify(file, digest)
        if state == "verified":
            self.pairs.add((album_id, digest))
            self.put_into_album(album_id, digest)
        elif state == "unclear":
            if final:
                self.report.unclear += 1
            else:
                self.deferred.append(file)
        elif state == "failed":
            self.report.failed += 1

    def digest(self, file: _File) -> str | None:
        """The file's hash, computed if needed; None if it can't be used now."""
        location = config.filesystem_path(file.path)
        try:
            info = os.stat(location)
        except OSError as error:
            self.unreadable(file, error)
            return None
        if (info.st_size, info.st_mtime_ns) != (file.size_bytes, file.mtime_ns):
            self.wait(file, "changed since the last scan, scan again")
            return None
        current = self.conn.execute(
            "SELECT hash, status FROM files WHERE id = ?", (file.id,)
        ).fetchone()
        if current["status"] == "hashed" and current["hash"]:
            return current["hash"]
        try:
            result = hashing.hash_file(location, cancel=self.cancel)
        except hashing.HashingCancelledError:
            raise jobs.Cancelled from None
        except hashing.FileChangedError:
            self.wait(file, "changed while it was read, scan again")
            return None
        except OSError as error:
            self.unreadable(file, error)
            return None
        if (result.size_bytes, result.mtime_ns) != (file.size_bytes, file.mtime_ns):
            self.wait(file, "changed since the last scan, scan again")
            return None
        now = db.utc_now()
        with db.transaction(self.conn):
            self.conn.execute(
                "INSERT OR IGNORE INTO photos (hash, state, updated_at) VALUES (?, 'pending', ?)",
                (result.digest, now),
            )
            self.conn.execute(
                "UPDATE files SET hash = ?, status = 'hashed', last_error = NULL, updated_at = ? "
                "WHERE id = ?",
                (result.digest, now, file.id),
            )
        return result.digest

    def upload(self, file: _File, digest: str) -> str:
        photo = self.photo(digest)
        db.set_state(
            self.conn,
            db.PHOTOS,
            digest,
            "uploading",
            attempts=photo["attempts"] + 1,
            upload_started_at=db.utc_now(),
            last_error=None,
        )
        try:
            flickr_id = self.client.upload(
                file.path, title=os.path.splitext(file.name)[0], digest=digest, cancel=self.cancel
            )
        except OutcomeUnknownError as error:
            db.set_state(self.conn, db.PHOTOS, digest, "unclear", last_error=str(error))
            self.log(
                "warning",
                f"Upload got no answer; it is looked up on Flickr before another attempt: "
                f"{file.path}",
                file_id=file.id,
                photo_hash=digest,
            )
            self.connection_failed()
            return "unclear"
        except FlickrRejectedError as error:
            db.set_state(self.conn, db.PHOTOS, digest, "failed", last_error=str(error))
            self.log("error", f"Flickr refused the upload ({error}): {file.path}", file_id=file.id)
            return "failed"
        except (FlickrAuthError, CancelledError, OSError) as error:
            # Nothing reached Flickr: login refused, cancelled while waiting
            # for the rate limit, or the file could not be opened.
            db.set_state(self.conn, db.PHOTOS, digest, "pending", last_error=_describe(error))
            if isinstance(error, OSError):
                self.unreadable(file, error)
                return "not readable"
            raise
        db.set_state(self.conn, db.PHOTOS, digest, "uploaded", flickr_id=flickr_id)
        self.report.uploaded += 1
        self.log("info", f"Uploaded: {file.path}", file_id=file.id, photo_hash=digest)
        return "uploaded"

    def look_up(self, digest: str) -> str:
        """Settle an upload that got no answer: did it arrive on Flickr?"""
        hits = self.client.find_by_hash(digest)
        if len(hits) == 1:
            db.set_state(
                self.conn, db.PHOTOS, digest, "uploaded", flickr_id=hits[0], last_error=None
            )
            self.report.uploaded += 1
            self.log("info", "An upload without answer did arrive on Flickr", photo_hash=digest)
            return "uploaded"
        if hits:
            ids = ", ".join(hits)
            db.set_state(
                self.conn,
                db.PHOTOS,
                digest,
                "failed",
                last_error=f"on Flickr {len(hits)} times: {ids}",
            )
            self.log(
                "error",
                f"Photo is on Flickr {len(hits)} times ({ids}); delete the extra copies on "
                "Flickr, then reset it",
                photo_hash=digest,
            )
            return "failed"
        started = self.photo(digest)["upload_started_at"]
        # The search index lags behind: only after a while does "not found" mean "not there".
        if started is None or self.now() - datetime.fromisoformat(started) >= SEARCH_INDEX_GRACE:
            db.set_state(self.conn, db.PHOTOS, digest, "pending", last_error=None)
            return "pending"
        return "unclear"

    def verify(self, file: _File, digest: str) -> str:
        flickr_id = self.photo(digest)["flickr_id"]
        info = self.client.photo_info(flickr_id)
        if not self.client.is_own_photo(info, digest):
            reason = "missing on Flickr" if info is None else "not own or without its hash tag"
            db.set_state(self.conn, db.PHOTOS, digest, "failed", last_error=reason)
            self.log("error", f"Upload {flickr_id} {reason}: {file.path}", file_id=file.id)
            return "failed"
        if not info.is_private:
            self.client.make_private(flickr_id)
            info = self.client.photo_info(flickr_id)
            if info is None or not info.is_private:
                db.set_state(
                    self.conn, db.PHOTOS, digest, "failed", last_error="could not be made private"
                )
                self.log(
                    "error",
                    f"Photo {flickr_id} is NOT private on Flickr and could not be made private; "
                    f"please check it on Flickr: {file.path}",
                    file_id=file.id,
                    photo_hash=digest,
                )
                return "failed"
            self.log(
                "warning",
                f"Photo {flickr_id} did not arrive private and was made private: {file.path}",
                file_id=file.id,
            )
        db.set_state(
            self.conn,
            db.PHOTOS,
            digest,
            "verified",
            verified_at=db.utc_now(),
            hash_tagged=1,
            last_error=None,
        )
        return "verified"

    def adopt(self, file: _File, digest: str, flickr_id: str) -> str:
        """Take over the old Flickr photo the user said this file is."""
        owner = self.conn.execute(
            "SELECT hash FROM photos WHERE flickr_id = ?", (flickr_id,)
        ).fetchone()
        if owner is not None and owner["hash"] != digest:
            self.log(
                "error",
                f"Flickr photo {flickr_id} already belongs to other file content; check the "
                f"pre-check decision: {file.path}",
                file_id=file.id,
            )
            self.report.failed += 1
            return "conflict"
        # Repeatable without harm, so no intent is stored before it. The
        # photo's visibility stays as it is: the tool never changes it.
        self.client.add_hash_tag(flickr_id, digest)
        db.set_state(
            self.conn,
            db.PHOTOS,
            digest,
            "verified",
            origin="adopted",
            flickr_id=flickr_id,
            hash_tagged=1,
            verified_at=db.utc_now(),
            last_error=None,
        )
        self.report.adopted += 1
        self.log(
            "info",
            f"Taken over photo {flickr_id} already on Flickr: {file.path}",
            file_id=file.id,
            photo_hash=digest,
        )
        return "verified"

    def put_into_album(self, album_id: int, digest: str) -> None:
        pair = self.conn.execute(
            "SELECT state FROM album_photos WHERE album_id = ? AND photo_hash = ?",
            (album_id, digest),
        ).fetchone()
        if pair["state"] != "pending":
            return
        album = self.album(album_id)
        flickr_album = album["flickr_id"]
        if album["state"] == "creating":
            return  # an earlier attempt got no answer; settled at the end of the run
        if album["state"] == "planned":
            flickr_album = albums.create_on_flickr(self.conn, self.client, album_id, digest)
            if flickr_album is None:
                return
            self.report.albums_created += 1
            self.lights[album_id] = albums.GREEN
        # Also for the cover: "already in the album" counts as success.
        self.client.add_to_album(flickr_album, self.photo(digest)["flickr_id"])
        db.set_state(self.conn, db.ALBUM_PHOTOS, (album_id, digest), "added")
        self.report.added_to_albums += 1

    def settle_unclear(self) -> None:
        """Look up uploads without answer again, once the search index had its time."""
        if not self.deferred:
            return
        started = [
            datetime.fromisoformat(self.photo(self.file_hash(file))["upload_started_at"])
            for file in self.deferred
        ]
        remaining = (max(started) + SEARCH_INDEX_GRACE - self.now()).total_seconds()
        if remaining > 0:
            self.log(
                "info",
                f"Waiting {remaining / 60:.0f} min for Flickr's search index, to look up "
                f"{len(self.deferred)} upload(s) without answer",
            )
            while remaining > 0:
                self.check_cancel()
                step = min(1.0, remaining)
                self.sleep(step)
                remaining -= step
        for file in self.deferred:
            self.check_cancel()
            self.handle(file, final=True)

    def finish_albums(self) -> None:
        """Add what is still missing, then check each album against Flickr."""
        if self.conn.execute("SELECT 1 FROM albums WHERE state = 'creating'").fetchone():
            # Settles album creations that got no answer.
            try:
                with jobs.flickr_step():
                    albums.sync_with_flickr(self.conn, self.client)
            except FlickrClientError as error:
                self.log("warning", f"Album list not read again ({error}); settled next run")
        for album_id, digest in sorted(self.pairs):
            self.check_cancel()
            try:
                with jobs.flickr_step():
                    self.put_into_album(album_id, digest)
            except FlickrClientError as error:
                self.log("warning", f"Not added to its album yet ({error})", photo_hash=digest)
        for album_id in sorted({album_id for album_id, _ in self.pairs}):
            self.check_cancel()
            album = self.album(album_id)
            if album["state"] != "exists":
                continue
            try:
                with jobs.flickr_step():
                    on_flickr = self.client.album_photo_ids(album["flickr_id"])
            except FlickrClientError as error:
                self.log("warning", f"Album '{album['title']}' not checked ({error})")
                continue
            self.check_album(album_id, album["title"], on_flickr)

    def check_album(self, album_id: int, title: str, on_flickr: set[str]) -> None:
        added = self.conn.execute(
            "SELECT ap.photo_hash, p.flickr_id FROM album_photos ap "
            "JOIN photos p ON p.hash = ap.photo_hash WHERE ap.album_id = ? AND ap.state = 'added'",
            (album_id,),
        ).fetchall()
        missing = 0
        with db.transaction(self.conn):
            for row in added:
                if row["flickr_id"] in on_flickr:
                    db.set_state(
                        self.conn, db.ALBUM_PHOTOS, (album_id, row["photo_hash"]), "verified"
                    )
                else:
                    db.set_state(
                        self.conn, db.ALBUM_PHOTOS, (album_id, row["photo_hash"]), "pending"
                    )
                    missing += 1
        if missing:
            self.log(
                "warning",
                f"{missing} photo(s) not found in album '{title}' on Flickr; added again next run",
            )

    # --- Helpers ----------------------------------------------------------------

    def album_of(self, folder_id: int) -> int | None:
        if folder_id not in self.album_of_folder:
            row = albums.album_folder(self.conn, folder_id)
            self.album_of_folder[folder_id] = row["album_id"] if row else None
        return self.album_of_folder[folder_id]

    def album(self, album_id: int) -> sqlite3.Row:
        return self.conn.execute(
            "SELECT title, state, flickr_id FROM albums WHERE id = ?", (album_id,)
        ).fetchone()

    def photo(self, digest: str) -> sqlite3.Row:
        return self.conn.execute("SELECT * FROM photos WHERE hash = ?", (digest,)).fetchone()

    def file_hash(self, file: _File) -> str:
        return self.conn.execute("SELECT hash FROM files WHERE id = ?", (file.id,)).fetchone()[0]

    def wait(self, file: _File, reason: str) -> None:
        self.log("info", f"Waiting ({reason}): {file.path}", file_id=file.id)
        self.report.waiting += 1

    def unreadable(self, file: _File, error: OSError) -> None:
        message = f"not readable: {_describe(error)}"
        self.conn.execute(
            "UPDATE files SET status = 'unreadable', last_error = ?, updated_at = ? WHERE id = ?",
            (message, db.utc_now(), file.id),
        )
        self.log("warning", f"File {message}: {file.path}", file_id=file.id)
        self.report.failed += 1

    def connection_failed(self) -> None:
        self.connection_failures += 1
        if self.connection_failures >= MAX_CONNECTION_FAILURES:
            raise jobs.StopJob(
                f"no connection to Flickr for {self.connection_failures} files in a row"
            )

    def check_cancel(self) -> None:
        if self.cancel is not None and self.cancel.is_set():
            raise jobs.Cancelled

    def log(self, level: str, message: str, **refs) -> None:
        db.log_event(self.conn, level, message, run_id=self.run_id, **refs)


def _describe(error: BaseException) -> str:
    # OSError's text repeats the path in its \\?\ form; the strerror is enough.
    if isinstance(error, OSError) and error.strerror:
        return error.strerror
    return str(error) or type(error).__name__
