"""Download own Flickr albums to the PC: videos, photos or everything.

The way back from Flickr (spec, "Download"). Every chosen album has its own
filter and lands in its own subfolder of the chosen target, named after the
album. Originals are taken where Flickr offers them, else the largest version.

Nothing changes on Flickr, so the safeguards of the upload are not needed.
What is kept:

- Never into or around the photo archive: the target may neither lie inside
  a root nor contain one.
- Nothing is overwritten or deleted: files already there are skipped, and a
  download goes to "<name>.part" and is only renamed once complete.
- Only images and videos are kept, never an error page.
- The recording date is only filled in where a file has none (filedates).
- No file URLs in the log: they contain the photo's secret.

Like an upload, a download is a job in `runs` with its log in `events`. The
files themselves are not listed in the database: the target folder is the
record of what is there.
"""

import os
import re
import sqlite3
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from flickr_uploader import config, db, filedates, jobs, scanner
from flickr_uploader.flickr_client import (
    FlickrClientError,
    FlickrUnavailableError,
    MediaItem,
)
from flickr_uploader.hashing import CancelFlag

VIDEOS, PHOTOS, ALL = "videos", "photos", "all"
MEDIA_FILTERS = (VIDEOS, PHOTOS, ALL)
# Connection trouble with this many files in a row ends the download.
MAX_CONNECTION_FAILURES = 3
MAX_NAME_LENGTH = 150

_ORIGINAL = {"photo": "Original", "video": "Video Original"}
_PLAYER = "Video Player"  # the embeddable player, not a file
_KNOWN_EXTENSIONS = config.PHOTO_EXTENSIONS | config.VIDEO_EXTENSIONS | {".m4v", ".mkv", ".webm"}
_CONTENT_TYPES = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "video/mp4": ".mp4",
    "video/quicktime": ".mov",
}
_FORBIDDEN_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"} | {f"{p}{i}" for p in ("COM", "LPT") for i in range(1, 10)}
)


class TargetError(Exception):
    """The target folder can't be used for downloads."""


class _FileError(Exception):
    """This one file could not be saved; logged, then the next one."""


@dataclass(frozen=True)
class AlbumChoice:
    album_id: int  # albums.id in the database
    media: str = ALL  # VIDEOS, PHOTOS or ALL


@dataclass
class DownloadReport:
    run_id: int | None = None
    status: str = "running"
    albums: int = 0
    files: int = 0
    downloaded: int = 0
    existing: int = 0  # already in the target folder, skipped
    not_original: int = 0  # Flickr offered no original: largest version taken
    dated: int = 0  # got the recording date
    undated: int = 0  # Flickr doesn't know when it was taken
    failed: int = 0


@dataclass(frozen=True)
class _Album:
    title: str
    folder: Path
    folder_name: str
    items: list[tuple[MediaItem, str]]  # (item, file name without extension)


@dataclass(frozen=True)
class _Source:
    label: str
    url: str
    original: bool


def check_target(conn: sqlite3.Connection, target: str | os.PathLike) -> Path:
    """The target folder as absolute path; TargetError if it is in or around the archive.

    Writing there would put files into the photo archive, and the next scan
    would find them as new photos (with a changed hash, if a date was written).
    """
    absolute = os.path.abspath(target)
    for (root,) in conn.execute("SELECT path FROM roots"):
        if scanner.overlaps(absolute, root):
            raise TargetError(
                f"{absolute} lies in or around the photo archive ({root}); "
                "choose a folder outside it"
            )
    return Path(absolute)


def run_download(
    database: db.Database,
    client,
    choices: list[AlbumChoice],
    target: str | os.PathLike,
    *,
    cancel: CancelFlag | None = None,
) -> DownloadReport:
    """Download the chosen albums, each into its own subfolder of `target`.

    Raises TargetError or ValueError before anything starts, and
    jobs.JobInProgressError if an upload or download is running.
    """
    with jobs.exclusive(), database.connect() as conn:
        plan = validate_choices(conn, choices)
        return _Run(conn, client, cancel).execute(plan, check_target(conn, target))


def validate_choices(
    conn: sqlite3.Connection, choices: list[AlbumChoice]
) -> list[tuple[sqlite3.Row, str]]:
    """(album row, filter) per choice; ValueError for anything that can't be downloaded."""
    if not choices:
        raise ValueError("choose at least one album")
    if len({choice.album_id for choice in choices}) != len(choices):
        raise ValueError("an album was chosen twice")
    plan = []
    for choice in choices:
        if choice.media not in MEDIA_FILTERS:
            raise ValueError(f"media must be one of {MEDIA_FILTERS}")
        album = conn.execute(
            "SELECT id, title, flickr_id FROM albums WHERE id = ? AND state = 'exists'",
            (choice.album_id,),
        ).fetchone()
        if album is None:
            raise ValueError(f"album {choice.album_id} is not on Flickr")
        plan.append((album, choice.media))
    return plan


class _Run:
    def __init__(self, conn, client, cancel) -> None:
        self.conn, self.client, self.cancel = conn, client, cancel
        self.report = DownloadReport()
        self.run_id: int | None = None
        self.connection_failures = 0

    def execute(self, plan: list[tuple[sqlite3.Row, str]], target: Path) -> DownloadReport:
        db.recover_interrupted(self.conn)
        self.run_id = self.report.run_id = jobs.start(self.conn, jobs.DOWNLOAD)
        self.log("info", f"Download started: {len(plan)} album(s) to {target}")
        status = "completed"
        try:
            work = self.list_albums(plan, target)
            jobs.set_total(self.conn, self.run_id, sum(len(album.items) for album in work))
            for album in work:
                self.download_album(album)
        except jobs.Cancelled:
            status = "aborted"
            self.log("warning", "Download aborted by the user")
        except jobs.StopJob as stop:
            status = "aborted"
            self.log("error", f"Download stopped: {stop}")
        except BaseException as error:
            self.log("error", f"Download crashed: {type(error).__name__}: {error}")
            self.end("crashed")
            raise
        self.end(status)
        return self.report

    def end(self, status: str) -> None:
        r = self.report
        r.status = status
        self.log(
            "info",
            f"Download {status}: {r.downloaded} downloaded, {r.existing} already there, "
            f"{r.not_original} without original, {r.dated} dated, {r.undated} without date "
            f"on Flickr, {r.failed} failed",
        )
        jobs.finish(self.conn, self.run_id, status)

    # --- Steps ------------------------------------------------------------------

    def list_albums(self, plan, target: Path) -> list[_Album]:
        work = []
        for album, media in plan:
            self.check_cancel()
            try:
                with jobs.flickr_step():
                    items = self.client.album_media(album["flickr_id"], media)
            except FlickrUnavailableError as error:
                raise jobs.StopJob(f"Flickr not reachable ({error})") from None
            except FlickrClientError as error:
                self.log("error", f"Album '{album['title']}' could not be read ({error})")
                continue
            name = self.folder_name(album)
            work.append(_Album(album["title"], target / name, name, list(_named(items))))
            self.report.albums += 1
            self.log("info", f"Album '{album['title']}': {len(items)} file(s) ({media})")
        return work

    def download_album(self, album: _Album) -> None:
        if not album.items:
            return
        os.makedirs(config.filesystem_path(album.folder), exist_ok=True)
        present = _present_files(album.folder)
        for item, stem in album.items:
            self.check_cancel()
            self.report.files += 1
            self.handle(album, item, stem, present)
            jobs.advance(self.conn, self.run_id)

    def handle(self, album: _Album, item: MediaItem, stem: str, present: dict[str, str]) -> None:
        failures = self.connection_failures
        try:
            with jobs.flickr_step():
                existing = present.get(stem.casefold())
                if existing is None:
                    path = self.fetch(album, item, stem)
                    present[stem.casefold()] = path.name
                else:
                    path = album.folder / existing
                    self.report.existing += 1
                self.date(album, item, path, new=existing is None)
        except FlickrUnavailableError as error:
            self.log("warning", f"No connection to Flickr ({error}): {album.folder_name}/{stem}")
            self.connection_failed()
        except (FlickrClientError, _FileError, OSError) as error:
            self.log("error", f"Not downloaded ({_describe(error)}): {album.folder_name}/{stem}")
            self.report.failed += 1
        else:
            if self.connection_failures == failures:
                self.connection_failures = 0

    def fetch(self, album: _Album, item: MediaItem, stem: str) -> Path:
        source = self.source(item)
        with self.client.open_download(source.url) as download:
            kind = download.content_type
            if not (kind.startswith(("image/", "video/")) or kind == "application/octet-stream"):
                raise _FileError(f"no image or video received ({kind or 'no type'})")
            final = album.folder / (stem + _extension(download, item))
            partial = final.with_name(final.name + ".part")
            written = 0
            with open(config.filesystem_path(partial), "wb") as file:
                for chunk in download.chunks():
                    self.check_cancel()  # the .part stays and is loaded anew next time
                    file.write(chunk)
                    written += len(chunk)
            if download.length is not None and written != download.length:
                raise _FileError(f"incomplete, {written} of {download.length} bytes")
        if os.path.exists(config.filesystem_path(final)):
            raise _FileError(f"{final.name} appeared meanwhile; it is left as it is")
        # os.rename, unlike os.replace, never overwrites on Windows.
        os.rename(config.filesystem_path(partial), config.filesystem_path(final))
        self.report.downloaded += 1
        note = source.label
        if not source.original:
            self.report.not_original += 1
            note += ", no original on Flickr"
        self.log("info", f"Downloaded ({note}): {album.folder_name}/{final.name}")
        return final

    def source(self, item: MediaItem) -> _Source:
        """The original if Flickr offers it, else the largest version."""
        if item.media == "photo" and item.original_url:
            return _Source(_ORIGINAL["photo"], item.original_url, True)
        sizes = [
            size
            for size in self.client.media_sizes(item.id)
            if size.media == item.media and size.label != _PLAYER
        ]
        if not sizes:
            raise _FileError("Flickr offers no file for it")
        for size in sizes:
            if size.label == _ORIGINAL.get(item.media):
                return _Source(size.label, size.url, True)
        largest = max(sizes, key=lambda size: (size.width or 0) * (size.height or 0))
        return _Source(largest.label, largest.url, False)

    def date(self, album: _Album, item: MediaItem, path: Path, *, new: bool) -> None:
        when = filedates.taken_time(item.taken)
        if when is None:
            self.report.undated += 1
            return
        filled = filedates.apply_date(path, when)
        self.report.dated += 1
        if filled and not new:
            self.log("info", f"Recording date added: {album.folder_name}/{path.name}")

    # --- Helpers ----------------------------------------------------------------

    def folder_name(self, album: sqlite3.Row) -> str:
        """The album's subfolder; the Flickr id is added if another album has the same name."""
        name = _safe_name(album["title"]) or album["flickr_id"]
        for (other,) in self.conn.execute(
            "SELECT title FROM albums WHERE state = 'exists' AND id != ?", (album["id"],)
        ):
            if (_safe_name(other) or "").casefold() == name.casefold():
                return f"{name} ({album['flickr_id']})"
        return name

    def connection_failed(self) -> None:
        self.connection_failures += 1
        if self.connection_failures >= MAX_CONNECTION_FAILURES:
            raise jobs.StopJob(
                f"no connection to Flickr for {self.connection_failures} files in a row"
            )

    def check_cancel(self) -> None:
        if self.cancel is not None and self.cancel.is_set():
            raise jobs.Cancelled

    def log(self, level: str, message: str) -> None:
        db.log_event(self.conn, level, message, run_id=self.run_id)


def _named(items: list[MediaItem]):
    """(item, file name without extension); titles used twice get the Flickr id."""
    names = {item.id: _safe_name(item.title) for item in items}
    # Windows file names ignore case, so "Clip" and "clip" would collide.
    counts = Counter(name.casefold() for name in names.values() if name)
    for item in items:
        name = names[item.id]
        if not name:
            yield item, item.id
        elif counts[name.casefold()] > 1:
            yield item, f"{name} ({item.id})"
        else:
            yield item, name


def _present_files(folder: Path) -> dict[str, str]:
    """Finished files in a folder: name without extension (case-folded) -> file name."""
    present = {}
    for name in os.listdir(config.filesystem_path(folder)):
        stem, extension = os.path.splitext(name)
        if extension.lower() != ".part":
            present[stem.casefold()] = name
    return present


def _safe_name(title: str) -> str:
    """A title usable as a Windows file or folder name; empty if nothing usable is left."""
    name = _FORBIDDEN_CHARS.sub("_", title).strip().rstrip(". ")
    suffix = os.path.splitext(name)[1].lower()
    if suffix in config.PHOTO_EXTENSIONS | config.VIDEO_EXTENSIONS:  # a title like "x.jpg"
        name = name[: -len(suffix)].rstrip(". ")
    if name.split(".")[0].upper() in _RESERVED_NAMES:
        name = "_" + name
    return name[:MAX_NAME_LENGTH].rstrip(". ")


def _extension(download, item: MediaItem) -> str:
    if download.extension in _KNOWN_EXTENSIONS:
        return download.extension
    if download.content_type in _CONTENT_TYPES:
        return _CONTENT_TYPES[download.content_type]
    if item.media == "photo" and item.original_format:
        return "." + item.original_format.lower()
    return ".mp4" if item.media == "video" else ".jpg"


def _describe(error: BaseException) -> str:
    # OSError's text repeats the path in its \\?\ form; the strerror is enough.
    if isinstance(error, OSError) and error.strerror:
        return error.strerror
    return str(error) or type(error).__name__
