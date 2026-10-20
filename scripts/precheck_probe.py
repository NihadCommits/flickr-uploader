r"""
Try the pre-check on one real folder. Read-only: nothing is uploaded or changed
on Flickr, and the tool's database is not touched (a temporary one is used).

    .venv\Scripts\python.exe scripts\precheck_probe.py "D:\Bilder\2019\2019-08 Italien"

Pick a folder with photos directly inside that you uploaded to Flickr before,
without this tool: the result shows how many of them the pre-check recognizes.
The per-file list stays on your screen; for feedback the summary is enough.
"""

import sys
import tempfile
from pathlib import Path

try:
    from flickr_uploader import auth, db, flickr_client, precheck, scanner
except ImportError:
    sys.exit(r"flickr_uploader ist nicht installiert: .venv\Scripts\python.exe -m pip install -e .")

LABELS = {"certain": "sicher", "probable": "wahrscheinlich"}


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print(__doc__)
        return 2
    folder = Path(argv[0])
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
                root = scanner.add_root(conn, folder)
                scanner.scan_root(conn, root)
                folder_id = conn.execute(
                    "SELECT id FROM folders WHERE root_id = ? AND rel_path = ''", (root,)
                ).fetchone()[0]
                print(f"Prüfe {folder} ...")
                report = precheck.precheck_folder(conn, client, folder_id)
                rows = conn.execute(
                    "SELECT f.name, f.taken_at, m.flickr_id, m.confidence "
                    "FROM files f LEFT JOIN precheck_matches m ON m.file_id = f.id "
                    "WHERE f.folder_id = ? AND f.status IN ('new', 'hashed') "
                    "ORDER BY f.name, m.confidence",
                    (folder_id,),
                ).fetchall()
        except (scanner.ScanError, flickr_client.FlickrClientError) as error:
            print(f"Fehler: {type(error).__name__}: {error}")
            return 1
        finally:
            database.close()

    print()
    for name, taken, flickr_id, confidence in rows:
        result = f"{LABELS[confidence]} (Flickr {flickr_id})" if flickr_id else "kein Treffer"
        print(f"  {name:32} {taken or 'ohne Datum':19}  {result}")

    print("\nZusammenfassung (die reicht als Rückmeldung):")
    print(f"  Dateien geprüft:           {report.files}")
    print(f"  sicher schon auf Flickr:   {report.certain}")
    print(f"  wahrscheinlich:            {report.probable}")
    print(f"  ohne Aufnahmedatum:        {report.without_date}")
    print(f"  nicht lesbar:              {report.unreadable}")
    print(f"  durchsuchter Zeitraum:     {report.searched or '-'}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
