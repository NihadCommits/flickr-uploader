r"""
Try a real upload run with one folder, without touching the tool's database.

    .venv\Scripts\python.exe scripts\upload_probe.py "D:\Test\Upload-Test"
    .venv\Scripts\python.exe scripts\upload_probe.py "D:\Test\Upload-Test" "Mein Testalbum"
    .venv\Scripts\python.exe scripts\upload_probe.py "D:\Test\Upload-Test" --retry-failed

The folder becomes an album folder (title: its name, or the second argument),
is ticked and uploaded the way the tool does it: private and hidden from
search, with hash tag, checked by getInfo, into the album. Only the files
directly in the folder count, like in the tool.

The probe keeps its own database in %LOCALAPPDATA%\flickr-uploader-probe, so
the tool's real one is not created yet. Run it again on the same folder to see
that nothing is uploaded twice. Press Ctrl+C during a run to try the abort,
then start it again: it continues where it stopped. Files that failed (e.g.
refused by Flickr) are only tried again with --retry-failed.

Nothing is ever deleted on Flickr (the tool has no permission to). Delete the
test photos and the album on Flickr yourself afterwards, if you like.
"""

import os
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

try:
    from flickr_uploader import albums, auth, config, db, flickr_client, scanner, uploader
except ImportError:
    sys.exit(r"flickr_uploader ist nicht installiert: .venv\Scripts\python.exe -m pip install -e .")

PROBE_DIR_NAME = "flickr-uploader-probe"
POLL_SECONDS = 0.5
LEVELS = {"info": "     ", "warning": "WARN ", "error": "FEHLER"}
LIGHTS = {
    albums.GREEN: "grün (Album ist auf Flickr)",
    albums.BLUE: "blau (wird neu angelegt)",
    albums.YELLOW: "GELB (ähnlicher Titel auf Flickr, wartet auf Entscheidung)",
}


def main(argv: list[str]) -> int:
    retry_failed = "--retry-failed" in argv
    argv = [arg for arg in argv if arg != "--retry-failed"]
    if len(argv) not in (1, 2):
        print(__doc__)
        return 2
    folder = Path(argv[0]).resolve()
    title = argv[1] if len(argv) == 2 else None
    if not folder.is_dir():
        print(f"Kein Ordner: {folder}")
        return 2
    try:
        client = flickr_client.FlickrClient.from_store()
    except auth.AuthError as error:
        print(f"Fehler: {error}")
        return 1

    local = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    paths = config.app_paths({config.HOME_ENV_VAR: str(Path(local) / PROBE_DIR_NAME)})
    database = db.Database(paths.database, backup_dir=paths.backups)
    try:
        database.open()
    except db.AlreadyRunningError:
        print("Die Probe läuft schon in einem anderen Fenster.")
        return 1
    try:
        print(f"Testprofil: {paths.data_dir}")
        with database.connect() as conn:
            folder_id = prepare(conn, folder, title)
            if folder_id is None:
                return 1
            if retry_failed:
                retry(conn, folder_id)
            last_event = conn.execute("SELECT coalesce(max(id), 0) FROM events").fetchone()[0]
        report = run_with_live_log(database, client, last_event)
        with database.connect() as conn:
            summarize(conn, client, folder_id, report)
        return 0 if report.status == "completed" else 1
    finally:
        database.close()


def prepare(conn, folder: Path, title: str | None) -> int | None:
    try:
        root_id = scanner.add_root(conn, folder)  # the known one on later runs
    except scanner.OverlappingRootError as error:
        print(f"Fehler: {error}")
        print("Für die Probe jeweils denselben Ordner nehmen, nicht einen darüber oder darunter.")
        return None
    scanner.scan_root(conn, root_id)
    folder_id = conn.execute(
        "SELECT id FROM folders WHERE root_id = ? AND rel_path = ''", (root_id,)
    ).fetchone()[0]
    files = conn.execute(
        "SELECT count(*) FROM files WHERE folder_id = ? AND status != 'missing'", (folder_id,)
    ).fetchone()[0]
    print(f"Ordner: {folder} ({files} Dateien direkt darin)")
    current = albums.album_folder(conn, folder_id)
    current_title = (
        conn.execute("SELECT title FROM albums WHERE id = ?", (current["album_id"],)).fetchone()[0]
        if current
        else None
    )
    if current is None or (title is not None and title.strip() != current_title):
        albums.assign_album(conn, folder_id, title)
    albums.set_included(conn, folder_id, True)
    return folder_id


def retry(conn, folder_id: int) -> None:
    failed = conn.execute(
        "SELECT DISTINCT p.hash FROM photos p JOIN files f ON f.hash = p.hash "
        "WHERE f.folder_id = ? AND p.state = 'failed'",
        (folder_id,),
    ).fetchall()
    for row in failed:
        uploader.reset_failed(conn, row[0])
    print(f"{len(failed)} fehlgeschlagene Datei(en) für einen neuen Versuch freigegeben")


def run_with_live_log(database, client, last_event: int) -> uploader.RunReport:
    cancel = threading.Event()
    outcome = {}

    def work():
        try:
            outcome["report"] = uploader.run_upload(database, client, cancel=cancel)
        except BaseException as error:  # shown below, after the log
            outcome["error"] = error

    worker = threading.Thread(target=work, name="upload")
    worker.start()
    print("Upload läuft (Strg+C bricht nach dem aktuellen Foto ab) ...\n")
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
                    print("\nAbbruch angefordert, das aktuelle Foto wird noch fertig ...\n")
                    cancel.set()
    worker.join()
    if "error" in outcome:
        raise outcome["error"]
    return outcome["report"]


def summarize(conn, client, folder_id: int, report: uploader.RunReport) -> None:
    album_id = albums.album_folder(conn, folder_id)["album_id"]
    album = conn.execute("SELECT title, flickr_id FROM albums WHERE id = ?", (album_id,)).fetchone()
    light = albums.lights(conn)[album_id]
    open_matches = conn.execute(
        "SELECT f.name, m.flickr_id, m.confidence FROM precheck_matches m "
        "JOIN files f ON f.id = m.file_id WHERE f.folder_id = ? AND m.decision = 'open' "
        "ORDER BY f.name",
        (folder_id,),
    ).fetchall()

    print("\nZusammenfassung (die reicht als Rückmeldung):")
    print(f"  Lauf:                      {report.status}")
    print(f"  Dateien im Ordner:         {report.files}")
    print(f"  neu hochgeladen:           {report.uploaded}")
    print(f"  schon auf Flickr:          {report.already_done}")
    print(f"  übernommen (alte Fotos):   {report.adopted}")
    print(f"  ins Album einsortiert:     {report.added_to_albums}")
    print(f"  Alben neu angelegt:        {report.albums_created}")
    print(f"  übersprungen (RAW usw.):   {report.skipped}")
    print(f"  wartet auf Entscheidung:   {report.waiting}")
    print(f"  fehlgeschlagen:            {report.failed}")
    print(f"  unklar (nächster Lauf):    {report.unclear}")
    print(f"  Album:                     {album['title']} - {LIGHTS[light]}")
    if album["flickr_id"]:
        print(f"  Link:                      {album_link(client.user_nsid, album['flickr_id'])}")
    if report.failed:
        print("\nFehlgeschlagen: Der Grund steht oben im Log (FEHLER/WARN). Für einen")
        print("neuen Versuch denselben Befehl mit --retry-failed am Ende aufrufen.")
    if open_matches:
        print("\nZurückgehalten, weil sie schon auf Flickr sein könnten (Pre-Check):")
        for match in open_matches:
            print(f"  {match['name']}: {match['confidence']}, Flickr-Foto {match['flickr_id']}")


def album_link(user_nsid: str, album_id: str) -> str:
    return f"https://www.flickr.com/photos/{user_nsid}/albums/{album_id}"


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
