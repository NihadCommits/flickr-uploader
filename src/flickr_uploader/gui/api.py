"""The Python side of the window: everything the web frontend may call.

pywebview exposes the public methods of Api as window.pywebview.api.<name>()
and runs every call in a thread of its own, so each method opens its own short
database connection. The frontend keeps no state: it renders what these
methods return, and every action is written to the database at once (spec,
section "GUI").

Expected problems come back as {"error": "<text for the user>"} instead of
an exception, so the frontend can show them as they are. Attributes and
helpers start with "_": pywebview would otherwise expose them to the page.
"""

import dataclasses
import importlib.metadata
import os
import sqlite3
import threading
import time
import webbrowser
from collections.abc import Callable, Mapping
from datetime import UTC, datetime

from flickr_uploader import (
    albums,
    auth,
    config,
    db,
    downloader,
    jobs,
    precheck,
    scanner,
    settings,
    uploader,
)
from flickr_uploader.flickr_client import FlickrClient, FlickrClientError, RateLimiter

_ELIGIBLE = ("new", "hashed", "unreadable")
LOG_LINES = 150  # events shown in the log of a run
TARGET_IN_ARCHIVE = (
    "Dieser Ordner liegt in oder über einem Ordner, aus dem du hochlädst (deinem "
    "Foto-Archiv). Downloads kommen nie dorthin. Bitte einen Ordner außerhalb wählen, "
    "z. B. „Downloads“."
)
MATCH_REASONS = {
    "certain": "gleicher Aufnahmezeitpunkt, gleicher Name und gleiche Größe",
    "probable": "gleicher Aufnahmezeitpunkt und gleicher Name oder gleiche Größe",
}
SETUP_COMMAND = ".venv\\Scripts\\python.exe -m flickr_uploader.auth setup"
# What the page may change directly; the download folder only comes from the dialog.
EDITABLE_SETTINGS = (settings.DOWNLOAD_MEDIA, settings.SYNC_AT_START)

LIGHT_TEXTS = {
    albums.GREEN: "Album ist auf Flickr und wird verwendet.",
    albums.BLUE: "Neues Album: Es wird mit dem ersten hochgeladenen Foto angelegt.",
    albums.YELLOW: (
        "Auf Flickr gibt es ein Album mit sehr ähnlichem Titel oder mehrere mit "
        "diesem Titel. Bitte den Titel ändern oder bestätigen, dass ein neues "
        "Album angelegt werden soll. Bis dahin wird nichts hochgeladen."
    ),
}


def _in_thread(work: Callable[[], None]) -> None:
    threading.Thread(target=work, daemon=True).start()


def _show_in_explorer(path: str) -> None:
    os.startfile(path)  # noqa: S606 - Windows only, like the whole app


class AccountMismatchError(auth.AuthError):
    """The stored login belongs to another Flickr account than the database."""


class Api:
    def __init__(
        self,
        database: db.Database,
        *,
        client_factory: Callable[..., FlickrClient] | None = None,
        store: auth.CredentialStore | None = None,
        login_flow: Callable[[auth.ApiCredentials], auth.LoginFlow] = auth.LoginFlow,
        background: Callable[[Callable[[], None]], None] = _in_thread,
        open_url: Callable[[str], object] = webbrowser.open,
        open_folder: Callable[[str], object] = _show_in_explorer,
    ) -> None:
        self._database = database
        # The Credential Manager is only opened when needed (never in tests).
        self._store = store
        self._client_factory = client_factory or (
            lambda **kwargs: FlickrClient.from_store(self._credentials(), **kwargs)
        )
        self._login_flow = login_flow
        # A login that waits for the code from Flickr: (credentials, flow).
        self._login: tuple[auth.ApiCredentials, auth.LoginFlow] | None = None
        self._background = background
        self._open_url = open_url
        self._open_folder = open_folder
        # The running upload or download of this process (the run itself is in the DB).
        self._job_lock = threading.Lock()
        self._job_running = False
        self._cancel: threading.Event | None = None
        self._last_report: dict | None = None
        self._window = None
        # One budget for all Flickr calls of the app, whichever thread makes them.
        self._limiter = RateLimiter()
        self._prechecks: set[int] = set()
        self._prechecks_lock = threading.Lock()

    def _attach(self, window) -> None:
        """Called by app.py once the window exists (for the folder dialog)."""
        self._window = window

    # --- Status ---------------------------------------------------------------

    def status(self) -> dict:
        """Login and database, for the status bar. Asks Flickr nothing."""
        try:
            self._flickr()
        except AccountMismatchError as error:
            login = {"logged_in": False, "message": str(error)}
        except auth.AuthError:
            login = {
                "logged_in": False,
                "message": "Nicht angemeldet. Einrichten im Reiter „Einstellungen“.",
            }
        else:
            login = {"logged_in": True, "message": "Bei Flickr angemeldet"}
        return {**login, "setup_command": SETUP_COMMAND, "database": str(self._database.path)}

    # --- Source folders ---------------------------------------------------------

    def roots(self) -> list[dict]:
        with self._database.connect() as conn:
            rows = conn.execute("SELECT id, path, scanned_at FROM roots ORDER BY path").fetchall()
        return [
            {
                "id": row["id"],
                "path": row["path"],
                "scanned_at": row["scanned_at"],
                "reachable": os.path.isdir(config.filesystem_path(row["path"])),
            }
            for row in rows
        ]

    def add_root(self) -> dict:
        """Ask for a folder, register it as source folder and scan it."""
        path = self._pick_folder()
        if path is None:
            return {"cancelled": True}
        try:
            with self._database.connect() as conn:
                root_id = scanner.add_root(conn, path)
        except scanner.OverlappingRootError as error:
            return {"error": f"Überschneidet sich mit einem schon geöffneten Ordner: {error}"}
        except scanner.RootUnavailableError:
            return {"error": f"Der Ordner ist nicht erreichbar: {path}"}
        return {"root_id": root_id, **self.scan(root_id)}

    def scan(self, root_id: int) -> dict:
        try:
            with self._database.connect() as conn:
                report = scanner.scan_root(conn, root_id)
        except scanner.RootUnavailableError as error:
            return {
                "error": f"Der Ordner ist gerade nicht erreichbar, nichts wurde geändert ({error})"
            }
        return {
            "report": {
                "folders": report.folders,
                "files": report.files,
                "new_files": report.new_files,
                "changed_files": report.changed_files,
                "missing_files": report.missing_files,
                "unreadable": len(report.unreadable),
            }
        }

    def tree(self, root_id: int) -> dict:
        """All present folders of a source folder, with what the tree view shows."""
        with self._database.connect() as conn:
            root = conn.execute("SELECT id, path FROM roots WHERE id = ?", (root_id,)).fetchone()
            if root is None:
                return {"error": "Diesen Ausgangsordner gibt es nicht mehr."}
            return {
                "root": {"id": root["id"], "path": root["path"]},
                "folders": self._folders(conn, root),
            }

    # --- Album folders and ticks --------------------------------------------------

    def make_album_folder(self, folder_id: int, title: str | None = None) -> dict:
        try:
            with self._database.connect() as conn:
                return {"album_id": albums.assign_album(conn, folder_id, title or None)}
        except sqlite3.IntegrityError as error:
            if "nested album folder" not in str(error):
                raise
            return {
                "error": "Über oder unter einem Album-Ordner kann kein weiterer Album-Ordner "
                "liegen. Der Elternordner ist führend."
            }
        except ValueError:
            return {"error": "Der Albumtitel darf nicht leer sein."}

    def remove_album_folder(self, folder_id: int) -> dict:
        with self._database.connect() as conn:
            albums.unassign_album(conn, folder_id)
        return {}

    def rename_planned_album(self, album_id: int, title: str) -> dict:
        try:
            with self._database.connect() as conn:
                albums.retitle_planned(conn, album_id, title)
        except ValueError:
            return {"error": "Der Albumtitel darf nicht leer sein."}
        except albums.AlbumError:
            return {"error": "Dieses Album ist schon auf Flickr; umbenennen geht dort."}
        return {}

    def confirm_new_album(self, album_id: int) -> dict:
        try:
            with self._database.connect() as conn:
                albums.confirm_new_album(conn, album_id)
        except albums.AlbumError:
            return {"error": "Nur für geplante Alben möglich."}
        return {}

    def set_included(self, folder_id: int, included: bool) -> dict:
        """Tick or untick a folder; ticking starts its pre-check in the background."""
        try:
            with self._database.connect() as conn:
                albums.set_included(conn, folder_id, bool(included))
                checked = conn.execute(
                    "SELECT prechecked_at IS NOT NULL FROM folders WHERE id = ?", (folder_id,)
                ).fetchone()[0]
        except albums.NotInAlbumFolderError:
            return {"error": "Anhaken geht nur innerhalb eines Album-Ordners."}
        if included and not checked:
            self._start_precheck(folder_id)
        return {}

    def refresh_albums(self) -> dict:
        """Read the album list from Flickr again (the traffic lights depend on it)."""
        try:
            with self._database.connect() as conn:
                report = albums.sync_with_flickr(conn, self._flickr())
        except auth.AuthError as error:
            return {"error": _login_error(error)}
        except FlickrClientError as error:
            return {"error": f"Flickr ist gerade nicht erreichbar ({error})."}
        with self._database.connect() as conn:
            lights = _light_counts(conn)
        return {
            "albums": report.flickr_albums,
            "merged": report.merged,
            "deleted": report.deleted,
            "lights": lights,
        }

    def use_existing_album(self, folder_id: int, album_id: int) -> dict:
        """Put an album folder into an album that is on Flickr under another name."""
        try:
            with self._database.connect() as conn:
                albums.use_album(conn, folder_id, album_id)
        except albums.AlbumError:
            return {"error": "Das geht nur für einen Album-Ordner."}
        except db.RecordNotFoundError:
            return {"error": "Dieses Album gibt es nicht mehr. Bitte die Albenliste aktualisieren."}
        return {}

    # --- Pre-check decisions ----------------------------------------------------------

    def matches(self, folder_id: int) -> dict:
        """Files of a folder that may already be on Flickr, with the user's decisions."""
        with self._database.connect() as conn:
            rows = conn.execute(
                "SELECT f.id AS file_id, f.name, f.taken_at, f.width, f.height, m.flickr_id, "
                "m.confidence, m.decision, p.state = 'verified' AS done "
                "FROM precheck_matches m JOIN files f ON f.id = m.file_id "
                "LEFT JOIN photos p ON p.hash = f.hash "
                "WHERE f.folder_id = ? AND f.status != 'missing' "
                "ORDER BY f.name, m.confidence = 'certain' DESC, m.flickr_id",
                (folder_id,),
            ).fetchall()
        files: dict[int, dict] = {}
        for row in rows:
            file = files.setdefault(
                row["file_id"],
                {
                    "file_id": row["file_id"],
                    "name": row["name"],
                    "taken": row["taken_at"],
                    "size": f"{row['width']} × {row['height']}" if row["width"] else None,
                    # On Flickr by now (taken over or uploaded): nothing left to decide.
                    "done": bool(row["done"]),
                    "candidates": [],
                    "decisions": set(),
                },
            )
            file["candidates"].append(
                {
                    "flickr_id": row["flickr_id"],
                    "confidence": row["confidence"],
                    "reason": MATCH_REASONS[row["confidence"]],
                    "chosen": row["decision"] == "same",
                }
            )
            file["decisions"].add(row["decision"])
        result = []
        for file in files.values():
            decisions = file.pop("decisions")
            file["state"] = (
                "same" if "same" in decisions else "open" if "open" in decisions else "upload"
            )
            result.append(file)
        return {"files": result, "open": sum(file["state"] == "open" for file in result)}

    def choose_same(self, file_id: int, flickr_id: str) -> dict:
        """This file IS that Flickr photo: take it over instead of uploading it."""
        try:
            with self._database.connect() as conn:
                precheck.choose_existing(conn, file_id, flickr_id)
        except db.RecordNotFoundError:
            return {"error": "Dieser Treffer gilt nicht mehr. Bitte die Ansicht neu laden."}
        return {}

    def choose_upload(self, file_id: int) -> dict:
        """None of the matches is this file: upload it."""
        with self._database.connect() as conn:
            precheck.choose_upload(conn, file_id)
        return {}

    def reset_choice(self, file_id: int) -> dict:
        with self._database.connect() as conn:
            precheck.reset_choice(conn, file_id)
        return {}

    def decide_folder(self, folder_id: int, choice: str) -> dict:
        """Decide all open files of a folder at once.

        "same": every file with exactly one certain match takes over that photo;
        "upload": every open file is uploaded.
        """
        if choice not in ("same", "upload"):
            return {"error": "Unbekannte Entscheidung."}
        decided = 0
        with self._database.connect() as conn:
            for file in self.matches(folder_id)["files"]:
                if file["state"] != "open":
                    continue
                certain = [c for c in file["candidates"] if c["confidence"] == "certain"]
                if choice == "upload":
                    precheck.choose_upload(conn, file["file_id"])
                elif len(certain) == 1:
                    precheck.choose_existing(conn, file["file_id"], certain[0]["flickr_id"])
                else:
                    continue
                decided += 1
        return {"decided": decided}

    def open_on_flickr(self, photo_id: str) -> dict:
        """Show a photo on flickr.com in the normal browser."""
        try:
            user = self._flickr().user_nsid
        except auth.AuthError as error:
            return {"error": _login_error(error)}
        self._open_url(f"https://www.flickr.com/photos/{user}/{photo_id}")
        return {}

    # --- Upload ------------------------------------------------------------------------

    def upload_plan(self) -> dict:
        """What a run would do now with the ticked folders."""
        with self._database.connect() as conn:
            lights = albums.lights(conn)
            album_of = {
                (row["root_id"], row["rel_path"]): row["album_id"]
                for row in conn.execute(
                    "SELECT root_id, rel_path, album_id FROM folders "
                    "WHERE album_id IS NOT NULL AND missing = 0"
                )
            }
            rows = conn.execute(
                "SELECT f.status, p.state AS photo, d.root_id, d.rel_path, "
                "d.prechecked_at IS NOT NULL AS checked, EXISTS (SELECT 1 FROM precheck_matches m "
                "WHERE m.file_id = f.id AND m.decision = 'open') AS open "
                "FROM files f JOIN folders d ON d.id = f.folder_id "
                "LEFT JOIN photos p ON p.hash = f.hash "
                "WHERE d.included = 1 AND d.missing = 0 AND f.status != 'missing'"
            ).fetchall()
        plan = dict.fromkeys(
            ("files", "ready", "done", "skipped", "failed", "decide", "album", "unchecked"), 0
        )
        for row in rows:
            plan["files"] += 1
            album_id = _enclosing_album(
                row["rel_path"],
                {path: album for (root, path), album in album_of.items() if root == row["root_id"]},
            )
            if row["status"].startswith("skipped_"):
                plan["skipped"] += 1
            elif row["photo"] == "verified":
                plan["done"] += 1
            elif row["photo"] == "failed":
                plan["failed"] += 1
            elif album_id is not None and lights.get(album_id) == albums.YELLOW:
                plan["album"] += 1
            elif row["open"]:
                plan["decide"] += 1
            else:
                plan["ready"] += 1
                plan["unchecked"] += not row["checked"]
        return plan

    def start_upload(self) -> dict:
        """Start (or resume) the upload of everything ticked, in the background."""
        try:
            client = self._flickr()
        except auth.AuthError as error:
            return {"error": _login_error(error)}
        return self._start_job(
            lambda cancel: uploader.run_upload(self._database, client, cancel=cancel)
        )

    def pause_job(self) -> dict:
        """Stop the running upload or download after its current file.

        Starting again continues where it stopped: what is done is skipped.
        """
        with self._job_lock:
            if self._job_running and self._cancel is not None:
                self._cancel.set()
        return {}

    # --- Download ------------------------------------------------------------------

    def choose_download_target(self) -> dict:
        """Ask for the folder downloads go to; never one in or around the photo archive."""
        path = self._pick_folder()
        if path is None:
            return {"cancelled": True}
        try:
            with self._database.connect() as conn:
                target = downloader.check_target(conn, path)
                settings.put(conn, settings.DOWNLOAD_TARGET, str(target))
        except downloader.TargetError:
            return {"error": TARGET_IN_ARCHIVE}
        return {"path": str(target)}

    def start_download(self, choices: list[dict]) -> dict:
        """Download the chosen albums ({"album_id", "media"} each) into the chosen folder."""
        if not choices:
            return {"error": "Bitte mindestens ein Album auswählen."}
        with self._database.connect() as conn:
            target = settings.get(conn, settings.DOWNLOAD_TARGET)
        if not target:
            return {"error": "Bitte zuerst einen Zielordner wählen."}
        if not os.path.isdir(config.filesystem_path(target)):
            return {
                "error": f"Den Zielordner gibt es nicht (mehr) oder er ist gerade nicht "
                f"erreichbar: {target}. Bitte einen anderen wählen."
            }
        try:
            client = self._flickr()
        except auth.AuthError as error:
            return {"error": _login_error(error)}
        picked = [downloader.AlbumChoice(int(c["album_id"]), c["media"]) for c in choices]
        try:
            with self._database.connect() as conn:
                downloader.validate_choices(conn, picked)
                downloader.check_target(conn, target)
        except downloader.TargetError:
            return {"error": TARGET_IN_ARCHIVE}
        except ValueError:
            return {
                "error": "Die Auswahl passt nicht mehr zur Albenliste. Bitte die Albenliste "
                "aktualisieren und neu auswählen."
            }
        return self._start_job(
            lambda cancel: downloader.run_download(
                self._database, client, picked, target, cancel=cancel
            )
        )

    def open_folder(self, path: str) -> dict:
        """Show a folder in Windows Explorer, e.g. where a download went."""
        if not path or not os.path.isdir(config.filesystem_path(path)):
            return {"error": "Diesen Ordner gibt es nicht (mehr)."}
        self._open_folder(path)
        return {}

    def _start_job(self, run: Callable[[threading.Event], object]) -> dict:
        """Run an upload or download in the background; only one at a time."""
        with self._job_lock:
            if self._job_running:
                return {"error": "Es läuft schon ein Upload oder Download."}
            self._job_running = True
            self._cancel = threading.Event()
            self._last_report = None
        cancel = self._cancel

        def work() -> None:
            try:
                report = run(cancel)
                self._last_report = dataclasses.asdict(report)
            except jobs.JobInProgressError:
                self._log_warning("Job not started: another upload or download is running")
            except Exception as error:  # the run is marked crashed; say why in the log
                self._log_warning(f"Job ended by an error: {type(error).__name__}: {error}")
            finally:
                with self._job_lock:
                    self._job_running = False

        self._background(work)
        return {}

    def job(self) -> dict:
        """The latest run with its progress and log, for the progress view."""
        with self._database.connect() as conn:
            run = conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()
            events = []
            if run is not None:
                events = conn.execute(
                    "SELECT id, ts, level, message FROM events WHERE ts >= ? "
                    "ORDER BY id DESC LIMIT ?",
                    (run["started_at"], LOG_LINES),
                ).fetchall()
        with self._job_lock:
            running = self._job_running
            pausing = running and self._cancel is not None and self._cancel.is_set()
            report = self._last_report
        return {
            "running": running,
            "pausing": pausing,
            "run": dict(run) if run is not None else None,
            "report": report if report and run and report["run_id"] == run["id"] else None,
            "events": [dict(event) for event in reversed(events)],
        }

    def _shutdown(self, timeout: float = 15.0) -> None:
        """Called by app.py when the window closes: let a running job stop cleanly.

        The current photo gets `timeout` seconds; whatever is left unfinished is
        settled by the recovery at the next start.
        """
        self.pause_job()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._job_lock:
                if not self._job_running:
                    return
            time.sleep(0.2)

    def _log_warning(self, message: str) -> None:
        with self._database.connect() as conn:
            db.log_event(conn, "warning", message)

    # --- Flickr access ----------------------------------------------------------------
    #
    # The same login as `python -m flickr_uploader.auth setup`, from the
    # settings tab. Key, secret and token go only into the Windows Credential
    # Manager: never into the database, the log or back to the page, and only
    # once the login worked. Error texts never contain them, nor a URL (the
    # request URLs carry the API key).

    def account(self) -> dict:
        """What the settings tab shows about the Flickr login: never a key or token."""
        try:
            store = self._credentials()
            has_key = store.load_api_credentials() is not None
            token = store.load_token()
        except auth.CredentialStoreError:
            return {
                "state": "damaged",
                "pending": self._login is not None,
                "setup_command": SETUP_COMMAND,
            }
        problem = None
        if has_key and token is not None and token.user_nsid:
            try:
                self._check_account(token.user_nsid, token.username)
            except AccountMismatchError as error:
                problem = str(error)
        return {
            "state": "ready" if has_key and token is not None else "missing",
            "pending": self._login is not None,
            "username": token.username if token else None,
            "fullname": token.fullname if token else None,
            "access_level": token.access_level if token else None,
            "problem": problem,
            "setup_command": SETUP_COMMAND,
        }

    def start_login(self, key: str, secret: str) -> dict:
        """Check key and secret with Flickr and open its page to allow access."""
        if self._job_is_running():
            return {"error": "Bitte erst den laufenden Upload oder Download anhalten."}
        try:
            credentials = auth.ApiCredentials.parse(str(key or ""), str(secret or ""))
        except auth.InvalidCredentialsError:
            return {
                "error": "Der API-Key hat 32 Zeichen, das Secret 16 (jeweils Ziffern und "
                "Buchstaben a–f). Bitte beide noch einmal von Flickr kopieren."
            }
        try:
            flow = self._login_flow(credentials)
        except auth.InvalidCredentialsError:
            return {"error": "Flickr kennt diesen API-Key oder dieses Secret nicht. Bitte prüfen."}
        except OSError as error:
            return {"error": f"Flickr ist gerade nicht erreichbar ({type(error).__name__})."}
        except Exception as error:  # flickrapi/requests: show the kind only, never the text
            return {"error": f"Die Anmeldung hat nicht geklappt ({type(error).__name__})."}
        self._login = (credentials, flow)
        self._open_url(flow.url)
        return {}

    def reopen_login_page(self) -> dict:
        login = self._login
        if login is None:
            return {"error": "Bitte zuerst API-Key und Secret eingeben."}
        self._open_url(login[1].url)
        return {}

    def finish_login(self, code: str) -> dict:
        """Take the code Flickr showed; only now is anything stored."""
        login = self._login
        if login is None:
            return {"error": "Bitte zuerst API-Key und Secret eingeben."}
        if not str(code or "").strip():
            return {"error": "Bitte den Code eintragen, den Flickr nach dem Erlauben zeigt."}
        credentials, flow = login
        try:
            token = flow.finish(str(code))
        except auth.InvalidCredentialsError:
            return {
                "error": "Flickr hat den Code nicht angenommen. Er gilt nur kurz und nur "
                "einmal: Bitte genau abschreiben oder mit „Abbrechen“ von vorn beginnen."
            }
        except OSError as error:
            return {"error": f"Flickr ist gerade nicht erreichbar ({type(error).__name__})."}
        except Exception as error:  # see start_login
            return {"error": f"Die Anmeldung hat nicht geklappt ({type(error).__name__})."}
        try:
            self._check_account(token.user_nsid, token.username)
        except AccountMismatchError as error:
            self._login = None
            return {"error": str(error)}
        try:
            store = self._credentials()
            store.save_api_credentials(credentials)
            store.save_token(token)
        except auth.CredentialStoreError:
            return {"error": "Der Windows-Anmeldeinformationsspeicher ließ sich nicht verwenden."}
        self._login = None
        return {"username": token.username}

    def cancel_login(self) -> dict:
        self._login = None
        return {}

    def logout(self) -> dict:
        """Remove key, secret and login from the Credential Manager."""
        if self._job_is_running():
            return {"error": "Bitte erst den laufenden Upload oder Download anhalten."}
        try:
            self._credentials().clear()
        except auth.CredentialStoreError:
            return {"error": "Der Windows-Anmeldeinformationsspeicher ließ sich nicht verwenden."}
        self._login = None
        return {}

    def open_flickr_page(self, page: str) -> dict:
        """Open one of the fixed Flickr pages around the login in the normal browser."""
        pages = {"create_key": auth.API_KEY_URL, "authorized_apps": auth.AUTHORIZED_APPS_URL}
        if page not in pages:
            return {"error": "Unbekannte Seite."}
        self._open_url(pages[page])
        return {}

    # --- Settings ---------------------------------------------------------------------

    def settings(self) -> dict:
        """All settings, plus whether the remembered download folder can be used."""
        with self._database.connect() as conn:
            values = settings.get_all(conn)
            target = values[settings.DOWNLOAD_TARGET]
            problem = None
            if target:
                try:
                    downloader.check_target(conn, target)
                except downloader.TargetError:
                    problem = "archive"
                else:
                    if not os.path.isdir(config.filesystem_path(target)):
                        problem = "missing"
        return {**values, "download_target_problem": problem}

    def set_setting(self, key: str, value) -> dict:
        if key not in EDITABLE_SETTINGS:
            return {"error": "Diese Einstellung lässt sich hier nicht ändern."}
        try:
            with self._database.connect() as conn:
                settings.put(conn, key, value)
        except ValueError:
            return {"error": "Dieser Wert passt nicht zu der Einstellung."}
        return {}

    def info(self) -> dict:
        """Version, where the database and its backups are, for the settings tab."""
        backup_dir = self._database.backup_dir
        backups = sorted(backup_dir.glob(f"{db.BACKUP_PREFIX}*.db")) if backup_dir.is_dir() else []
        return {
            "version": _version(),
            "database": str(self._database.path),
            "database_folder": str(self._database.path.parent),
            "backup_folder": str(backup_dir),
            "backups": len(backups),
            "latest_backup": _backup_time(backups[-1]) if backups else None,
            "keep_backups": db.DEFAULT_KEEP_BACKUPS,
        }

    def backup_now(self) -> dict:
        """Copy the database into the backup folder now (checked, the newest 10 stay)."""
        try:
            path = self._database.backup()
        except (db.DatabaseError, OSError) as error:
            return {"error": f"Das Backup hat nicht geklappt: {error}"}
        return {"latest_backup": _backup_time(path)}

    # --- Overviews -------------------------------------------------------------------

    def overview(self) -> dict:
        """What the step guide shows: how far the user got, and what needs a look."""
        with self._database.connect() as conn:

            def value(sql: str):
                return conn.execute(sql).fetchone()[0]

            return {
                "roots": value("SELECT count(*) FROM roots"),
                "album_folders": value(
                    "SELECT count(*) FROM folders WHERE album_id IS NOT NULL AND missing = 0"
                ),
                "included": value(
                    "SELECT count(*) FROM folders WHERE included = 1 AND missing = 0"
                ),
                "open_matches": value(
                    "SELECT count(DISTINCT file_id) FROM precheck_matches WHERE decision = 'open'"
                ),
                "lights": _light_counts(conn),
                "flickr_albums": value("SELECT count(*) FROM albums WHERE state = 'exists'"),
                "albums_synced_at": value("SELECT max(last_synced) FROM albums"),
            }

    def album_folders(self) -> list[dict]:
        """Every album folder with its album and traffic light, for the albums tab."""
        with self._database.connect() as conn:
            lights = albums.lights(conn)
            rows = conn.execute(
                "SELECT d.id, d.root_id, d.rel_path, r.path AS root_path, a.id AS album_id, "
                "a.title, a.state, a.photo_count, a.video_count "
                "FROM folders d JOIN roots r ON r.id = d.root_id "
                "JOIN albums a ON a.id = d.album_id "
                "WHERE d.missing = 0 ORDER BY a.title COLLATE NOCASE, d.rel_path"
            ).fetchall()
        return [
            {
                "folder_id": row["id"],
                "root_id": row["root_id"],
                "path": scanner.folder_path(row["root_path"], row["rel_path"]),
                "album": _album(
                    {
                        "id": row["album_id"],
                        "title": row["title"],
                        "state": row["state"],
                        "photo_count": row["photo_count"],
                        "video_count": row["video_count"],
                    },
                    lights,
                ),
            }
            for row in rows
        ]

    def flickr_albums(self) -> dict:
        """The user's albums on Flickr as last read, and which folders use them."""
        with self._database.connect() as conn:
            rows = conn.execute(
                "SELECT a.id, a.title, a.photo_count, a.video_count, "
                "(SELECT count(*) FROM folders d WHERE d.album_id = a.id AND d.missing = 0) "
                "AS used_by FROM albums a WHERE a.state = 'exists' ORDER BY a.title COLLATE NOCASE"
            ).fetchall()
            synced_at = conn.execute("SELECT max(last_synced) FROM albums").fetchone()[0]
        return {"synced_at": synced_at, "albums": [dict(row) for row in rows]}

    # --- Helpers ---------------------------------------------------------------------

    def _flickr(self) -> FlickrClient:
        # A client per use (each has its own HTTP session), one shared budget.
        client = self._client_factory(limiter=self._limiter)
        self._check_account(client.user_nsid)
        return client

    def _check_account(self, nsid: str, username: str = "") -> None:
        """Bind the database to the first Flickr account it is used with.

        Its albums and photos belong to that account: with another one,
        uploads would land in the wrong place and duplicates would follow.
        Rebinding is only allowed while the database holds nothing from Flickr.
        """
        with self._database.connect() as conn:
            bound = settings.get(conn, settings.FLICKR_ACCOUNT)
            if bound is not None and bound["nsid"] != nsid and _has_flickr_data(conn):
                owner = f"„{bound['username']}“" if bound["username"] else "einem anderen Konto"
                raise AccountMismatchError(
                    f"Angemeldet ist ein anderes Flickr-Konto als das, zu dem diese Datenbank "
                    f"gehört ({owner}). Bitte in den Einstellungen wieder mit diesem Konto "
                    "anmelden, sonst geraten Alben und Fotos durcheinander."
                )
            same = bound is not None and bound["nsid"] == nsid
            account = {"nsid": nsid, "username": username or (bound["username"] if same else "")}
            if account != bound:
                settings.put(conn, settings.FLICKR_ACCOUNT, account)

    def _credentials(self) -> auth.CredentialStore:
        if self._store is None:
            self._store = auth.CredentialStore()
        return self._store

    def _job_is_running(self) -> bool:
        with self._job_lock:
            return self._job_running

    def _pick_folder(self) -> str | None:
        import webview

        chosen = self._window.create_file_dialog(webview.FileDialog.FOLDER)
        return chosen[0] if chosen else None

    def _start_precheck(self, folder_id: int) -> None:
        with self._prechecks_lock:
            if folder_id in self._prechecks:
                return
            self._prechecks.add(folder_id)

        def work() -> None:
            try:
                client = self._flickr()
                with self._database.connect() as conn:
                    precheck.precheck_folder(conn, client, folder_id)
            except Exception as error:  # the thread's end: log it, the upload checks again
                with self._database.connect() as conn:
                    db.log_event(
                        conn,
                        "warning",
                        f"Pre-check of a ticked folder not done ({type(error).__name__}: "
                        f"{error}); the upload runs it again",
                    )
            finally:
                with self._prechecks_lock:
                    self._prechecks.discard(folder_id)

        self._background(work)

    def _folders(self, conn: sqlite3.Connection, root: sqlite3.Row) -> list[dict]:
        rows = {
            row["id"]: row
            for row in conn.execute(
                "SELECT id, rel_path, album_id, included, prechecked_at FROM folders "
                "WHERE root_id = ? AND missing = 0",
                (root["id"],),
            )
        }
        lights = albums.lights(conn)
        album_rows = {
            row["id"]: row
            for row in conn.execute(
                "SELECT id, title, state, photo_count, video_count FROM albums WHERE id IN "
                "(SELECT album_id FROM folders WHERE root_id = ? AND album_id IS NOT NULL)",
                (root["id"],),
            )
        }
        album_of_path = {r["rel_path"]: r["album_id"] for r in rows.values() if r["album_id"]}
        progress = {
            row["folder_id"]: row
            for row in conn.execute(
                "SELECT f.folder_id, count(*) AS files, "
                "coalesce(sum(p.state = 'verified'), 0) AS verified, "
                "coalesce(sum(p.state = 'failed' OR f.status = 'unreadable'), 0) AS failed "
                "FROM files f JOIN folders d ON d.id = f.folder_id "
                "LEFT JOIN photos p ON p.hash = f.hash "
                f"WHERE d.root_id = ? AND f.status IN ({', '.join('?' * len(_ELIGIBLE))}) "
                "GROUP BY f.folder_id",
                (root["id"], *_ELIGIBLE),
            )
        }
        open_matches = dict(
            conn.execute(
                "SELECT f.folder_id, count(DISTINCT f.id) FROM precheck_matches m "
                "JOIN files f ON f.id = m.file_id JOIN folders d ON d.id = f.folder_id "
                "WHERE d.root_id = ? AND m.decision = 'open' GROUP BY f.folder_id",
                (root["id"],),
            ).fetchall()
        )
        with self._prechecks_lock:
            running = set(self._prechecks)

        folders = []
        for summary in scanner.folder_summaries(conn, root["id"]):
            row = rows[summary.folder_id]
            rel_path = row["rel_path"]
            album_id = row["album_id"]
            folders.append(
                {
                    "id": row["id"],
                    "path": rel_path,
                    "name": rel_path.rsplit("/", 1)[-1] if rel_path else _last_part(root["path"]),
                    "parent": rel_path.rsplit("/", 1)[0]
                    if "/" in rel_path
                    else ("" if rel_path else None),
                    "files": summary.own.files,
                    "bytes": summary.own.bytes,
                    "skipped": summary.own.skipped,
                    "total_files": summary.total.files,
                    "total_bytes": summary.total.bytes,
                    "album": _album(album_rows[album_id], lights) if album_id else None,
                    "in_album": _enclosing_album(rel_path, album_of_path),
                    "included": bool(row["included"]),
                    "prechecked": row["prechecked_at"] is not None,
                    "precheck_running": row["id"] in running,
                    "open_matches": open_matches.get(row["id"], 0),
                    "upload": _upload_state(row, progress.get(row["id"])),
                }
            )
        return folders


def _light_counts(conn: sqlite3.Connection) -> dict[str, int]:
    """How many album folders are green, blue and yellow."""
    lights = albums.lights(conn)
    counts = {albums.GREEN: 0, albums.BLUE: 0, albums.YELLOW: 0}
    for (album_id,) in conn.execute(
        "SELECT album_id FROM folders WHERE album_id IS NOT NULL AND missing = 0"
    ):
        counts[lights[album_id]] += 1
    return counts


def _album(row: Mapping, lights: dict[int, str]) -> dict:
    light = lights[row["id"]]
    return {
        "id": row["id"],
        "title": row["title"],
        "state": row["state"],
        "light": light,
        "light_text": LIGHT_TEXTS[light],
        "photo_count": row["photo_count"],
        "video_count": row["video_count"],
    }


def _login_error(error: auth.AuthError) -> str:
    if isinstance(error, AccountMismatchError):
        return str(error)
    if isinstance(error, auth.NotConfiguredError):
        return "Nicht bei Flickr angemeldet. Einrichten im Reiter „Einstellungen“."
    return f"Nicht bei Flickr angemeldet ({error}). Bitte im Reiter „Einstellungen“ neu einrichten."


def _has_flickr_data(conn: sqlite3.Connection) -> bool:
    """Does the database refer to photos or albums of a Flickr account?"""
    return bool(
        conn.execute(
            "SELECT EXISTS (SELECT 1 FROM albums WHERE flickr_id IS NOT NULL) "
            "OR EXISTS (SELECT 1 FROM photos WHERE flickr_id IS NOT NULL) "
            "OR EXISTS (SELECT 1 FROM precheck_matches)"
        ).fetchone()[0]
    )


def _version() -> str:
    try:
        return importlib.metadata.version("flickr-uploader")
    except importlib.metadata.PackageNotFoundError:
        return "?"


def _backup_time(path) -> str:
    """state-20261013T120000123456Z.db -> 2026-10-13T12:00:00+00:00 (ISO, UTC)."""
    stamp = path.stem.removeprefix(db.BACKUP_PREFIX)
    when = datetime.strptime(stamp, "%Y%m%dT%H%M%S%fZ").replace(tzinfo=UTC)
    return when.isoformat(timespec="seconds")


def _last_part(path: str) -> str:
    """D:\\Bilder -> Bilder; a drive root stays as it is."""
    return os.path.basename(path.rstrip("\\/")) or path


def _enclosing_album(rel_path: str, album_of_path: dict[str, int]) -> int | None:
    """The album of the folder itself or of the album folder above it."""
    parts = rel_path.split("/") if rel_path else []
    for depth in range(len(parts), -1, -1):
        album_id = album_of_path.get("/".join(parts[:depth]))
        if album_id is not None:
            return album_id
    return None


def _upload_state(folder: sqlite3.Row, progress: sqlite3.Row | None) -> dict | None:
    """How far the upload of a ticked folder got: open, partial, done or error."""
    if not folder["included"]:
        return None
    files = progress["files"] if progress else 0
    verified = progress["verified"] if progress else 0
    failed = progress["failed"] if progress else 0
    if failed:
        state = "error"
    elif files and verified == files:
        state = "done"
    elif verified:
        state = "partial"
    else:
        state = "open"
    return {"state": state, "files": files, "verified": verified, "failed": failed}
