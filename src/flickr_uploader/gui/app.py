"""Start the desktop window: open the database, then show the web frontend.

The page is served from the local folder web/ by pywebview's built-in server
on 127.0.0.1; nothing is loaded from the internet. Only this window's page
can reach the Python API.
"""

import sys
from pathlib import Path

from flickr_uploader import config, db
from flickr_uploader.gui.api import Api

TITLE = "Flickr Uploader"
WEB_DIR = Path(__file__).with_name("web")
BACKGROUND = "#F3F5F6"  # --bg in style.css, shown until the page is drawn


def main() -> int:
    try:
        paths = config.app_paths()
        database = db.Database(paths.database, backup_dir=paths.backups)
        database.open()
    except db.AlreadyRunningError:
        _show_error("Flickr Uploader läuft schon in einem anderen Fenster.")
        return 1
    except (config.ConfigError, db.DatabaseError, OSError) as error:
        _show_error(f"Die Datenbank konnte nicht geöffnet werden:\n\n{error}")
        return 1

    import webview

    try:
        with database.connect() as conn:
            db.recover_interrupted(conn)
        api = Api(database)
        window = webview.create_window(
            TITLE,
            url=str(WEB_DIR / "index.html"),
            js_api=api,
            width=1200,
            height=800,
            min_size=(900, 600),
            background_color=BACKGROUND,
        )
        api._attach(window)
        webview.start(http_server=True, private_mode=True)
        # The window is closed: a running upload stops after its current photo.
        api._shutdown()
    finally:
        database.close()
    return 0


def _show_error(message: str) -> None:
    """A message box before (or without) the window."""
    if sys.platform == "win32":
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, message, TITLE, 0x10)  # MB_ICONERROR
    else:  # pragma: no cover
        print(message, file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
