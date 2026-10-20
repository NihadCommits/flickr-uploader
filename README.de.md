# Flickr Uploader

[English](README.md) · **Deutsch**

Eine Windows-Desktop-App, die ausgewählte lokale Ordner als Alben zu Flickr
hochlädt, ohne doppelte Fotos oder doppelte Alben, und deine Alben wieder auf
den PC herunterlädt.

![Die Ansicht „Hochladen“: Ordnerbaum, Album-Ordner mit angehakten Unterordnern, laufender Upload](docs/screenshot.png)

## Was das Tool macht

- **Aus Ordnern werden Alben.** Du legst fest, welche Ordner Alben sind (der
  Titel ist der Ordnername und lässt sich ändern), und hakst an, was hinein
  soll, z. B. `Jpegs`, aber nicht `RAWs`. Vorhandene Flickr-Alben werden
  erkannt.
- **Keine Duplikate.** Jeder Upload trägt einen Tag mit einem Prüfwert des
  Dateiinhalts. Angehakte Ordner werden vorher mit den Fotos abgeglichen, die
  schon auf Flickr liegen; mögliche Treffer warten auf deine Entscheidung.
- **Fortsetzbar und geprüft.** Der Fortschritt wird nach jeder Datei
  gespeichert, Pausieren, Schließen oder ein Absturz kosten also nichts. Ein
  Foto gilt erst als hochgeladen, wenn Flickr es bestätigt.
- **Immer privat.** Uploads sind privat und aus der Suche verborgen. Die App
  ändert nie die Sichtbarkeit eines Fotos.
- **Deine Fotos werden nur gelesen.** In deinen Ordnern wird nie etwas
  geschrieben, verschoben oder gelöscht.
- **Alben herunterladen.** Pro Album wählst du Videos, Fotos oder alles. Die
  Dateien kommen als Original, wo Flickr es hat, mit eingetragenem
  Aufnahmedatum, jedes Album in einem eigenen Ordner.

RAW-Dateien und zu große Videos werden übersprungen und im Protokoll
aufgeführt; die App konvertiert und komprimiert nichts.

## Voraussetzungen

- Windows 10 oder 11 mit der WebView2-Runtime (bei Windows 11 vorinstalliert)
- Python 3.11 oder neuer
- Ein Flickr-Konto und ein eigener API-Key (für nicht-kommerzielle Nutzung
  kostenlos)

## Einrichtung

```powershell
git clone https://github.com/NihadCommits/flickr-uploader.git
cd flickr-uploader
py -3 -m venv .venv
.venv\Scripts\python.exe -m pip install -e .
.venv\Scripts\python.exe -m flickr_uploader
```

Danach verbindest du in der App unter **Einstellungen → Flickr-Zugang** dein
Flickr-Konto: API-Key bei Flickr beantragen, Key und Secret eintragen, den
Zugriff auf Flickr erlauben und den angezeigten Code eingeben. Key, Secret und
Anmeldung liegen nur im Windows-Anmeldeinformationsspeicher, nie in einer
Datei. (Im Terminal geht das genauso:
`.venv\Scripts\python.exe -m flickr_uploader.auth setup`.)

## Bedienung

**Hochladen**

1. *Ordner öffnen …*: den Ordner mit deinen Fotos wählen.
2. Im Baum einen Ordner anklicken und *Als Album festlegen* wählen.
3. Die Unterordner anhaken, die hochgeladen werden sollen. Eine Ampel zeigt,
   ob das Album schon auf Flickr ist (grün), neu angelegt wird (blau) oder
   deine Entscheidung braucht (gelb, z. B. bei einem sehr ähnlichen Titel).
4. Bei Dateien, die vielleicht schon auf Flickr sind, *Ist dasselbe Foto* oder
   *Trotzdem hochladen* wählen.
5. *Hochladen starten*. *Pausieren* hält nach der aktuellen Datei an,
   *Fortsetzen* macht dort weiter.

**Alben**: deine Album-Ordner und deine Alben auf Flickr; ein Ordner kann ein
vorhandenes Album verwenden, das dort anders heißt.

**Herunterladen**: Alben anhaken, pro Album *Alles*, *Fotos* oder *Videos*
wählen, einen Zielordner außerhalb deiner Foto-Ordner wählen und starten.

**Einstellungen**: Flickr-Zugang, Vorauswahl und Zielordner fürs
Herunterladen, Albenliste beim Start, Speicherort von Datenbank und Backups.

Datenbank und Backups liegen unter `%LOCALAPPDATA%\flickr-uploader`. Eine
Datenbank gehört zu genau einem Flickr-Konto; für ein zweites Konto vor dem
Start `FLICKR_UPLOADER_HOME` auf einen anderen Ordner setzen.

**Extra:** `scripts\export_albums.py` und `scripts\rename_albums.py` benennen
vorhandene Flickr-Alben gesammelt über eine CSV-Datei um (erst ein Probelauf,
mit `--apply` wird wirklich umbenannt).

## Lizenz

[MIT](LICENSE). Technische Dokumentation: [docs/PROJECT_SPEC.md](docs/PROJECT_SPEC.md).
