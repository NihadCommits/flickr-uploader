r"""
Export all existing Flickr photosets (albums) to a CSV file.

Setup (once): store API key and login in the Windows Credential Manager with
    .venv\Scripts\python.exe -m flickr_uploader.auth setup

Run:
    .venv\Scripts\python.exe scripts\export_albums.py

Result: albums_export.csv in the current directory. Git ignores it, because it
contains your album names.

The CSV includes an empty "new_title" column, ready for you to fill in by hand
and feed into rename_albums.py afterwards. The "description" column is only a
backup that rename_albums.py sends along unchanged; do not edit it.
"""

import csv
import sys

try:
    from flickr_uploader import auth
except ImportError:
    sys.exit(r"flickr_uploader ist nicht installiert: .venv\Scripts\python.exe -m pip install -e .")


def fetch_all_photosets(flickr):
    """Fetch every photoset (album), handling pagination."""
    albums = []
    page = 1
    while True:
        result = flickr.photosets.getList(page=page, per_page=500)
        photosets = result["photosets"]
        albums.extend(photosets["photoset"])
        if page >= photosets["pages"]:
            break
        page += 1
    return albums


def main():
    try:
        flickr = auth.flickr_api()
    except auth.AuthError as error:
        sys.exit(f"Fehler: {error}")

    albums = fetch_all_photosets(flickr)
    print(f"{len(albums)} Alben gefunden.")

    out_path = "albums_export.csv"
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(
            [
                "photoset_id",
                "title",
                "description",
                "photo_count",
                "created",
                "last_updated",
                "new_title",
            ]
        )
        for album in albums:
            writer.writerow(
                [
                    album["id"],
                    album["title"]["_content"],
                    album["description"]["_content"],
                    album["photos"],
                    album["date_create"],
                    album["date_update"],
                    "",  # <- hier den gewünschten neuen Namen eintragen, sonst leer lassen
                ]
            )

    print(f"Fertig: {out_path}")


if __name__ == "__main__":
    main()
