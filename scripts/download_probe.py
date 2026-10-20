r"""
Try the download with real albums, before the GUI exists.

    .venv\Scripts\python.exe scripts\download_probe.py --list
    .venv\Scripts\python.exe scripts\download_probe.py "D:\Downloads" "Mein Album=videos"
    .venv\Scripts\python.exe scripts\download_probe.py "D:\Downloads" 72157...=all "Album 2=photos"

Every album is given as "<album>=<filter>": the album by ID or exact title,
the filter videos, photos or all. Each album lands in its own subfolder of the
target, named after the album. Originals where Flickr offers them, otherwise
the largest version; the recording date goes into the files where missing.

Nothing changes on Flickr. A temporary database is used (a download needs no
lasting one: what is there already is told by the target folder), so run it
again with the same target to see that only missing files are loaded.
Ctrl+C stops after the current file.
"""

import sys
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path

try:
    from flickr_uploader import albums, auth, db, downloader, flickr_client
except ImportError:
    sys.exit(r"flickr_uploader ist nicht installiert: .venv\Scripts\python.exe -m pip install -e .")

POLL_SECONDS = 0.5
LEVELS = {"info": "     ", "warning": "WARN ", "error": "FEHLER"}


def main(argv: list[str]) -> int:
    if not argv or (argv[0] != "--list" and len(argv) < 2):
        print(__doc__)
        return 2
    try:
        client = flickr_client.FlickrClient.from_store()
    except auth.AuthError as error:
        print(f"Fehler: {error}")
        return 1

    with tempfile.TemporaryDirectory() as scratch:
        database = db.Database(Path(scratch) / "probe.db")
        database.open()
        try:
            with database.connect() as conn:
                print("Albenliste wird von Flickr geholt ...")
                albums.sync_with_flickr(conn, client)
                if argv[0] == "--list":
                    list_albums(conn)
                    return 0
                choices = parse_choices(conn, argv[1:])
                if choices is None:
                    return 2
            report = run_with_live_log(database, client, choices, argv[0])
        except downloader.TargetError as error:
            print(f"Fehler: {error}")
            return 2
        finally:
            database.close()
    summarize(report)
    return 0 if report.status == "completed" else 1


def list_albums(conn) -> None:
    for row in conn.execute(
        "SELECT flickr_id, title, photo_count, video_count FROM albums "
        "WHERE state = 'exists' ORDER BY title COLLATE NOCASE"
    ):
        print(f"{row[0]}  {row[2]:>5} Fotos {row[3]:>5} Videos  {row[1]}")


def parse_choices(conn, specs: list[str]) -> list[downloader.AlbumChoice] | None:
    choices = []
    for spec in specs:
        wanted, _, media = spec.rpartition("=")
        if not wanted or media not in downloader.MEDIA_FILTERS:
            print(f'"{spec}": bitte als "<Album>=videos", "=photos" oder "=all" angeben')
            return None
        rows = conn.execute(
            "SELECT id FROM albums WHERE state = 'exists' AND (flickr_id = ? OR title = ?)",
            (wanted, wanted),
        ).fetchall()
        if len(rows) != 1:
            problem = "nicht gefunden" if not rows else "mehrdeutig, bitte die ID angeben"
            print(f'Album "{wanted}": {problem} (--list zeigt alle)')
            return None
        choices.append(downloader.AlbumChoice(rows[0][0], media))
    return choices


def run_with_live_log(database, client, choices, target) -> downloader.DownloadReport:
    cancel = threading.Event()
    outcome = {}

    def work():
        try:
            outcome["report"] = downloader.run_download(
                database, client, choices, target, cancel=cancel
            )
        except BaseException as error:  # shown below, after the log
            outcome["error"] = error

    worker = threading.Thread(target=work, name="download")
    worker.start()
    print("Download läuft (Strg+C bricht nach der aktuellen Datei ab) ...\n")
    last_event = 0
    with database.connect() as conn:
        while True:
            try:
                alive = worker.is_alive()
                for event in conn.execute(
                    "SELECT id, ts, level, message FROM events WHERE id > ? ORDER BY id",
                    (last_event,),
                ).fetchall():
                    last_event = event["id"]
                    stamp = datetime.fromisoformat(event["ts"]).astimezone().strftime("%H:%M:%S")
                    print(f"{stamp} {LEVELS.get(event['level'], '')} {event['message']}")
                if not alive:
                    break
                time.sleep(POLL_SECONDS)
            except KeyboardInterrupt:
                if not cancel.is_set():
                    print("\nAbbruch angefordert, die aktuelle Datei wird noch fertig ...\n")
                    cancel.set()
    worker.join()
    if "error" in outcome:
        raise outcome["error"]
    return outcome["report"]


def summarize(report: downloader.DownloadReport) -> None:
    print("\nZusammenfassung (die reicht als Rückmeldung):")
    print(f"  Lauf:                      {report.status}")
    print(f"  Alben:                     {report.albums}")
    print(f"  Dateien:                   {report.files}")
    print(f"  heruntergeladen:           {report.downloaded}")
    print(f"  schon vorhanden:           {report.existing}")
    print(f"  ohne Original (größte):    {report.not_original}")
    print(f"  mit Aufnahmedatum:         {report.dated}")
    print(f"  ohne Datum auf Flickr:     {report.undated}")
    print(f"  fehlgeschlagen:            {report.failed}")


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
