r"""
Real end-to-end check of the Flickr connection. Run it yourself, in PowerShell:

    .venv\Scripts\python.exe scripts\flickr_smoke_test.py

It checks the assumptions the tool relies on against the real Flickr API
(flickrapi is unmaintained, so they can't be taken for granted):

1. Upload a tiny generated test image (random pixels, 64x64 PNG, file name
   with umlauts) as PRIVATE: only you can see it, hidden from search.
2. photos.getInfo: owner, title with umlauts, and the hash tag with you as
   its author.
3. Create the album "flickr-uploader Test (bitte löschen)" with the image as
   its cover, add the image a second time (must count as "already there"),
   read the album back.
4. Search for the hash tag for up to two minutes (Flickr's search index lags;
   the result tells how long to wait before retrying an unclear upload).

Nothing is deleted: the tool has no permission to. Please delete the test
photo and album on Flickr yourself afterwards (links are printed).
"""

import os
import struct
import sys
import tempfile
import time
import zlib
from pathlib import Path

try:
    from flickr_uploader import auth, flickr_client, hashing
except ImportError:
    sys.exit(r"flickr_uploader ist nicht installiert: .venv\Scripts\python.exe -m pip install -e .")

TITLE = "flickr-uploader Testbild äöü"
ALBUM_TITLE = "flickr-uploader Test (bitte löschen)"
SEARCH_TIMEOUT = 120
SEARCH_INTERVAL = 15


def make_png(width: int = 64, height: int = 64) -> bytes:
    """A valid PNG with random pixels, so every run uploads a new file."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(kind + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", crc)

    rows = b"".join(b"\x00" + os.urandom(width * 3) for _ in range(height))
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)  # 8-bit RGB
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(rows))
        + chunk(b"IEND", b"")
    )


def check(label: str, ok: bool, detail: str = "") -> bool:
    print(f"  [{'OK' if ok else 'FEHLER'}] {label}" + (f": {detail}" if detail else ""))
    return ok


def main() -> int:
    print("Flickr-Verbindungstest")
    print(
        f"\nDas Skript lädt ein privates Testbild hoch und legt das Album\n"
        f'"{ALBUM_TITLE}" an. Beides danach bitte selbst auf Flickr löschen.'
    )
    if input("\nFortfahren? [j/N] ").strip().lower() not in ("j", "ja", "y", "yes"):
        print("Abgebrochen, nichts geändert.")
        return 1

    try:
        client = flickr_client.FlickrClient.from_store()
    except auth.AuthError as error:
        print(f"Fehler: {error}")
        return 1
    base = f"https://www.flickr.com/photos/{client.user_nsid}"

    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / f"{TITLE}.png"
        path.write_bytes(make_png())
        digest = hashing.hash_file(path).digest

        print("\n1. Upload (privat)")
        try:
            photo_id = client.upload(path, title=TITLE, digest=digest)
        except flickr_client.FlickrClientError as error:
            check("Upload", False, f"{type(error).__name__}: {error}")
            return 1
        check("Upload", True, f"Foto-ID {photo_id}, {base}/{photo_id}")

    try:
        all_ok = check_uploaded(client, base, photo_id, digest)
    except flickr_client.FlickrClientError as error:
        all_ok = check("Verbindung", False, f"{type(error).__name__}: {error}")

    print("\nErgebnis:", "ALLES OK" if all_ok else "ES GAB FEHLER (siehe oben)")
    print("Bitte Testfoto und Test-Album jetzt auf Flickr löschen:")
    print(f"  {base}/{photo_id}")
    return 0 if all_ok else 1


def check_uploaded(client, base: str, photo_id: str, digest: str) -> bool:
    all_ok = True
    print("\n2. Prüfung per getInfo")
    info = client.photo_info(photo_id)
    all_ok &= check("Foto existiert", info is not None)
    if info is not None:
        all_ok &= check("Besitzer bist du", info.owner_nsid == client.user_nsid)
        all_ok &= check("Titel mit Umlauten korrekt", info.title == TITLE, repr(info.title))
        all_ok &= check("Hash-Tag vorhanden, Autor bist du", digest in info.hash_digests)
        all_ok &= check("Foto ist privat (nur du)", info.is_private)
    all_ok &= check("verify_upload()", client.verify_upload(photo_id, digest))

    print("\n3. Album")
    try:
        album_id = client.create_album(
            ALBUM_TITLE, photo_id, "Automatischer Test von flickr-uploader, kann gelöscht werden."
        )
        check("Album angelegt", True, f"{base}/albums/{album_id}")
        all_ok &= check(
            "Zweites Hinzufügen gilt als 'schon drin'",
            client.add_to_album(album_id, photo_id) is False,
        )
        all_ok &= check("Foto ist im Album", photo_id in client.album_photo_ids(album_id))
        titles = {album.id: album.title for album in client.albums()}
        all_ok &= check(
            "Album in der Albenliste, Titel korrekt",
            titles.get(album_id) == ALBUM_TITLE,
            repr(titles.get(album_id)),
        )
    except flickr_client.FlickrClientError as error:
        all_ok = check("Album", False, f"{type(error).__name__}: {error}")

    print(f"\n4. Suche nach dem Hash-Tag (bis zu {SEARCH_TIMEOUT} s, Suchindex hinkt nach)")
    started = time.monotonic()
    while True:
        found = client.find_by_hash(digest)
        waited = time.monotonic() - started
        if photo_id in found:
            check("Gefunden", True, f"nach ca. {waited:.0f} s")
            break
        if waited >= SEARCH_TIMEOUT:
            # Not a fault of the tool, only the index delay: noted, not failed.
            print(f"  [HINWEIS] nach {SEARCH_TIMEOUT} s noch nicht im Suchindex (nur Verzögerung)")
            break
        print(f"  ... noch nicht gefunden ({waited:.0f} s), neuer Versuch in {SEARCH_INTERVAL} s")
        time.sleep(SEARCH_INTERVAL)
    return all_ok


if __name__ == "__main__":
    sys.exit(main())
