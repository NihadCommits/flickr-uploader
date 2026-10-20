r"""
Rename Flickr albums (photosets) based on a CSV produced by export_albums.py.

Reads albums_export.csv and, for every row where "new_title" is filled in and
differs from "title", renames that album via flickr.photosets.editMeta.

Runs in DRY-RUN mode by default (only prints what it would do, no Flickr login).
Pass --apply to actually perform the renames.

Setup (once), stores the login in the Windows Credential Manager:
    .venv\Scripts\python.exe -m flickr_uploader.auth setup

Usage:
    .venv\Scripts\python.exe scripts\rename_albums.py                # dry run
    .venv\Scripts\python.exe scripts\rename_albums.py --apply        # actually rename
    .venv\Scripts\python.exe scripts\rename_albums.py my_edits.csv --apply
"""

import argparse
import csv
import sys

try:
    from flickr_uploader import auth
except ImportError:
    sys.exit(r"flickr_uploader ist nicht installiert: .venv\Scripts\python.exe -m pip install -e .")


def get_flickr():
    """Return a Flickr client logged in with the stored credentials (write access)."""
    try:
        return auth.flickr_api()
    except auth.AuthError as error:
        sys.exit(f"Fehler: {error}")


def load_changes(csv_file):
    changes = []
    # utf-8-sig also accepts files re-saved with a BOM (e.g. by Excel).
    with open(csv_file, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            new_title = row["new_title"].strip()
            if new_title and new_title != row["title"]:
                changes.append(
                    (row["photoset_id"], row["title"], new_title, row.get("description"))
                )
    return changes


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("csv_file", nargs="?", default="albums_export.csv")
    parser.add_argument(
        "--apply", action="store_true", help="Änderungen wirklich durchführen (sonst nur Vorschau)"
    )
    args = parser.parse_args()

    changes = load_changes(args.csv_file)

    if not changes:
        print("Keine Änderungen gefunden (new_title leer oder identisch mit title).")
        return

    print(f"{len(changes)} Umbenennung(en) geplant:\n")
    for photoset_id, old, new, _ in changes:
        print(f"  [{photoset_id}]  '{old}'  ->  '{new}'")

    if not args.apply:
        print(
            "\nDry-Run – es wurde nichts geändert. Mit --apply erneut ausführen, um zu übernehmen."
        )
        return

    flickr = get_flickr()

    print("\nWende Änderungen an...")
    for photoset_id, old, new, description in changes:
        params = {"photoset_id": photoset_id, "title": new}
        # The API docs don't say whether omitting "description" clears it,
        # so send the exported one back unchanged when we have it.
        if description is not None:
            params["description"] = description
        flickr.photosets.editMeta(**params)
        print(f"  OK: {old} -> {new}")

    print("\nFertig.")


if __name__ == "__main__":
    main()
