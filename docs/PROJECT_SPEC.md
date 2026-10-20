# Flickr Folder Uploader – Phase 1 Spec

## Ziel
GUI-Tool (Python), das eine lokale Ordnerstruktur (Windows) durchsucht, dem Nutzer
erlaubt, gezielt einzelne Ordner für den Upload auszuwählen und sie Flickr-Alben
zuzuordnen, und den Upload robust, resumable und nachvollziehbar durchführt.

Leitbild: Die Windows-Ordner werden so nah wie möglich in Flickr gespiegelt.
Keine doppelten Fotos, keine doppelten Alben, keine wirren Albennamen.

Kein automatisiertes Voll-Backup, kein RAW-Handling im Tool (wird vorher am PC
erledigt), keine Kompression in Phase 1 (zu große/inkompatible Dateien werden
übersprungen + geloggt).

Dazu kommt der Rückweg: Eigene Alben lassen sich wieder auf den PC laden, mit
Filter nur Videos, nur Fotos oder alles, als Original, wo Flickr es anbietet,
und mit dem Aufnahmedatum in den Dateien (siehe "Download").

## Nicht-Ziele in Phase 1 (bewusst verschoben)
- Video-Kompression / Ziel-Bitrate-Berechnung → Phase 2
- Fuzzy-Matching-Warnsystem für ähnliche (nicht gleiche) Albennamen → Phase 2
- Dry-Run-Modus, Video-Splitting → Phase 3
- EXIF in heruntergeladene Fotos schreiben, für die Flickr kein Original
  anbietet → Phase 2, falls nötig (bräuchte eine neue Abhängigkeit)

**Nie, in keiner Phase:** Fotos öffentlich oder für Freunde/Familie sichtbar
hochladen oder die Sichtbarkeit von Fotos lockern. Jeder Upload ist privat und
aus der Suche verborgen, ohne Einstellmöglichkeit. Veröffentlichen ist eine
eigene, spätere Entscheidung des Nutzers auf Flickr, unabhängig von diesem Tool.

## Grundprinzipien für Zuverlässigkeit

1. **Die DB ist die einzige Wahrheit.** UI, Uploader und Bericht lesen nur aus ihr.
2. **Erst die Absicht schreiben, dann handeln.** Vor jedem Flickr-Aufruf mit
   Nebenwirkung (Upload, Album anlegen) wird der Zwischenzustand committet.
   Nach einem Absturz ist damit immer erkennbar, was evtl. halb passiert ist.
3. **Jede Antwort sofort sichern.** Eine von Flickr erhaltene ID wird im selben
   Moment committet, noch vor dem nächsten Schritt.
4. **Im Zweifel erst nachsehen, dann wiederholen.** Unklare Uploads werden per
   Hash-Tag auf Flickr gesucht, bevor erneut hochgeladen wird.
5. **Wiederholbar ohne Schaden.** Album-Zuordnungen sind idempotent
   ("Photo already in set" zählt als Erfolg).
6. **Das Foto-Archiv wird nur gelesen.** Das Tool schreibt, verschiebt oder
   löscht nie etwas in den Ordnern des Nutzers. Geschrieben wird nur beim
   Download, und zwar ausschließlich in einen eigens gewählten Zielordner, der
   weder in einem Root liegt noch einen enthält. Dort wird nie etwas
   überschrieben oder gelöscht.
7. **Nur eine Instanz gleichzeitig.** Eine Lock-Datei neben der DB verhindert
   einen zweiten Start. Sonst würde die Recovery der zweiten Instanz die
   laufenden Uploads der ersten für abgebrochen halten.
8. **Unmögliche Zustände sind nicht speicherbar.** Wo es geht, erzwingt die DB
   selbst die Regeln (CHECK-Constraints, Trigger, erlaubte Zustandsübergänge),
   nicht nur der Python-Code.

## Ordner- und Album-Modell

- **Album-Ordner:** Ein Ordner, dem der Nutzer ein Album zuweist, z. B.
  `2026-06 Portugal`. Der Albumtitel wird mit dem Ordnernamen vorbelegt, ist
  aber **jederzeit manuell änderbar** (siehe "Albumnamen manuell anpassen").
- **Der Elternordner ist führend.** Unterhalb eines Album-Ordners kann kein
  weiterer Album-Ordner liegen, die UI verhindert das. Sonderfälle (Unterordner
  in ein anderes Album) werden manuell außerhalb des Tools erledigt.
- **Angehakte Ordner:** Innerhalb eines Album-Ordners hakt der Nutzer an, welche
  Ordner hochgeladen werden: der Album-Ordner selbst und/oder beliebige
  Unterordner. Hochgeladen werden nur die Dateien *direkt* in angehakten Ordnern,
  es gibt kein implizites Mitnehmen von Unterordnern.
  Beispiel: `2026-06 Portugal/Jpegs/` angehakt, `Videos/` und `RAWs/` nicht →
  nur die JPEGs landen im Album "2026-06 Portugal".
- Anhaken ist nur innerhalb eines Album-Ordners möglich. Neu auftauchende
  Unterordner sind nicht automatisch angehakt.
- Mehrere Album-Ordner dürfen bewusst auf dasselbe Album zeigen (z. B. dieselbe
  Reise auf zwei Platten).
- **Erledigt-Markierung:** Die UI leitet den Status jedes Ordners aus der DB ab
  und zeigt ihn im Baum an (vollständig / teilweise / Fehler). Es werden
  **keine Marker-Dateien** in die Foto-Ordner geschrieben (Prinzip 6). Solche
  Dateien würden außerdem beim Kopieren von Ordnern mitwandern und dann falsche
  Aussagen machen. Die dauerhafte Markierung sitzt stattdessen am Foto auf Flickr
  (Hash-Tag, siehe unten), damit lässt sich der Zustand sogar nach DB-Verlust
  rekonstruieren.

## Scanner

`scanner.py` erfasst Ordner und Mediendateien unter einem Root mit Größe und
Änderungszeit, ohne Dateien zu öffnen. Beim nächsten Scan gleicht er ab:

- **Neu:** Datei bekommt `new` oder direkt `skipped_*` (RAW, nicht unterstützt,
  zu groß). Begleit- und Systemdateien werden gar nicht erfasst.
- **Geändert** (Größe oder mtime anders): Hash, EXIF-Daten und Pre-Check-Treffer
  werden verworfen, der Pre-Check des Ordners läuft neu.
- **Neu prüfbar:** Kommt in einem schon geprüften Ordner eine Datei hinzu, die
  der Pre-Check vergleichen würde (neu, wieder da, oder ein Format, das jetzt
  unterstützt wird), muss der Ordner neu geprüft werden. Sonst würde sie
  ungeprüft hochgeladen.
- **Verschwunden:** Datei wird `missing`, Ordner `missing = 1`. Die Zeilen
  bleiben samt Album-Zuordnung erhalten. Taucht der Ordner wieder auf, geht es
  nahtlos weiter.
- **Groß-/Kleinschreibung** wird wie unter Windows ignoriert: Ein Umbenennen von
  `portugal` in `Portugal` behält Ordner und Album.

Sicherheitsregeln, damit nie fälschlich etwas als gelöscht gilt:

- Ist der Root nicht erreichbar (Platte abgesteckt), bricht der Scan ab, ohne
  etwas zu ändern.
- Ein Ordner, der nicht lesbar ist, bleibt samt allem darunter unverändert
  (plus Warnung im Log).
- "Verschwunden" wird erst nach einem **vollständigen** Durchlauf gesetzt. Ein
  abgebrochener Scan behält seinen Fortschritt, markiert aber nichts.
- Verknüpfungen (Symlinks, Junctions) werden nicht verfolgt, sie können zurück
  in den Baum zeigen (Endlosschleife) oder aus ihm heraus. Eine Junction meldet
  sich als normaler Ordner und ist nur an ihrem Reparse-Tag erkennbar.
- `$RECYCLE.BIN` und `System Volume Information` werden übersprungen.
- Roots dürfen sich nicht überlappen. Ändert sich der Laufwerksbuchstabe, wird
  der Root per `relocate_root()` umgehängt, alle IDs bleiben erhalten.

Jeder Ordner wird in einer eigenen Transaktion geschrieben (Fortschritt bleibt
bei Abbruch erhalten, die UI sieht ihn in der DB). Ordner-Statistiken werden
bei jeder Abfrage aus den Datei-Zeilen berechnet (`folder_summaries()`), nicht
gespeichert, damit sie nie von den Daten abweichen. Gemessen: ca. 57.000
Dateien/s beim ersten Scan und 187.000/s beim erneuten Scan (lokale SSD); bei
HDD/NAS bestimmt das Auflisten der Ordner die Dauer.

**Lange Pfade:** Jeder Dateizugriff läuft über `config.filesystem_path()`, also
in der Form `\\?\C:\…`. Damit funktionieren Pfade über 260 Zeichen unabhängig
von der Windows-Einstellung `LongPathsEnabled` (auf dem Zielsystem aktiv, aber
nicht auf jedem PC). In DB und Anzeige stehen normale Pfade.

## Album-Abgleich

Vor jedem Upload-Lauf wird die komplette Albenliste frisch per
`flickr.photosets.getList` geholt und in `albums` gespiegelt. Geplante Titel
werden normalisiert verglichen (Unicode NFC, Leerzeichen getrimmt und
zusammengefasst, Groß-/Kleinschreibung ignoriert):

| Ampel | Bedeutung | Verhalten |
|---|---|---|
| grün | genau ein Album mit exakt gleichem Titel | wird verwendet |
| gelb | Treffer nur nach Normalisierung, oder mehrere Alben mit gleichem Titel | Nutzer muss wählen (vorhandenes Album oder Titel ändern), bis dahin kein Upload für diesen Ordner |
| blau | kein Treffer | neues Album wird angelegt |

Die Albenliste lässt sich als Text prüfen: heute per `scripts/export_albums.py`
(CSV), im Tool über die Sidebar plus Export-Funktion.

### Albumnamen manuell anpassen

Der Vorschlag aus dem Ordnernamen ist nur der Standard. Der Nutzer kann jederzeit
eingreifen:

1. **Titel eines geplanten Albums ändern.** Das ist rein lokal, bis das Album
   angelegt wird. Danach läuft der Abgleich (Ampel) sofort neu.
2. **Bestehendes Flickr-Album direkt wählen.** Statt über den Titel wird ein
   vorhandenes Album aus der Liste zugeordnet, z. B. wenn es auf Flickr anders
   heißt als der Ordner.
3. **Gelbe Ampel bewusst auflösen.** "Trotzdem neues Album anlegen" wird als
   Entscheidung gespeichert (`albums.title_confirmed`), damit die Warnung nicht
   bei jedem Lauf wiederkommt.
4. **Bestehendes Flickr-Album umbenennen.** Per `photosets.editMeta`, nach
   Bestätigung durch den Nutzer. Die Beschreibung wird unverändert mitgeschickt,
   weil die Flickr-Doku offenlässt, ob sie sonst gelöscht wird. Ablauf nach
   Prinzip 2: neuer Titel zuerst als `pending_title` gespeichert, dann
   `editMeta`, dann `title` übernommen. Bricht es dazwischen ab, wird die
   Umbenennung beim nächsten Start einfach wiederholt (gleicher Titel nochmal
   setzen ist harmlos).

## Abgleich mit bereits hochgeladenen Fotos (Pre-Check)

Viele Fotos liegen schon auf Flickr, hochgeladen ohne dieses Tool und damit ohne
Hash-Tag. Damit sie nicht doppelt hochgeladen werden, läuft ein Abgleich, **aber
nur für Ordner, die der Nutzer anhakt**, nicht für das ganze Archiv.

- **Auslöser:** Anhaken eines Ordners. Der Abgleich läuft im Hintergrund, das
  Ergebnis landet in der DB (`folders.prechecked_at`, `precheck_matches`).
- **Lokal:** Pro Datei werden Aufnahmedatum (EXIF `DateTimeOriginal`, sonst
  `DateTime`) und Pixelmaße gelesen, mit Pillow. Ungültige Datumswerte wie
  `0000:00:00` (Kamera ohne gestellte Uhr) gelten als "ohne Datum".
- **Flickr:** Eine Suche über die eigenen Fotos im Aufnahmezeitraum des Ordners:
  `flickr.photos.search(user_id="me", min_taken_date, max_taken_date,
  extras="date_taken,o_dims,machine_tags")`. Bei einem typischen Ordner (eine
  Reise, ein Monat) sind das 1–2 Aufrufe. Gesucht wird im ganzen Fotostream,
  nicht nur im Zielalbum, weil alte Fotos oft in keinem oder einem anders
  benannten Album liegen.
- **Treffer-Stufen:**
  - *sicher:* gleiches Aufnahmedatum (auf die Sekunde) + Flickr-Titel =
    Dateiname ohne Endung + gleiche Pixelmaße (auch um 90° gedreht, weil
    Hochformat je nach EXIF-Ausrichtung vertauscht gemeldet werden kann)
  - *wahrscheinlich:* gleiches Aufnahmedatum + (Titel **oder** Pixelmaße)
  - Nur gleiche Sekunde ohne Titel und Maße ist **kein** Treffer (z. B. zwei
    Kameras, gleicher Moment).
  - Dateien ohne EXIF-Datum (Videos, Screenshots): *wahrscheinlich* nur bei
    gleichem Titel innerhalb des Ordner-Zeitraums (± 1 Tag). Hat der Ordner
    gar keine datierten Dateien, ist keine Suche möglich; der Bericht weist
    solche Dateien als "ohne Aufnahmedatum" aus.
  - Fotos, deren Flickr-ID das Tool schon kennt (eigene Uploads), sind nie
    Treffer.
- **Umsetzung** (`precheck.py`, Pillow liest nur den Dateikopf, keine Pixel):
  Erst werden alle Metadaten gelesen, dann in einer kurzen Transaktion
  gespeichert. Die DB-Schreibsperre während des Lesens zu halten, würde
  Uploader und UI minutenlang aussperren. Gelesene Metadaten bleiben erhalten,
  auch bei Abbruch oder Flickr-Fehler. Der Ordner gilt aber erst als geprüft,
  wenn die Treffer gespeichert sind. Liefert Flickr mehr als 4000 Treffer,
  wird der Zeitraum halbiert, bis jede Suche vollständig ist (Grenzen
  lückenlos, ohne Überlappung).
- **Erneuter Abgleich:** Offene Vorschläge werden ersetzt, Entscheidungen des
  Nutzers bleiben. Ändert sich eine Datei, verwirft der Scanner ihre Treffer,
  und der Ordner muss neu geprüft werden.
- **Echtdaten-Test:** `scripts/precheck_probe.py "<Ordner>"` prüft einen
  echten Ordner rein lesend (temporäre DB, nichts auf Flickr geändert).
  Ergebnis am 2026-10-12: Ein Ordner mit 47 Fotos, die alle schon auf Flickr
  lagen, ergab 47 "sicher". Ein Ordner mit 49 nie hochgeladenen Fotos ergab
  keinen einzigen Treffer. Noch nicht mit echten Daten geprüft: Ordner mit
  Videos oder Dateien ohne EXIF-Datum.
- **Entscheidung durch den Nutzer**, pro Datei oder gesammelt pro Ordner
  (`choose_existing` / `choose_upload` / `reset_choice`; eine Datei entspricht
  genau einem Flickr-Foto, die übrigen Kandidaten werden dabei "anderes Foto"):
  - *"Ist dasselbe Foto"* → das Flickr-Foto wird **übernommen** (adoptiert):
    Datei hashen, `photos`-Zeile mit dieser Flickr-ID und `origin='adopted'`,
    nachträglich den Hash-Tag setzen (`flickr.photos.addTags`, harmlos
    wiederholbar), dann wie gewohnt ins Zielalbum einsortieren. So landen auch
    alte Fotos im richtigen Album, ohne neuen Upload.
  - *"Ist ein anderes Foto"* → normaler Upload.
- **Sperre:** Eine Datei mit offenem Treffer wird nicht hochgeladen, bis
  entschieden ist. Ein angehakter Ordner ohne abgeschlossenen Abgleich ebenfalls
  nicht.
- Bei künftigen Läufen sind adoptierte Fotos über den Hash-Tag eindeutig
  erkennbar, der Abgleich muss für sie nicht erneut laufen.

## Modulstruktur

```
flickr-uploader/
├── README.md
├── docs/
│   ├── PROJECT_SPEC.md              # dieses Dokument
│   └── RELEASE_CHECKLIST.md         # Pflicht-Checks vor Veröffentlichung
├── src/
│   └── flickr_uploader/
│       ├── __init__.py
│       ├── __main__.py              # Einstieg: python -m flickr_uploader
│       ├── config.py                # Pfade, Flickr-Limits, Dateitypen
│       ├── auth.py                  # OAuth-Flow, Token-Speicherung (keyring)
│       ├── db.py                    # SQLite-Zugriff, Schema, Migrationen, Backup
│       ├── scanner.py               # Ordner-Scanning, Anzahl/Größe je Ordner
│       ├── hashing.py               # XXH3-128 über Dateiinhalt
│       ├── precheck.py              # Abgleich mit alten Fotos (EXIF + Flickr-Suche)
│       ├── flickr_client.py         # einzige Stelle, die flickrapi benutzt
│       ├── albums.py                # Album-Ordner, Ampel, Abgleich, Albumanlage
│       ├── uploader.py              # Upload-Lauf: Zustandsautomaten, Recovery
│       ├── downloader.py            # Alben herunterladen (Filter je Album)
│       ├── filedates.py             # Aufnahmedatum in geladene Dateien schreiben
│       ├── jobs.py                  # ein Auftrag zur Zeit, Lauf + Fortschritt in der DB
│       ├── settings.py              # Einstellungen in der DB: Schlüssel, Vorgaben, Prüfung
│       └── gui/
│           ├── app.py               # pywebview-Fenster starten
│           ├── api.py               # Python-API fürs Frontend (nur über db.py)
│           └── web/
│               ├── index.html
│               ├── app.js
│               ├── style.css        # eigenes Stylesheet (Farb-Variablen, Flickr-Farben)
│               ├── views/
│               │   ├── ui.js                # Helfer: Elemente (Text nie als HTML), Größen
│               │   ├── folder_browser.js    # Baum mit Checkboxen + Stats
│               │   ├── album_assignment.js  # Album-Ordner + Titel
│               │   ├── album_overview.js    # Reiter "Alben": Album-Ordner + Flickr-Alben
│               │   ├── steps.js             # Schrittleiste: was als Nächstes dran ist
│               │   ├── download_view.js     # Alben herunterladen (Filter, Ziel)
│               │   ├── decisions.js         # "Ist dasselbe Foto" / "Trotzdem hochladen"
│               │   ├── settings_view.js     # Reiter "Einstellungen"
│               │   └── progress_view.js     # Upload-Karte: Plan, Fortschritt, Pause, Protokoll
│               └── vendor/          # fremde Dateien mit Lizenz (derzeit keine)
├── scripts/
│   ├── export_albums.py             # Stand-alone: bestehende Alben exportieren
│   ├── rename_albums.py             # Stand-alone: Umbenennung anhand CSV
│   ├── flickr_smoke_test.py         # Echttest: Annahmen über die Flickr-API
│   ├── precheck_probe.py            # Echttest: Pre-Check an einem Ordner (nur lesend)
│   ├── upload_probe.py              # Echttest: Upload-Lauf mit eigener Test-DB
│   └── download_probe.py            # Echttest: Alben herunterladen (temporäre DB)
├── tests/
├── .github/workflows/ci.yml         # Lint, Tests, Secret-Scan
├── .gitattributes                   # Zeilenenden (LF im Repo)
├── .gitignore
├── .gitleaks.toml                   # Secret-Scan-Regeln (inkl. Flickr)
├── .pre-commit-config.yaml          # Git-Hooks
├── pyproject.toml                   # Tool-Konfiguration (ruff, pytest)
├── requirements.txt                 # Laufzeit-Abhängigkeiten, gepinnt
└── requirements-dev.txt             # + pre-commit, pytest, ruff
```

## DB-Schema (SQLite)

Maßgeblich ist das Schema in `db.py`. Die Blöcke unten (v1 und die
Migrationen) sind eine Kopie zur Übersicht, ein Test (`tests/test_db.py`)
stellt sicher, dass beide übereinstimmen.

- **Speicherort:** `%LOCALAPPDATA%\flickr-uploader\state.db`, also außerhalb des
  Repos und auf einer lokalen Platte (WAL funktioniert nicht auf Netzlaufwerken).
  Für ein getrenntes Testprofil kann `FLICKR_UPLOADER_HOME` auf einen anderen
  Ordner zeigen. `config.py` lehnt dabei Netzlaufwerke und Ordner innerhalb eines
  Git-Repos ab.
- **Beim Öffnen:** Lock-Datei `state.db.lock` exklusiv sperren (Prinzip 7),
  `PRAGMA quick_check` (Beschädigung früh erkennen), Migrationen anwenden. Vor
  einer Migration wird automatisch ein Backup angelegt. Ist die DB neuer als
  das Programm, wird sie nicht angefasst.
- **Backup:** zu Beginn jedes Laufs per SQLite-Backup-API nach
  `...\backups\state-<Zeitstempel>.db`, geprüft per `quick_check`, die letzten
  10 bleiben.
- **Pro Verbindung:** `foreign_keys = ON`, `synchronous = FULL` (übersteht auch
  Stromausfall, nicht nur Programmabsturz), Busy-Timeout 5 s. `journal_mode =
  WAL` wird einmalig gesetzt und bleibt in der Datei gespeichert.
- **Tabellen sind `STRICT`:** SQLite prüft dann die Spaltentypen, statt z. B.
  Text in einer INTEGER-Spalte stillschweigend zu speichern. Braucht SQLite ≥
  3.37, was `db.py` beim Öffnen prüft.
- **Transaktionen explizit:** Verbindungen laufen im Autocommit-Modus, jede
  einzelne Änderung ist sofort committet. Mehrteilige Änderungen laufen in
  `BEGIN IMMEDIATE … COMMIT` (Schreibsperre von Anfang an, keine
  Deadlock-Upgrades zwischen Threads).
- **Schema-Änderungen:** Bis zum ersten echten Einsatz (erste DB auf dem
  Nutzer-PC) darf Schema v1 noch direkt angepasst werden. Ab dann nur noch per
  Migration (v2, v3, …), vorhandene Versionen werden nie mehr verändert.
- Schema-Version über `PRAGMA user_version`. Zeitstempel als ISO-8601-String in
  UTC (die Standard-Datetime-Adapter von `sqlite3` sind seit Python 3.12
  deprecated). Ordnerpfade relativ mit `/` als Trenner.
- Jede SQLite-Verbindung gehört genau einem Thread und ist kurzlebig.

```sql
-- Ausgangsordner, z. B. D:\Bilder. Alle Ordnerpfade sind relativ dazu,
-- damit ein geänderter Laufwerksbuchstabe nur hier angepasst werden muss.
CREATE TABLE roots (
    id          INTEGER PRIMARY KEY,
    path        TEXT NOT NULL UNIQUE,
    scanned_at  TEXT                               -- letzter vollständiger Scan
) STRICT;

-- Ein Foto auf Flickr, identifiziert über den Dateiinhalt.
-- Gleiche Datei in mehreren Ordnern = ein Foto, ein Upload.
CREATE TABLE photos (
    hash               TEXT NOT NULL PRIMARY KEY,  -- XXH3-128, hex
    state              TEXT NOT NULL CHECK (state IN
                         ('pending', 'uploading', 'uploaded', 'verified',
                          'unclear', 'failed')),
    origin             TEXT NOT NULL DEFAULT 'upload'
                         CHECK (origin IN ('upload', 'adopted')),  -- adopted = altes Foto übernommen
    flickr_id          TEXT UNIQUE,
    hash_tagged        INTEGER NOT NULL DEFAULT 0 CHECK (hash_tagged IN (0, 1)),
    attempts           INTEGER NOT NULL DEFAULT 0,
    upload_started_at  TEXT,
    verified_at        TEXT,
    last_error         TEXT,
    updated_at         TEXT NOT NULL,
    -- hochgeladen/verifiziert heißt immer: Flickr-ID bekannt; davor nie
    CHECK (
        (state IN ('uploaded', 'verified') AND flickr_id IS NOT NULL)
        OR (state IN ('pending', 'uploading', 'unclear') AND flickr_id IS NULL)
        OR state = 'failed'
    )
) STRICT;
CREATE INDEX idx_photos_state ON photos(state);

-- Alle Alben: bestehende von Flickr und vom Nutzer geplante.
CREATE TABLE albums (
    id                  INTEGER PRIMARY KEY,
    title               TEXT NOT NULL,             -- bewusst nicht UNIQUE (Flickr erlaubt Dubletten)
    description         TEXT,                      -- von Flickr, wird beim Umbenennen mitgeschickt
    pending_title       TEXT,                      -- Umbenennung beauftragt, noch nicht bestätigt
    title_confirmed     INTEGER NOT NULL DEFAULT 0 CHECK (title_confirmed IN (0, 1)),
    flickr_id           TEXT UNIQUE,               -- NULL, solange nicht auf Flickr
    state               TEXT NOT NULL CHECK (state IN ('planned', 'creating', 'exists')),
    primary_photo_hash  TEXT REFERENCES photos(hash),  -- vor photosets.create gesetzt
    created_by_tool     INTEGER NOT NULL DEFAULT 0 CHECK (created_by_tool IN (0, 1)),
    last_synced         TEXT,
    photo_count         INTEGER,                   -- laut Albenliste von Flickr, letzter Abgleich
    video_count         INTEGER,
    updated_at          TEXT NOT NULL,
    CHECK ((state = 'exists') = (flickr_id IS NOT NULL)),
    CHECK (state != 'creating' OR primary_photo_hash IS NOT NULL)
) STRICT;

CREATE TABLE folders (
    id             INTEGER PRIMARY KEY,
    root_id        INTEGER NOT NULL REFERENCES roots(id),
    rel_path       TEXT NOT NULL,                  -- '' = der Root selbst, Trenner '/'
    album_id       INTEGER REFERENCES albums(id),  -- gesetzt = Album-Ordner
    included       INTEGER NOT NULL DEFAULT 0 CHECK (included IN (0, 1)),
    missing        INTEGER NOT NULL DEFAULT 0 CHECK (missing IN (0, 1)),  -- nicht mehr auf der Platte
    prechecked_at  TEXT,                           -- letzter Abgleich mit alten Fotos
    UNIQUE (root_id, rel_path)
) STRICT;
CREATE INDEX idx_folders_album ON folders(album_id);

-- Elternordner ist führend: kein Album-Ordner über oder unter einem anderen.
CREATE TRIGGER folders_no_nested_album_insert
BEFORE INSERT ON folders
WHEN NEW.album_id IS NOT NULL
BEGIN
    SELECT RAISE(ABORT, 'nested album folder')
    WHERE EXISTS (
        SELECT 1 FROM folders f
        WHERE f.root_id = NEW.root_id
          AND f.album_id IS NOT NULL
          AND (f.rel_path = '' OR NEW.rel_path = ''
               OR substr(NEW.rel_path, 1, length(f.rel_path) + 1) = f.rel_path || '/'
               OR substr(f.rel_path, 1, length(NEW.rel_path) + 1) = NEW.rel_path || '/')
    );
END;

CREATE TRIGGER folders_no_nested_album_update
BEFORE UPDATE OF album_id ON folders
WHEN NEW.album_id IS NOT NULL
BEGIN
    SELECT RAISE(ABORT, 'nested album folder')
    WHERE EXISTS (
        SELECT 1 FROM folders f
        WHERE f.root_id = NEW.root_id
          AND f.id != NEW.id
          AND f.album_id IS NOT NULL
          AND (f.rel_path = '' OR NEW.rel_path = ''
               OR substr(NEW.rel_path, 1, length(f.rel_path) + 1) = f.rel_path || '/'
               OR substr(f.rel_path, 1, length(NEW.rel_path) + 1) = NEW.rel_path || '/')
    );
END;

-- Eine lokale Datei. Der Upload-Zustand steht in photos, nicht hier.
CREATE TABLE files (
    id          INTEGER PRIMARY KEY,
    folder_id   INTEGER NOT NULL REFERENCES folders(id),
    name        TEXT NOT NULL,
    size_bytes  INTEGER NOT NULL,
    mtime_ns    INTEGER NOT NULL,                  -- Größe/mtime geändert → neu hashen
    hash        TEXT REFERENCES photos(hash),      -- NULL bis gehasht
    status      TEXT NOT NULL CHECK (status IN
                  ('new', 'hashed', 'skipped_raw', 'skipped_too_large',
                   'skipped_unsupported', 'unreadable', 'missing')),
    taken_at    TEXT,                              -- EXIF DateTimeOriginal (Ortszeit wie Flickr)
    width       INTEGER,
    height      INTEGER,
    last_error  TEXT,
    updated_at  TEXT NOT NULL,
    UNIQUE (folder_id, name),
    CHECK (status != 'hashed' OR hash IS NOT NULL)
) STRICT;
CREATE INDEX idx_files_hash ON files(hash);

-- Pre-Check: mögliche Treffer unter den alten Flickr-Fotos.
CREATE TABLE precheck_matches (
    file_id     INTEGER NOT NULL REFERENCES files(id),
    flickr_id   TEXT NOT NULL,
    confidence  TEXT NOT NULL CHECK (confidence IN ('certain', 'probable')),
    decision    TEXT NOT NULL DEFAULT 'open'
                  CHECK (decision IN ('open', 'same', 'different')),
    checked_at  TEXT NOT NULL,
    decided_at  TEXT,
    PRIMARY KEY (file_id, flickr_id)
) STRICT;

-- Welches Foto in welches Album gehört (n:m).
CREATE TABLE album_photos (
    album_id    INTEGER NOT NULL REFERENCES albums(id),
    photo_hash  TEXT NOT NULL REFERENCES photos(hash),
    state       TEXT NOT NULL CHECK (state IN ('pending', 'added', 'verified')),
    updated_at  TEXT NOT NULL,
    PRIMARY KEY (album_id, photo_hash)
) STRICT;
CREATE INDEX idx_album_photos_photo ON album_photos(photo_hash);

-- Ein Upload- oder Download-Auftrag (immer nur einer); die UI verfolgt ihn hier.
CREATE TABLE runs (
    id           INTEGER PRIMARY KEY,
    kind         TEXT NOT NULL CHECK (kind IN ('upload', 'download')),
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    status       TEXT NOT NULL
                   CHECK (status IN ('running', 'completed', 'aborted', 'crashed')),
    items_total  INTEGER,                          -- bekannt, sobald die Arbeitsliste steht
    items_done   INTEGER NOT NULL DEFAULT 0,
    CHECK ((status = 'running') = (finished_at IS NULL))
) STRICT;

-- Live-Log und Abschlussbericht (die UI hält keinen eigenen Zustand).
CREATE TABLE events (
    id          INTEGER PRIMARY KEY,
    run_id      INTEGER REFERENCES runs(id),
    ts          TEXT NOT NULL,
    level       TEXT NOT NULL CHECK (level IN ('info', 'warning', 'error')),
    file_id     INTEGER REFERENCES files(id),
    photo_hash  TEXT REFERENCES photos(hash),
    message     TEXT NOT NULL
) STRICT;
CREATE INDEX idx_events_run ON events(run_id);
```

Migration v2 (2026-10-13, die erste echte Migration, siehe "Einstellungen" im
Abschnitt GUI):

```sql
-- Einstellungen der Oberfläche (Schlüssel -> Wert als JSON). Welche Schlüssel
-- es gibt und was sie bedeuten, legt settings.py fest. Nie Zugangsdaten.
CREATE TABLE settings (
    key         TEXT NOT NULL PRIMARY KEY,
    value       TEXT NOT NULL,                     -- JSON
    updated_at  TEXT NOT NULL
) STRICT;
```

## Zustandsautomaten

### Foto (`photos.state`)

```
pending ──(Absicht committet)──▶ uploading ──(Antwort mit ID, sofort committet)──▶ uploaded ──(getInfo ok)──▶ verified
   │                                │                                                 └──(Foto fehlt)──▶ failed
   │                                ├──(Timeout / Absturz)──▶ unclear ──(Suche per Hash-Tag)
   │                                │                           ├─ 1 Treffer  → uploaded (ID übernehmen)
   │                                │                           ├─ 0 Treffer  → pending (erst nach Wartezeit, s. Suchindex)
   │                                │                           └─ >1 Treffer → failed + Bericht (Duplikat, Nutzer entscheidet)
   │                                └──(sicher nichts gesendet)──▶ pending
   └──(Pre-Check: Nutzer bestätigt "dasselbe Foto")──▶ verified (origin = adopted)

failed ──(Nutzer setzt zurück, Flickr-ID wird geleert)──▶ pending
```

Erlaubte Übergänge sind in `db.py` hinterlegt, jeder andere wird abgelehnt.
Die Kopplung von Zustand und Flickr-ID erzwingt die DB zusätzlich per CHECK.
`uploading → pending` gibt es nur, wenn sicher nichts bei Flickr angekommen ist:
Die Datei ließ sich nicht öffnen, der Lauf wurde beim Warten auf das Rate-Limit
abgebrochen, oder Flickr hat die Anmeldung abgelehnt. Sonst müsste so ein Foto
unnötig als `unclear` auf den Suchindex warten.

- Jedes Foto wird mit dem Machine-Tag `flickruploader:hash=<xxh3-128-hex>`
  hochgeladen. Das ist der dauerhafte Marker auf Flickr-Seite. Der Tag ist am
  Foto sichtbar, verrät aber nichts außer einem Prüfwert.
- Die Suche läuft per `flickr.photos.search(user_id="me", machine_tags=...)`.
  Zur vollständigen Rekonstruktion (z. B. nach DB-Verlust) können alle eigenen
  Fotos seitenweise per `flickr.people.getPhotos(extras="machine_tags")` (500 pro
  Seite) abgeglichen werden.
- **Nur eigene Hash-Tags zählen.** Je nach Einstellung dürfen auch andere
  Flickr-Mitglieder Tags an fremde Fotos hängen. Ein Hash-Tag wird deshalb nur
  gewertet, wenn sein Autor der Nutzer selbst ist (getInfo liefert den Autor je
  Tag) und der Wert ein gültiger Digest ist (`hashing.is_valid_digest`).
- Verifizierung per `flickr.photos.getInfo`: ID existiert, gehört dem Nutzer und
  trägt den erwarteten Hash-Tag. Erst dann `verified`, und erst das gilt als
  hochgeladen.
- Direkt vor dem Hashen bzw. Upload wird die Datei erneut auf Größe/mtime
  geprüft. Hat sie sich seit dem Scan geändert, wird sie **nicht** hochgeladen,
  sondern wartet auf einen neuen Scan. Erst der verwirft Hash und
  Pre-Check-Treffer der alten Fassung, und der Abgleich läuft neu. Einfach neu
  zu hashen und hochzuladen würde den Pre-Check umgehen.

### Album (`albums.state`)

```
planned ──(primary_photo_hash + state=creating committet)──▶ creating ──(Antwort mit ID)──▶ exists
                                                                │
                                              (Recovery: kein Treffer)──▶ planned
exists ──(Album wurde auf Flickr gelöscht, beim Sync erkannt)──▶ planned
```

- `photosets.create` verlangt zwingend eine `primary_photo_id`, ein leeres Album
  kann auf Flickr nicht angelegt werden. Das Album entsteht deshalb erst, wenn
  sein erstes Foto `verified` ist, und zwar mit diesem als Titelbild.
- Albumanlage läuft serialisiert über einen globalen Lock (sonst drohen
  doppelte Alben).
- `creating` nach Absturz/Timeout: `getList` durchsuchen nach einem Album mit
  gleichem Titel **und** dem gemerkten Titelbild. Treffer → ID übernehmen, kein
  Treffer → zurück auf `planned`. Titel plus Titelbild identifiziert das Album
  eindeutig, auch wenn es Alben mit gleichem Titel gibt. Bekommt die Anlage
  mitten im Lauf keine Antwort, bleibt das Album `creating`, und seine Fotos
  warten. Am Ende des Laufs wird die Albenliste erneut geholt und abgeglichen,
  danach werden die Fotos einsortiert. Ein zweiter Anlageversuch "auf Verdacht"
  findet nie statt.
- **Umsetzung** (`albums.py`): `sync_with_flickr()` spiegelt die Albenliste,
  in dieser Reihenfolge: Alben in `creating` abgleichen, Liste übernehmen,
  gelöschte Alben behandeln, offene Umbenennungen senden, grüne geplante Alben
  mit dem vorhandenen Flickr-Album zusammenführen (Ordner und
  Album-Zuordnungen ziehen um). Fehlt ein Album in der Liste, gilt es erst als
  gelöscht, wenn `photosets.getInfo` das bestätigt (Fehlercode 1). Eine
  unvollständige Liste oder ein Netzfehler ändern nichts. Ein gelöschtes Album,
  auf das noch ein Ordner zeigt, wird wieder `planned`, seine Zuordnungen
  `pending`. Eines ohne Ordner verschwindet aus dem Spiegel.
- Mehrere Album-Ordner mit exakt gleichem Titel teilen sich ein Album
  (`assign_album()`). Zwei geplante Alben, deren Titel sich nur in
  Schreibweise oder Leerzeichen unterscheiden, sind beide gelb.

### Album-Zuordnung (`album_photos.state`)

- `pending` → `photosets.addPhoto` → `added`. Fehlercode 3 ("Photo already in
  set") zählt als Erfolg, damit ist ein Retry immer gefahrlos.
- Am Ende eines Laufs pro Album einmal `photosets.getPhotos` (500 pro Seite) →
  `verified`. Das kostet ein paar Aufrufe pro Album statt einen pro Foto.
- `added` → `pending`, wenn die Verifizierung das Foto nicht im Album findet.
  `verified` → `pending` nur, wenn das Album neu angelegt werden musste.

### Lauf (`runs.status`)

`running` → `completed` | `aborted` | `crashed`. Beim Programmstart werden
Läufe, die noch auf `running` stehen, als `crashed` markiert.

## Ablauf (High-Level)

0. **Programmstart / Recovery:** DB öffnen (Lock, Integritätsprüfung,
   Migration), DB-Backup anlegen. `photos` in `uploading` → `unclear`. `runs` in
   `running` → `crashed`. Alben in `creating` abgleichen (siehe oben), offene
   Umbenennungen (`pending_title`) erneut anwenden.
1. Nutzer öffnet Root-Ordner (z. B. "Bilder") → `scanner.py` baut den Baum mit
   Datei-Anzahl/Größe pro Ordner (noch ohne Hashing) und schreibt `folders`/`files`.
2. Nutzer legt Album-Ordner fest (Titel = Ordnername vorbelegt, manuell
   änderbar) und hakt darin die gewünschten Ordner an. Jeder angehakte Ordner
   startet im Hintergrund den Pre-Check gegen alte Flickr-Fotos.
3. Sidebar zeigt die Ampel aus dem Album-Abgleich und offene Pre-Check-Treffer.
   Gelbe Alben und offene Treffer blockieren den Upload der betroffenen
   Ordner/Dateien, bis der Nutzer entschieden hat.
4. Klick auf "Upload starten" → `uploader.py` im Hintergrund-Thread, pro Datei
   in angehakten Ordnern:
   - RAW? → `skipped_raw`. Zu groß? → `skipped_too_large`. Format nicht
     unterstützt? → `skipped_unsupported`. Jeweils mit Event.
   - Offener Pre-Check-Treffer? → überspringen, bleibt im Bericht.
   - Hash berechnen (bzw. übernehmen, wenn Größe/mtime unverändert) → `photos`-
     und `album_photos`-Zeile anlegen, falls nicht vorhanden.
   - Foto schon `verified`? → nur Album-Zuordnung.
   - Foto `unclear`? → erst Suche per Hash-Tag.
   - Sonst Upload nach Zustandsautomat, danach Album-Zuordnung bzw. Albumanlage.
   - Fehler → `last_error` + Event, weiter mit der nächsten Datei.
5. Fortschritt + Live-Log: Die UI fragt alle ~500 ms den Stand aus der DB ab.
   Abbruch ist jederzeit möglich: Das laufende Foto wird noch fertig bearbeitet
   bzw. landet beim nächsten Start als `unclear` in der Recovery.
6. Laufende: Album-Verifizierung per `getPhotos`, Abschlussbericht aus `events`
   und Status-Zählern.
7. Erneuter Start: Alles `verified` wird übersprungen, der Rest fortgesetzt.

**Umsetzung** (`uploader.run_upload()`, immer nur ein Lauf gleichzeitig):

- Zu Beginn: DB-Backup, `db.recover_interrupted()`, `albums.sync_with_flickr()`.
  Für angehakte Ordner ohne abgeschlossenen Pre-Check läuft er jetzt nach
  (z. B. weil Flickr beim Anhaken nicht erreichbar war). Treffer halten die
  Dateien wie gewohnt zurück.
- Pro Datei in einem angehakten Ordner: übersprungen (RAW usw.), wartend
  (gelbes Album, offener Pre-Check, kein Album-Ordner, seit dem Scan
  geändert) oder bearbeitet: hashen, hochladen bzw. übernehmen, per `getInfo`
  prüfen, ins Album. Das erste geprüfte Foto eines blauen Albums legt das
  Album an und wird sein Titelbild.
- Ein Upload ohne Antwort (`unclear`) wird ans Ende des Laufs gestellt. Dort
  wartet der Lauf die Frist für den Suchindex ab (abbrechbar) und sucht dann
  per Hash-Tag. So ist auch dieses Foto nach einem einzigen Lauf erledigt.
- **Verbindung:** Jeder einzelne Aufruf wurde vom Client schon wiederholt.
  Klappt es bei drei Dateien in Folge trotzdem nicht, endet der Lauf als
  `aborted`, statt hunderte Dateien als fehlgeschlagen zu melden. Eine
  abgelehnte Anmeldung beendet den Lauf sofort.
- Kommt ein Upload nicht privat an, wird er mit `make_private()` privat gemacht
  und erneut geprüft. Gelingt das nicht, wird er `failed` mit einer deutlichen
  Fehlermeldung und kommt in kein Album.
- Am Ende: noch offene Album-Zuordnungen nachholen, dann jedes berührte Album
  einmal per `getPhotos` prüfen (`added` → `verified`, fehlt das Foto →
  `pending`, wird im nächsten Lauf erneut hinzugefügt).
- Ein unerwarteter Fehler (Programmfehler) markiert den Lauf als `crashed` und
  wird weitergereicht. Alles bis dahin Committete bleibt gültig.
- **Echtdaten-Test:** `scripts/upload_probe.py "<Ordner>" ["<Albumtitel>"]`
  lädt einen echten Ordner hoch, mit eigener Test-DB unter
  `%LOCALAPPDATA%\flickr-uploader-probe`. Die echte DB des Tools entsteht dabei
  noch nicht. Erneut aufrufen zeigt, dass nichts doppelt hochgeladen wird.
  Strg+C testet den Abbruch. Ergebnis am 2026-10-12: Ein Ordner mit 115
  Dateien ergab 111 hochgeladen, verifiziert und ins neu angelegte Album
  einsortiert, 0 Fehler und 0 unklar. Die RAW-Datei wurde übersprungen. Die
  3 MTS-Videos gingen erst mit Dateiendung im Upload durch (siehe
  Flickr-Client).

## Download (Flickr → PC)

Ergänzung zum Upload, gewünscht am 2026-10-12: Inhalte eigener Flickr-Alben
auf den PC laden, statt sie über die Flickr-Webseite als ZIP zu holen. Vorbild
ist das lokale Hilfsskript, mit dem Videos samt Aufnahmedatum heruntergeladen
wurden.

- **Auswahl:** ein oder mehrere eigene Alben aus dem gespiegelten Albenbestand
  (Abgleich wie beim Upload per `albums.sync_with_flickr()`). **Jedes Album
  hat seinen eigenen Filter:** nur Videos, nur Fotos oder alles. Ein Download
  kann also z. B. aus Album A nur die Videos und aus Album B alles holen.
  Flickr filtert schon bei der Abfrage (`photosets.getPhotos` mit
  `media=videos|photos|all`).
- **Ziel:** ein Ordner, den der Nutzer wählt (nativer Dialog). Jedes Album,
  ob eines oder mehrere, bekommt darin einen eigenen Unterordner mit seinem
  Titel, Windows-tauglich gemacht. Nie landet etwas direkt im Zielordner.
  Heißen zwei Alben auf Flickr gleich, hängt am Ordnernamen die Album-ID. Die Dateinamen sind der Flickr-Titel (bei Uploads dieses Tools also
  der ursprüngliche Dateiname ohne Endung) plus die Endung der geladenen
  Datei. Fehlt der Titel, steht die Flickr-ID im Namen. Kommt ein Titel im
  Album mehrfach vor, wird die ID angehängt.
- **Qualität:** immer das Original, wenn Flickr es anbietet (nur für den
  Besitzer). Bei Fotos ist das `url_o`, das gleich mit `getPhotos` kommt, also
  ohne zusätzlichen Aufruf je Foto. Bei Videos ist es "Video Original" aus
  `photos.getSizes`, mit einem Aufruf je Video. Gibt es kein Original, wird die
  größte vorhandene Fassung geladen und das im Log vermerkt.
- **Aufnahmedatum** (Datum und Uhrzeit auf die Sekunde):
  - Originale behalten ihre Metadaten unverändert, bei Fotos also das EXIF,
    bei Videos den Header.
  - Von Flickr umgerechnete Videos haben kein Datum. Das Datum, das Flickr als
    "aufgenommen" führt, wird in den MP4/MOV-Header geschrieben (`mvhd`,
    `tkhd`, `mdhd`). Dabei ändern sich nur ein paar Bytes, ohne Neukodierung,
    und nur dort, wo die Datei noch kein Datum hat. Ein vorhandenes Datum wird
    nie überschrieben. Der Explorer zeigt es als "Medium erstellt".
  - Bei allen Dateien werden Erstell- und Änderungsdatum auf diesen Zeitpunkt
    gesetzt.
  - Flickr speichert die Zeit ohne Zeitzone. Sie wird als Ortszeit des PCs
    gelesen, der Explorer zeigt also genau die Uhrzeit, die Flickr zeigt.
  - Kennt Flickr das Datum nicht (`datetakenunknown`), wird nichts eingetragen.
    Flickr zeigt dann das Upload-Datum, und das wäre falsch.
  - Fotos ohne Original (umgerechnete JPEGs ohne EXIF) bekommen nur das
    Dateidatum. EXIF hineinzuschreiben ist nicht vorgesehen, siehe Nicht-Ziele.
- **Wiederholen:** Was im Album-Unterordner schon liegt (gleicher Name, egal
  welche Endung), wird übersprungen. Ein erneuter Download lädt nur, was fehlt,
  und trägt fehlende Daten auch in die vorhandenen Dateien nach. Ein Abbruch
  ist jederzeit möglich. Die angefangene Datei bleibt als `.part` liegen und
  wird beim nächsten Mal neu geladen.

**Sicherheiten:** Ein Download ändert auf Flickr nichts, er nutzt nur lesende
Aufrufe. Deshalb entfällt fast alles, was der Upload braucht: keine
Zustandsautomaten, kein "erst die Absicht schreiben", keine Prüfung per
`getInfo`, keine Duplikatsuche, keine Album-Sperre. Ein wiederholter Download
schadet nicht. Was bleibt, ist wenig, aber Pflicht:

1. **Nie ins Foto-Archiv.** Mit "Archiv" sind die Ausgangsordner (Roots)
   gemeint, die der Nutzer im Tool öffnet, um von dort hochzuladen, z. B.
   `D:\Bilder`. Der Zielordner darf weder in einem Root liegen noch einen
   enthalten, auch nicht `D:\` über `D:\Bilder`. Ordner, die das Tool nie
   gescannt hat, kennt es nicht und kann sie deshalb nicht schützen. Sonst schriebe das Tool ins Archiv (Prinzip 6), und
   der nächste Scan fände die geladenen Dateien als neue Fotos. Mit
   eingetragenem Datum haben sie einen anderen Hash, ein erneuter Upload wäre
   ein Duplikat.
2. **Nichts überschreiben, nichts löschen.** Vorhandene Dateien werden
   übersprungen. Geladen wird nach `<name>.<endung>.part`, und erst eine
   vollständige Datei (Größe gleich `Content-Length`) wird umbenannt.
3. **Nur Medien speichern.** Antwortet Flickr mit etwas anderem als einem Bild
   oder Video (z. B. einer Fehlerseite), wird nichts gespeichert.
4. **Header nur auffüllen** und nur in Dateien im Album-Unterordner des
   Downloads, nie ein vorhandenes Datum überschreiben.
5. **Keine Datei-URLs im Log.** Sie enthalten das Geheimnis des Fotos, mit dem
   jeder ein privates Original laden könnte. Gemeldet werden nur Titel,
   Fassung und HTTP-Status.
6. Wie überall: Dateizugriffe über `filesystem_path()`, Dateinamen
   Windows-tauglich (verbotene Zeichen, reservierte Namen, Länge).

**Umsetzung** (`downloader.run_download()` mit einer Liste von
`AlbumChoice(album_id, media)`, dazu `filedates.py` für das Datum und
`jobs.py` für das, was Upload und Download teilen):

- Läuft wie der Upload als Hintergrund-Auftrag. Es läuft immer nur ein Auftrag
  gleichzeitig, Upload oder Download (gemeinsame Sperre in `jobs.py`). So
  teilen sie sich auch nicht das API-Budget.
- Erst wird alles geprüft (Alben auf Flickr, Filter gültig, kein Album doppelt,
  Zielordner außerhalb des Archivs), dann startet der Lauf. Eine falsche
  Auswahl hinterlässt also keinen halben Lauf.
- Lauf und Log stehen in der DB wie beim Upload (`runs` mit `kind =
  'download'`, `events`). Die Fortschrittsanzeige liest damit wie überall nur
  aus der DB, und das Log übersteht einen Neustart. Eine Dateiliste führt der
  Download nicht in der DB: Was schon da ist, erkennt er am Zielordner.
- Flickr-Zugriff über den Client: Albuminhalt (`photosets.getPhotos` mit
  `media` und `extras=media,date_taken,original_format,url_o`), Fassungen
  (`photos.getSizes`, nur für Videos und Fotos ohne `url_o`) und das
  gestreamte Laden der Datei in 1-MiB-Blöcken, Abbruch je Block. Fehler wie
  beim Upload: Ist die Verbindung bei drei Dateien in Folge weg oder wird die
  Anmeldung abgelehnt, endet der Lauf. Andere Fehler kommen pro Datei ins Log,
  und es geht weiter.
- Bericht: geladen, schon vorhanden, ohne Original (größte Fassung), Datum
  eingetragen, ohne Datum auf Flickr, fehlgeschlagen.
- Das lokale Hilfsskript `download_album_videos.py` (nur Videos, nie im Repo)
  entfällt, sobald der Download im Tool läuft.
- **Echtdaten-Test:** `scripts/download_probe.py "<Ziel>" "<Album>=videos|photos|all" ...`
  lädt echte Alben herunter, mit einer temporären DB (`--list` zeigt die Alben
  mit Anzahl Fotos und Videos). Erneut aufrufen zeigt, dass nur Fehlendes
  geladen wird. Ergebnis am 2026-10-12: 2 Alben, 12 Dateien, alle 12 als
  Original geladen und mit Aufnahmedatum, 0 Fehler. Der Weg für umgerechnete
  Videos (Datum in den Header schreiben) kam dabei nicht vor. Er ist mit
  synthetischen Dateien getestet und lief schon im Hilfsskript an echten Videos.

**DB-Änderungen** (im Schema v1, das noch direkt angepasst werden durfte):

- `runs.kind` (`upload` | `download`)
- `runs.items_total` und `runs.items_done` für den Fortschrittsbalken (beide
  Auftragsarten)
- `albums.photo_count` und `albums.video_count` aus `photosets.getList`, damit
  die Albumauswahl zeigt, wie viel Inhalt ein Album hat (Stand des letzten
  Abgleichs)

## Flickr-Client

`flickr_client.py` ist die einzige Stelle, die flickrapi benutzt. Er übersetzt
dessen gemischte Fehlersignale in vier klare Fälle:

| Fehler | Bedeutung | Reaktion |
|---|---|---|
| `FlickrUnavailableError` | Netzwerk weg, Flickr überlastet (HTTP 429/5xx, Code 105/106) | später erneut versuchen |
| `FlickrAuthError` | Anmeldung abgelehnt (HTTP 401, Codes 96–100) | Lauf stoppen, Setup neu |
| `FlickrRejectedError` | Flickr lehnt ab, mit Fehlercode | Wiederholen hilft nicht |
| `OutcomeUnknownError` | nicht wiederholbarer Aufruf ohne Antwort | erst nachsehen, dann ggf. wiederholen |

- **Automatisch wiederholt** (bis zu 3 Versuche, 2 s und 8 s Pause) werden nur
  Aufrufe, bei denen eine Wiederholung nichts kaputt machen kann: Lesen, Foto
  zum Album hinzufügen (Code 3 "schon drin" = Erfolg), Umbenennen, Tag setzen.
- **Nie automatisch wiederholt:** Upload und Album anlegen. Kommt dort keine
  Antwort, kann Flickr es trotzdem erledigt haben (`OutcomeUnknownError`). Der
  Uploader sucht dann per Hash-Tag bzw. Albenliste, bevor er es erneut
  versucht (siehe Zustandsautomaten).
- **Upload:** Antwortformat ausdrücklich XML (`format="etree"`), der
  Upload-Endpunkt kann kein JSON, flickrapi würde sonst an der Antwort
  scheitern. Die Datei öffnet der Client selbst (flickrapi schließt sie nie)
  über `filesystem_path()`. Ein lokaler Dateifehler ist kein "Ausgang unbekannt".
- **Dateiname beim Upload:** flickrapi schickt jede Datei als `dummy name`,
  ohne Endung, und Flickr muss den Typ dann am Inhalt erkennen. Bei MTS-Videos
  gelingt das nicht (Fehler 5, "Filetype was not recognised", beobachtet am
  2026-10-12). Die Flickr-Webseite nimmt sie dagegen an, weil sie den Namen
  mitschickt. Der Client ersetzt deshalb flickrapis `do_upload` (nur an der
  eigenen Instanz, sonst unverändert, der Name ist nicht Teil der
  OAuth-Signatur) und schickt `upload.<endung>` mit. Das ist ASCII, nur die
  Endung zählt, und der Titel geht ohnehin separat.
- **Sichtbarkeit: immer privat, ohne Option.** `upload()` hat keinen Parameter
  dafür und sendet immer `is_public=0, is_friend=0, is_family=0, hidden=2`
  (nur der Besitzer, aus der Suche verborgen). `verify_upload()` verlangt
  zusätzlich zu Besitzer und Hash-Tag, dass das Foto privat ist (`getInfo` →
  `visibility`; fehlt die Angabe, gilt es als nicht privat). Kommt ein eigener
  Upload doch nicht privat an, macht `make_private()` ihn privat
  (`setPerms` + `setSafetyLevel hidden=1`; Achtung: "verbergen" ist beim Upload
  `2`, bei `setSafetyLevel` `1`). Übernommene alte Fotos (Pre-Check) behalten
  ihre Sichtbarkeit, das Tool ändert fremde Entscheidungen nicht.
- **Rate-Limit:** Jeder Versuch geht durch einen Limiter mit gleitendem
  Fenster, Budget 3400 Aufrufe/Stunde (Reserve zu Flickrs 3600 für die
  Hilfsskripte und Neustarts). Ein Abbruch wird beim Warten binnen einer
  Sekunde bemerkt.
- **Suche:** Flickr liefert pro Suchanfrage höchstens 4000 Treffer. Sind es
  mehr, meldet der Client `SearchTooBroadError`, statt still unvollständig zu
  sein. Der Pre-Check muss den Zeitraum dann teilen.
- **Album prüfen:** `album_exists()` (`photosets.getInfo`) antwortet nur dann
  mit "nein", wenn Flickr "nicht gefunden" meldet. Jeder andere Fehler wird
  geworfen, damit nie aus einem Fehlschlag auf "gelöscht" geschlossen wird.
- **Keine Secrets in Meldungen:** Fehler werden ohne die ursprüngliche
  Exception weitergereicht (`from None`), weil deren Text die Request-URL mit
  dem API-Key enthalten kann.
- **Echttest:** `scripts/flickr_smoke_test.py` prüft die Annahmen gegen das
  echte Flickr (privates Testbild, Album, Hash-Tag-Autor, Umlaute, Suchindex).

## Flickr-Rahmenbedingungen

- **API-Limit:** 3600 Aufrufe pro Stunde und Key. Pro Foto fallen ca. 3 Aufrufe
  an (Upload, getInfo, addPhoto). Der Client zählt mit und drosselt. Beim
  Download kostet jedes Video einen Aufruf (`getSizes`), Fotos fast keinen
  (`url_o` kommt mit `getPhotos`, 500 pro Aufruf). Das Laden der Dateien
  selbst zählt nicht zum API-Limit.
- **Suchindex:** `photos.search` findet neue Fotos erst mit Verzögerung
  (gemessen am 2026-09-30: 26 s; Wartezeit daher
  `SEARCH_INDEX_GRACE_SECONDS` = 300 s). Ein
  `unclear` mit 0 Treffern wird deshalb erst nach einer Wartezeit wieder auf
  `pending` gesetzt.
- **Upload-Anforderungen** (Flickr-Hilfe "Flickr upload requirements", geprüft
  am 2026-09-30, als Konstanten in `config.py`):
  - Fotos: JPEG, PNG, GIF (nicht animiert), TIFF und BMP (werden von Flickr als
    JPEG gespeichert), bis 200 MB, Seitenverhältnis höchstens 31,25 : 1.
  - **HEIC/HEIF nimmt Flickr nur über die Flickr-App an, nicht über die API.**
    Solche Dateien werden wie RAW übersprungen + geloggt und müssen vorher am PC
    umgewandelt werden.
  - Videos: MP4, AVI, WMV, MOV, MPEG, 3GP, M2TS, MTS, OGG, OGV, bis 1 GB.
    MTS (AVCHD) ist dasselbe Format wie M2TS. Die Flickr-Hilfe nennt es nicht,
    über die Webseite nimmt Flickr es aber an (vom Nutzer bestätigt). Über die
    API klappt es nur mit Dateiendung im Upload (siehe Flickr-Client, mit
    echten MTS-Videos bestätigt am 2026-10-12). Beliebige
    Länge erlaubt, abgespielt werden bei Pro aber nur die ersten 10 Minuten
    (Free: 3). Die Länge ist in Phase 1 deshalb kein Ausschlussgrund.
  - "MB" ist bei Flickr nicht genau definiert. Gerechnet wird mit der kleineren
    Lesart (10⁶ Byte), damit kein Upload startet, den Flickr ablehnen würde.
- **Nicht gemeldete Dateien:** Begleitdateien ohne eigenes Bild (`.xmp`,
  `.aae`, `.thm`, …), Systemdateien (`Thumbs.db`, `desktop.ini`) und
  macOS-Ressourcendateien (`._*`) zählen weder als Foto noch als "nicht
  unterstützt". Sie tauchen im Bericht nicht auf.

## GUI

- **pywebview** (natives Fenster, rendert per Edge WebView2, bei Windows 11
  vorinstalliert) mit einem Frontend aus HTML/CSS/JS.
- **Look: modern und schlicht** (entschieden 2026-10-13, statt des
  ursprünglich geplanten Retro-Looks mit XP.css, der im Einsatz zu veraltet
  wirkte). Helle Flächen, klare Typografie, Farben angelehnt an Flickr: Blau
  `#0063DC` für Aktionen und Auswahl, Pink `#FF0084` nur sparsam als Akzent,
  Grau `#F3F5F6` als Hintergrund. Das Flickr-Logo wird bewusst nicht
  nachgebaut (Markenzeichen, das Repo ist öffentlich). Schrift ist die
  Systemschrift (Segoe UI), also keine Schriftdateien. Eigenes kleines
  Stylesheet mit Farb-Variablen statt eines CSS-Frameworks, Icons als kleines
  Inline-SVG. Native Windows-Titelleiste.
- **Einstellungen** kommen in die DB, nicht in eine eigene Datei. So gibt es
  nur eine Quelle der Wahrheit, und sie sind im Backup enthalten. Die Tabelle
  `settings` kam mit Migration v2 (Schritt 4). `settings.py` legt fest, welche
  Schlüssel es gibt, mit Vorgabe und Prüfung je Schlüssel. Ein fehlender,
  beschädigter oder veralteter Wert fällt auf die Vorgabe zurück, statt das
  Fenster zu stören. Derzeit: `download_target` (letzter Zielordner, nur über
  den Ordnerdialog gesetzt, der ihn prüft), `download_media` (Vorauswahl beim
  Anhaken: `all`/`photos`/`videos`), `sync_albums_at_start` (Albenliste beim
  Start holen, Vorgabe an). Zugangsdaten nie.
- **Alles lokal:** Alles, was die Seite braucht, liegt unter `gui/web/`.
  Fremde Dateien kämen mit Lizenz nach `gui/web/vendor/`, derzeit gibt es
  keine. Nichts wird aus dem Internet geladen. So ist das Tool
  offline-fähig, und keine fremde Webseite bekommt Zugriff auf die Python-API
  (`window.pywebview.api`).
- **Kein eigener Zustand im Frontend:** Jede Ansicht wird aus API-Aufrufen
  gerendert, die aus der DB lesen. Aktionen schreiben über die API sofort in die
  DB, danach wird neu gerendert. Der Fortschritt kommt per Polling aus der DB.
- **Threads:** pywebview führt API-Methoden in eigenen Threads aus, und Upload
  bzw. Download laufen in einem Hintergrund-Thread (immer nur einer). `db.py` gibt deshalb jedem Thread
  eine eigene SQLite-Verbindung.
- Ordnerauswahl über den nativen Windows-Dialog (`create_file_dialog`).
- **Download-Ansicht** (Reiter "Herunterladen", Schritt 3): durchsuchbare
  Liste der eigenen Alben mit Anzahl Fotos und Videos (Stand des letzten
  Abgleichs, aktualisiert beim Start oder im Reiter "Alben"), Mehrfachauswahl
  per Haken, **je ausgewähltem Album ein eigener Filter** (Alles / Fotos /
  Videos, jeweils mit Anzahl), Zielordner per Dialog. Ein Zielordner im oder
  über dem Archiv wird gleich bei der Auswahl abgelehnt, `start_download()`
  prüft Auswahl und Ziel noch einmal, bevor der Lauf startet. Fortschritt,
  Pausieren/Fortsetzen und Protokoll wie beim Upload (gemeinsame Bausteine),
  danach ein Ergebnis auf Deutsch und "Im Explorer zeigen". Upload und
  Download laufen nie gleichzeitig. Die Auswahl ist reine Formulareingabe der
  Seite, bis der Lauf startet. Der Zielordner ist eine Einstellung (seit
  Schritt 4): Der Dialog prüft und speichert ihn, `start_download()` nimmt ihn
  aus der DB. Gibt es ihn nicht mehr oder liegt er inzwischen im Archiv, zeigt
  die Seite das an und startet nicht. Beim Anhaken ist der Filter aus den
  Einstellungen vorausgewählt.
- **Geführter Ablauf** (seit 2026-10-13, weil der Ablauf ohne Erklärung nicht
  zu erschließen war): Über dem Ordnerbaum zeigt eine Schrittleiste die vier
  Schritte (Ordner öffnen, Album-Ordner festlegen, Ordner anhaken, Hochladen),
  was erledigt ist und was als Nächstes dran ist, mit einem Satz dazu.
- **Albenliste automatisch:** Beim Start holt die App die Albenliste von
  Flickr (wenn angemeldet), damit die Ampel stimmt, ohne dass man daran denken
  muss. Danach meldet sie, was das für die eigenen Album-Ordner heißt (z. B.
  "1 vorhanden, 1 neu"). Von Hand aktualisieren geht im Reiter "Alben". In
  den Einstellungen abschaltbar.
- **Reiter "Alben":** alle Album-Ordner mit Ampel und Zustand, dazu die
  durchsuchbare Liste der eigenen Flickr-Alben (Anzahl Fotos/Videos, von
  welchen Ordnern verwendet, Stand des letzten Abgleichs). Einem Album-Ordner
  kann im Detailbereich ein vorhandenes Flickr-Album zugewiesen werden, wenn
  es dort anders heißt.
- Geprüft am 2026-09-30: pywebview 6.2.1 läuft unter Python 3.13 auf dem
  Zielsystem (Renderer edgechromium, Aufrufe JS → Python funktionieren).
- **Start:** `python -m flickr_uploader` (`gui/app.py`): DB öffnen (Sperre,
  Prüfung, Migration), Recovery, Fenster. Läuft schon eine Instanz oder
  scheitert die DB, kommt ein Windows-Meldungsfenster. Die Seite liefert
  pywebviews eingebauter Server auf 127.0.0.1 aus. Eine Content-Security-Policy
  in `index.html` verbietet zusätzlich alles außer eigenen Dateien.
- **Python-API** (`gui/api.py`): Jede Methode öffnet ihre eigene kurze
  DB-Verbindung (pywebview ruft sie in eigenen Threads auf). Erwartbare Fehler
  kommen als `{"error": "<Text für den Nutzer>"}` zurück. Alle Flickr-Aufrufe
  der App teilen sich einen Rate-Limiter, jeder Aufruf hat aber eine eigene
  HTTP-Sitzung. Interne Attribute beginnen mit `_`, sonst gäbe pywebview sie an
  die Seite weiter (ein Test prüft die Liste der öffentlichen Methoden).
- **Sprache der Oberfläche:** Deutsch. Die Log-Meldungen der Module sind
  Englisch, wie der Code.
- **Upload in der GUI** (Schritt 2): Eine Karte über dem Ordnerbaum zeigt,
  was ein Lauf jetzt täte (`upload_plan()`: bereit, schon auf Flickr, wartet
  auf Entscheidung, übersprungen, Fehler), und startet ihn im Hintergrund.
  Während des Laufs fragt die Seite jede Sekunde `job()` ab: Fortschrittsbalken
  aus `runs.items_done/items_total`, letzte Meldung, Protokoll aus `events`.
  **Pausieren** heißt: nach dem aktuellen Foto anhalten (derselbe Abbruch wie
  bisher). **Fortsetzen** startet einfach einen neuen Lauf, der Erledigtes
  überspringt. Ein echtes Einfrieren mitten im Lauf gibt es bewusst nicht, es
  brächte nichts außer neuen Fehlerquellen. Schließt man das Fenster während
  eines Laufs, hält er nach dem aktuellen Foto an (bis zu 15 s Wartezeit), der
  Rest regelt die Recovery beim nächsten Start.
- **Entscheidungen** (Schritt 2): Im Detailbereich eines angehakten Ordners
  stehen die Dateien, die vielleicht schon auf Flickr sind, mit Einstufung
  (sicher/wahrscheinlich), Grund und "Auf Flickr ansehen" (öffnet den
  normalen Browser). Pro Datei "Ist dasselbe Foto" (wird übernommen) oder
  "Trotzdem hochladen", für den ganzen Ordner "Alle sicheren übernehmen" bzw.
  "Alle trotzdem hochladen". Vor dem Lauf lässt sich jede Entscheidung
  zurücknehmen.
- **Reiter "Einstellungen"** (Schritt 4): Flickr-Zugang (siehe unten),
  Vorauswahl-Filter und Zielordner fürs Herunterladen, Albenliste beim Start holen (an/aus), dazu Version, Ort
  der Datenbank und der Backups (jeweils "Im Explorer zeigen") und "Jetzt
  Backup anlegen" (geprüft, die neuesten 10 bleiben). Jede Änderung wird sofort
  gespeichert. Die Seite darf über `set_setting()` nur die Einstellungen ändern,
  die dafür freigegeben sind, den Zielordner nur über den Dialog.
- **Flickr-Zugang in der GUI** (Schritt 4, gewünscht, damit auch andere das
  Tool ohne Terminal einrichten können): dieselbe Anmeldung wie
  `auth setup`, in drei Schritten. (1) Knopf zur Flickr-Seite für einen
  eigenen API-Key, (2) Key und Secret in Passwortfelder, `start_login()` prüft
  sie bei Flickr und öffnet die Seite zum Erlauben im normalen Browser, (3) den
  Code von Flickr eintragen, `finish_login()` holt den Token und speichert erst
  jetzt Key, Secret und Token im Credential Manager. Keys und Token gehen nie
  zurück an die Seite (`account()` meldet nur Zustand, Kontoname und Rechte),
  nie in DB oder Log, Fehlermeldungen nennen nur die Fehlerart, nie den Text
  (Verbindungsfehler tragen die URL mit dem API-Key). Die Seite leert die
  Felder, sobald Python die Werte hat. Der halbfertige Login (Request-Token)
  liegt nur im Speicher des Prozesses. Dazu "Neu einrichten", "Abmelden"
  (mit Rückfrage, danach Angebot, die Flickr-Seite zum Widerrufen zu öffnen)
  und "Zugriff auf Flickr ansehen oder widerrufen". Die Seite kann nur die
  zwei festen Flickr-Seiten öffnen lassen, keine beliebige URL. Während eines
  Uploads oder Downloads ist Anmelden und Abmelden gesperrt. Das Terminal
  bleibt als zweiter Weg.
- **Eine Datenbank, ein Flickr-Konto:** Die DB merkt sich das Konto, mit dem
  sie zuerst benutzt wird (`flickr_account` in `settings`). Ihre Alben und
  Fotos gehören zu diesem Konto. Mit einem anderen Konto kämen Uploads in
  falsche Alben und Duplikate. Ist ein anderes Konto angemeldet und enthält die
  DB schon etwas von Flickr (Alben, Fotos oder Pre-Check-Treffer mit
  Flickr-ID), gilt man als nicht angemeldet, mit Hinweis, und eine Anmeldung
  mit dem anderen Konto in der GUI wird abgelehnt (die alte bleibt). Ist die DB
  noch leer, wechselt sie einfach zum neuen Konto. Für ein zweites Konto ein
  eigenes Profil verwenden (`FLICKR_UPLOADER_HOME`).
- **Stand am 2026-10-13, alle 4 Schritte fertig:** Schritt 2 brachte
  Entscheidungen, Upload starten/pausieren/fortsetzen, Fortschritt und
  Protokoll, Schritt 3 den Reiter "Herunterladen", Schritt 4 den Reiter
  "Einstellungen" mit dem Flickr-Zugang.
- **Testen während der Entwicklung:** mit eigenem Profil
  (`FLICKR_UPLOADER_HOME`). Der erste Start ohne Profil legt die echte DB an,
  ab dann ändert sich das Schema nur noch per Migration.

## Credentials und Schutz vor Secrets im Repo

- API Key/Secret + Access Token/Secret **nie in einer Datei**, weder im
  Projektordner noch sonst wo.
- Speicherung ausschließlich im Windows-Anmeldeinformationsspeicher (Credential
  Manager) über keyrings `WinVaultKeyring`. Der wird **explizit** gewählt, nicht
  über keyrings automatische Erkennung, die sonst auch ein installiertes
  Klartext-Backend nehmen könnte.
- **Kein Klartext-Fallback** (bewusste Abweichung von der ersten Planung): Auf
  Windows ist der Credential Manager immer da, und eine Datei wäre die
  schwächste Stelle.
- Zwei Einträge: `flickr-uploader/api-credentials` (Key + Secret) und
  `flickr-uploader/access-token`. Ein neuer API-Key löscht den alten Token
  (Tokens gehören zu einem Key).
- `auth.py` erzeugt flickrapi immer mit `store_token=False`, sonst legt flickrapi
  still eine Klartextkopie in `%USERPROFILE%\.flickr\oauth-tokens.sqlite` an.
- Angefordert wird die Berechtigung `write` (Upload, Alben, Tags), nie `delete`:
  Das Tool soll auf Flickr nichts löschen können.
- `repr()` der Credential-Objekte zeigt keine Secrets, Fehlermeldungen enthalten
  nie Werte. Verbindungsfehler werden ohne URL gemeldet, weil die den API-Key
  enthält.
- **Einrichtung** im Reiter "Einstellungen" der App (Abschnitt GUI,
  "Flickr-Zugang in der GUI") oder im eigenen Terminal (PowerShell):
  `.venv\Scripts\python.exe -m flickr_uploader.auth setup`. Key und Secret
  werden verdeckt eingegeben. Kann das Terminal die Eingabe nicht verbergen
  (z. B. Git Bash), bricht das Setup ab. Gespeichert wird erst nach
  erfolgreicher Anmeldung, ein abgebrochenes Setup hinterlässt nichts.
  Weitere Befehle: `status` (prüft die Anmeldung online), `logout`.
- Tests berühren nie den echten Credential Manager oder das Netz
  (`tests/conftest.py` lässt jeden Versuch fehlschlagen).

Schutzebenen, damit auch ein einzelner Fehler kein Secret öffentlich macht:

1. **Architektur:** Secrets in keyring, DB und Logs unter `%LOCALAPPDATA%`,
   also gar nicht erst im Projektordner.
2. **`.gitignore`:** Credential-, Token-, DB-, Foto-/Video-/RAW-Dateien und
   CSV-Exporte.
3. **pre-commit-Hooks** bei jedem Commit: gitleaks (Standardregeln plus eigene
   Flickr-Regeln in `.gitleaks.toml`), Erkennung privater Schlüssel, Sperrliste
   für Credential-/DB-Dateien und Fotos/Videos/RAWs (greift auch bei
   `git add -f`), max. 500 KB pro Datei.
4. **CI:** gitleaks scannt bei jedem Push die komplette History.
5. **Test:** `tests/test_repo_hygiene.py` schlägt fehl, wenn die `.gitignore`
   aufgeweicht wird.
6. **Vor der Veröffentlichung:** [RELEASE_CHECKLIST.md](RELEASE_CHECKLIST.md).

## Hashing-Entscheidung

xxHash (`xxhash`-Paket) statt SHA-256, konkret die Variante **XXH3-128**
(`xxhash.xxh3_128`). xxHash ist nicht kryptographisch, für Duplikat-Erkennung
ist das egal: Es geht nur um zufällige Kollisionen, und da ist ein 128-Bit-Hash
auch bei Millionen Dateien praktisch kollisionsfrei. (XXH64 wäre mit 64 Bit
knapper, XXH3-128 ist zudem schneller.) Die Variante muss von Anfang an feststehen,
weil der Hash in der DB und im Machine-Tag auf Flickr gespeichert wird: ein
späterer Wechsel macht alle gespeicherten Hashes wertlos.

Gemessen am 2026-09-30 auf dem Zielsystem (256 MB im Speicher, also reine
CPU-Zeit): XXH3-128 ca. 38 GB/s, XXH64 ca. 20 GB/s, SHA-256 ca. 2,5 GB/s. Der
Vorteil wirkt sich aber nur aus, wenn die Platte schnell genug liefert: Eine HDD
liefert 150–250 MB/s, beim 10TB-Initial-Scan limitiert dort die I/O, nicht der
Hash. Bei NVMe-SSDs wäre SHA-256 dagegen der Flaschenhals.

Umsetzung in `hashing.py`:

- Gelesen wird in 1-MiB-Blöcken, auch 1-GB-Videos belegen also kaum Speicher.
- Größe und Änderungszeit werden vor und nach dem Lesen am offenen Handle
  verglichen, dazu die Zahl der gelesenen Bytes. Ändert sich die Datei während
  des Hashens, wird der Digest verworfen (`FileChangedError`), statt einen Hash
  über einen Mischzustand zu speichern.
- Gespeichert werden Digest, Größe und mtime aus *demselben* Lesevorgang. Nur
  dann bedeutet "Größe und mtime unverändert" beim nächsten Lauf sicher "Hash
  noch gültig".
- Ein Abbruch wird pro Block geprüft, der Nutzer wartet also nicht auf das Ende
  eines großen Videos.
- Ein Test nagelt Algorithmus und Format fest: offizieller Testvektor
  (leere Eingabe → `99aa06d3014798d86001c324468d497f`) plus ein Referenzwert
  über zwei Blöcke.

## Offene Punkte

1. **flickrapi ist nicht mehr gepflegt.** Letztes Release 2.4.0 vom Februar 2018.
   Importiert unter Python 3.13 ohne Warnungen. Echte Aufrufe sind per
   Smoke-Test (2026-09-30), Pre-Check-Probe und Upload-Probe (beide
   2026-10-12) geprüft. Eine Eigenheit musste umgangen werden: Der Upload
   schickt sonst keinen Dateinamen mit. Der Zugriff ist komplett in
   `flickr_client.py` gekapselt, damit ein späterer Wechsel (z. B. direkt
   `requests-oauthlib`) nur ein Modul betrifft.
