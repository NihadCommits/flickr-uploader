"""Tests for the window's Python API (flickr_uploader.gui.api), without a window.

The folder dialog is replaced, Flickr is the in-memory FakeFlickr, and work
meant for the background runs at once.
"""

import pytest
from PIL import ExifTags, Image

from fake_flickr import ME, FakeFlickr
from flickr_uploader import auth
from flickr_uploader.flickr_client import FlickrUnavailableError, FoundPhoto
from flickr_uploader.gui.api import Api


def jpeg(path, taken=None, color=(200, 0, 0)):
    path.parent.mkdir(parents=True, exist_ok=True)
    exif = Image.Exif()
    if taken:
        exif.get_ifd(ExifTags.IFD.Exif)[ExifTags.Base.DateTimeOriginal] = taken
    Image.new("RGB", (32, 24), color).save(path, exif=exif)


@pytest.fixture
def flickr():
    return FakeFlickr()


@pytest.fixture
def photos(tmp_path):
    base = tmp_path / "Bilder"
    jpeg(base / "2026-06 Portugal" / "Jpegs" / "IMG_0001.JPG", "2026:06:01 10:00:00")
    jpeg(base / "2026-06 Portugal" / "Jpegs" / "IMG_0002.JPG", "2026:06:02 10:00:00", (0, 200, 0))
    (base / "2026-06 Portugal" / "RAWs").mkdir()
    (base / "2026-06 Portugal" / "RAWs" / "IMG_0001.CR3").write_bytes(b"raw")
    (base / "2026-07 Wandern").mkdir()
    return base


class Window:
    """Stands in for the pywebview window: the folder dialog returns `chosen`."""

    def __init__(self, chosen):
        self.chosen = chosen

    def create_file_dialog(self, dialog_type):
        return [str(self.chosen)] if self.chosen else None


@pytest.fixture
def opened():
    return []


@pytest.fixture
def shown():
    return []


@pytest.fixture
def api(database, flickr, photos, opened, shown):
    api = Api(
        database,
        client_factory=lambda **kwargs: flickr,
        background=lambda work: work(),
        open_url=opened.append,
        open_folder=shown.append,
    )
    api._attach(Window(photos))
    return api


def ticked_portugal(api, flickr, with_old_photo=True):
    """Portugal is an album folder with Jpegs ticked; IMG_0001 maybe on Flickr already."""
    old = None
    if with_old_photo:
        old = flickr.add_photo("IMG_0001")
        flickr.old_photos.append(FoundPhoto(old, "IMG_0001", "2026-06-01 10:00:00", 32, 24))
    root_id = api.add_root()["root_id"]
    tree = api.tree(root_id)
    api.make_album_folder(folder(tree, "2026-06 Portugal")["id"])
    jpegs = folder(tree, "2026-06 Portugal/Jpegs")["id"]
    api.set_included(jpegs, True)
    return root_id, jpegs, old


def folder(tree, path):
    return next(f for f in tree["folders"] if f["path"] == path)


def test_status_without_login(database):
    def not_set_up(**kwargs):
        raise auth.NotConfiguredError("not set up yet")

    status = Api(database, client_factory=not_set_up).status()
    assert status["logged_in"] is False
    assert "Einstellungen" in status["message"]
    assert status["database"] == str(database.path)


def test_add_root_scans_it(api, photos):
    result = api.add_root()
    assert result["report"]["files"] == 3
    roots = api.roots()
    assert [(r["id"], r["path"], r["reachable"]) for r in roots] == [
        (result["root_id"], str(photos), True)
    ]


def test_cancelled_dialog_changes_nothing(api):
    api._attach(Window(None))
    assert api.add_root() == {"cancelled": True}
    assert api.roots() == []


def test_overlapping_source_folder_is_explained(api, photos):
    api.add_root()
    api._attach(Window(photos / "2026-06 Portugal"))
    assert "Überschneidet" in api.add_root()["error"]


def test_tree(api, photos):
    root_id = api.add_root()["root_id"]
    tree = api.tree(root_id)
    root = folder(tree, "")
    assert (root["name"], root["parent"], root["total_files"]) == ("Bilder", None, 3)
    assert tree["root"]["path"] == str(photos)
    jpegs = folder(tree, "2026-06 Portugal/Jpegs")
    assert (jpegs["name"], jpegs["parent"], jpegs["files"]) == ("Jpegs", "2026-06 Portugal", 2)
    assert folder(tree, "2026-06 Portugal/RAWs")["skipped"] == 1
    assert jpegs["album"] is None and jpegs["in_album"] is None and jpegs["upload"] is None


def test_album_folder_and_ticks(api, flickr):
    root_id = api.add_root()["root_id"]
    portugal = folder(api.tree(root_id), "2026-06 Portugal")
    album_id = api.make_album_folder(portugal["id"])["album_id"]

    tree = api.tree(root_id)
    album = folder(tree, "2026-06 Portugal")["album"]
    assert (album["id"], album["title"], album["state"], album["light"]) == (
        album_id,
        "2026-06 Portugal",
        "planned",
        "blue",
    )
    jpegs = folder(tree, "2026-06 Portugal/Jpegs")
    assert jpegs["in_album"] == album_id

    assert api.set_included(jpegs["id"], True) == {}
    jpegs = folder(api.tree(root_id), "2026-06 Portugal/Jpegs")
    assert jpegs["included"] is True
    # The pre-check ran right away (in the background in the app).
    assert jpegs["prechecked"] is True and jpegs["precheck_running"] is False
    assert jpegs["upload"] == {"state": "open", "files": 2, "verified": 0, "failed": 0}
    assert flickr.count("photos_taken_between") == 1


def test_open_matches_are_counted(api, flickr):
    old = flickr.add_photo("IMG_0001")
    flickr.old_photos.append(FoundPhoto(old, "IMG_0001", "2026-06-01 10:00:00", 32, 24))
    root_id = api.add_root()["root_id"]
    tree = api.tree(root_id)
    api.make_album_folder(folder(tree, "2026-06 Portugal")["id"])
    api.set_included(folder(tree, "2026-06 Portugal/Jpegs")["id"], True)
    assert folder(api.tree(root_id), "2026-06 Portugal/Jpegs")["open_matches"] == 1


def test_failed_precheck_is_logged_not_raised(api, flickr, database):
    flickr.fail_before["photos_taken_between"].append(FlickrUnavailableError("offline"))
    root_id = api.add_root()["root_id"]
    tree = api.tree(root_id)
    api.make_album_folder(folder(tree, "2026-06 Portugal")["id"])
    assert api.set_included(folder(tree, "2026-06 Portugal/Jpegs")["id"], True) == {}
    assert folder(api.tree(root_id), "2026-06 Portugal/Jpegs")["prechecked"] is False
    with database.connect() as conn:
        assert "not done" in conn.execute("SELECT message FROM events").fetchone()[0]


def test_rules_come_back_as_messages(api):
    root_id = api.add_root()["root_id"]
    tree = api.tree(root_id)
    portugal, jpegs, wandern = (
        folder(tree, p)["id"]
        for p in ("2026-06 Portugal", "2026-06 Portugal/Jpegs", "2026-07 Wandern")
    )
    assert "Anhaken" in api.set_included(wandern, True)["error"]
    api.make_album_folder(portugal)
    assert "führend" in api.make_album_folder(jpegs)["error"]
    assert "leer" in api.make_album_folder(wandern, "   ")["error"]


def test_planned_title_and_yellow_light(api, flickr):
    flickr.add_album("2026-06 portugal")
    root_id = api.add_root()["root_id"]
    api.refresh_albums()
    portugal = folder(api.tree(root_id), "2026-06 Portugal")
    album_id = api.make_album_folder(portugal["id"])["album_id"]
    assert folder(api.tree(root_id), "2026-06 Portugal")["album"]["light"] == "yellow"

    assert api.confirm_new_album(album_id) == {}
    assert folder(api.tree(root_id), "2026-06 Portugal")["album"]["light"] == "blue"
    assert api.rename_planned_album(album_id, "Portugal 2026") == {}
    assert folder(api.tree(root_id), "2026-06 Portugal")["album"]["title"] == "Portugal 2026"


def test_existing_flickr_album_is_used(api, flickr):
    flickr.add_album("2026-06 Portugal")
    root_id = api.add_root()["root_id"]
    assert api.refresh_albums()["albums"] == 1
    portugal = folder(api.tree(root_id), "2026-06 Portugal")
    api.make_album_folder(portugal["id"])
    album = folder(api.tree(root_id), "2026-06 Portugal")["album"]
    assert (album["state"], album["light"], album["photo_count"]) == ("exists", "green", 1)
    assert "umbenennen" in api.rename_planned_album(album["id"], "Neu")["error"]


def test_remove_album_folder(api):
    root_id = api.add_root()["root_id"]
    tree = api.tree(root_id)
    portugal = folder(tree, "2026-06 Portugal")["id"]
    api.make_album_folder(portugal)
    api.set_included(folder(tree, "2026-06 Portugal/Jpegs")["id"], True)
    assert api.remove_album_folder(portugal) == {}
    jpegs = folder(api.tree(root_id), "2026-06 Portugal/Jpegs")
    assert (jpegs["in_album"], jpegs["included"]) == (None, False)


def test_overview_tells_how_far_the_user_got(api, flickr):
    first = api.overview()
    assert (first["roots"], first["album_folders"], first["included"]) == (0, 0, 0)
    assert first["albums_synced_at"] is None

    flickr.add_album("2026-07 wandern")
    root_id = api.add_root()["root_id"]
    tree = api.tree(root_id)
    api.make_album_folder(folder(tree, "2026-06 Portugal")["id"])
    api.make_album_folder(folder(tree, "2026-07 Wandern")["id"])
    api.set_included(folder(tree, "2026-06 Portugal/Jpegs")["id"], True)
    result = api.refresh_albums()
    assert result["lights"] == {"green": 0, "blue": 1, "yellow": 1}

    now = api.overview()
    assert (now["roots"], now["album_folders"], now["included"]) == (1, 2, 1)
    assert now["lights"] == {"green": 0, "blue": 1, "yellow": 1}
    assert now["flickr_albums"] == 1 and now["albums_synced_at"]


def test_album_folders_and_flickr_albums(api, flickr, photos):
    flickr.add_album("2026-06 Portugal")
    flickr.add_album("Urlaub 2019")
    root_id = api.add_root()["root_id"]
    api.refresh_albums()
    tree = api.tree(root_id)
    api.make_album_folder(folder(tree, "2026-06 Portugal")["id"])
    api.make_album_folder(folder(tree, "2026-07 Wandern")["id"])

    rows = api.album_folders()
    assert [(r["album"]["title"], r["album"]["light"]) for r in rows] == [
        ("2026-06 Portugal", "green"),
        ("2026-07 Wandern", "blue"),
    ]
    assert rows[0]["path"] == str(photos / "2026-06 Portugal")
    assert rows[0]["root_id"] == root_id

    listed = api.flickr_albums()
    assert listed["synced_at"]
    assert [(a["title"], a["used_by"]) for a in listed["albums"]] == [
        ("2026-06 Portugal", 1),
        ("Urlaub 2019", 0),
    ]


def test_use_existing_album(api, flickr):
    flickr.add_album("Portugal Sommer 2026")
    root_id = api.add_root()["root_id"]
    api.refresh_albums()
    portugal = folder(api.tree(root_id), "2026-06 Portugal")["id"]
    planned = api.make_album_folder(portugal)["album_id"]
    existing = api.flickr_albums()["albums"][0]["id"]

    assert api.use_existing_album(portugal, existing) == {}
    album = folder(api.tree(root_id), "2026-06 Portugal")["album"]
    assert (album["id"], album["title"], album["light"]) == (
        existing,
        "Portugal Sommer 2026",
        "green",
    )
    assert planned not in [a["id"] for a in api.flickr_albums()["albums"]]
    wandern = folder(api.tree(root_id), "2026-07 Wandern")["id"]
    assert "Album-Ordner" in api.use_existing_album(wandern, existing)["error"]


def test_refresh_albums_without_connection(api, flickr):
    flickr.fail_before["albums"].append(FlickrUnavailableError("offline"))
    assert "nicht erreichbar" in api.refresh_albums()["error"]


def test_only_public_methods_reach_the_page():
    # pywebview exposes every public attribute of the API object to the page.
    public = {name for name in dir(Api) if not name.startswith("_")}
    assert public == {
        "account",
        "add_root",
        "album_folders",
        "backup_now",
        "cancel_login",
        "choose_download_target",
        "choose_same",
        "choose_upload",
        "confirm_new_album",
        "decide_folder",
        "finish_login",
        "flickr_albums",
        "info",
        "job",
        "logout",
        "make_album_folder",
        "matches",
        "open_flickr_page",
        "open_folder",
        "open_on_flickr",
        "overview",
        "pause_job",
        "refresh_albums",
        "remove_album_folder",
        "rename_planned_album",
        "reopen_login_page",
        "reset_choice",
        "roots",
        "scan",
        "set_included",
        "set_setting",
        "settings",
        "start_download",
        "start_login",
        "start_upload",
        "status",
        "tree",
        "upload_plan",
        "use_existing_album",
    }


# --- Decisions about photos that may be on Flickr ------------------------------------


def test_matches_and_decisions(api, flickr):
    root_id, jpegs, old = ticked_portugal(api, flickr)
    found = api.matches(jpegs)
    assert found["open"] == 1
    (file,) = found["files"]
    assert (file["name"], file["state"], file["taken"]) == (
        "IMG_0001.JPG",
        "open",
        "2026-06-01 10:00:00",
    )
    (candidate,) = file["candidates"]
    assert (candidate["flickr_id"], candidate["confidence"], candidate["chosen"]) == (
        old,
        "certain",
        False,
    )

    assert file["done"] is False
    assert api.choose_same(file["file_id"], old) == {}
    file = api.matches(jpegs)["files"][0]
    assert (file["state"], file["candidates"][0]["chosen"]) == ("same", True)
    assert api.reset_choice(file["file_id"]) == {}
    assert api.matches(jpegs)["files"][0]["state"] == "open"
    assert api.choose_upload(file["file_id"]) == {}
    assert api.matches(jpegs)["open"] == 0
    assert folder(api.tree(root_id), "2026-06 Portugal/Jpegs")["open_matches"] == 0


def test_decide_a_whole_folder(api, flickr):
    _, jpegs, old = ticked_portugal(api, flickr)
    assert api.decide_folder(jpegs, "same") == {"decided": 1}
    assert api.matches(jpegs)["files"][0]["state"] == "same"
    assert api.decide_folder(jpegs, "upload") == {"decided": 0}  # nothing open any more
    assert "Unbekannte" in api.decide_folder(jpegs, "maybe")["error"]


def test_choosing_a_photo_that_is_no_match(api, flickr):
    _, jpegs, _ = ticked_portugal(api, flickr)
    file_id = api.matches(jpegs)["files"][0]["file_id"]
    assert "gilt nicht mehr" in api.choose_same(file_id, "999")["error"]


def test_open_on_flickr(api, flickr, opened):
    assert api.open_on_flickr("5555") == {}
    assert opened == [f"https://www.flickr.com/photos/{flickr.user_nsid}/5555"]


# --- Upload --------------------------------------------------------------------------


def test_upload_plan(api, flickr):
    ticked_portugal(api, flickr)
    plan = api.upload_plan()
    assert (plan["files"], plan["ready"], plan["decide"], plan["done"]) == (2, 1, 1, 0)


def test_upload_runs_and_reports(api, flickr):
    root_id, jpegs, old = ticked_portugal(api, flickr)
    api.decide_folder(jpegs, "same")
    assert api.start_upload() == {}  # runs right away in this test

    job = api.job()
    assert job["running"] is False
    run = job["run"]
    assert (run["kind"], run["status"], run["items_total"], run["items_done"]) == (
        "upload",
        "completed",
        2,
        2,
    )
    assert (job["report"]["uploaded"], job["report"]["adopted"]) == (1, 1)
    assert api.matches(jpegs)["files"][0]["done"] is True  # taken over now
    assert any(event["message"].startswith("Uploaded") for event in job["events"])
    plan = api.upload_plan()
    assert (plan["ready"], plan["done"]) == (0, 2)
    assert folder(api.tree(root_id), "2026-06 Portugal/Jpegs")["upload"]["state"] == "done"


def test_pause_stops_after_the_current_photo_and_resume_goes_on(api, flickr):
    ticked_portugal(api, flickr, with_old_photo=False)
    flickr.on_upload = lambda photo_id: api.pause_job()
    api.start_upload()
    job = api.job()
    assert job["run"]["status"] == "aborted"
    assert job["report"]["uploaded"] == 1

    flickr.on_upload = None
    api.start_upload()
    job = api.job()
    assert (job["run"]["status"], job["report"]["uploaded"]) == ("completed", 1)
    assert api.upload_plan()["done"] == 2
    assert flickr.count("upload") == 2  # nothing twice


def test_only_one_upload_at_a_time(database, flickr):
    later = []
    api = Api(database, client_factory=lambda **kwargs: flickr, background=later.append)
    assert api.start_upload() == {}
    assert "läuft schon" in api.start_upload()["error"]
    assert api.job()["running"] is True


def test_upload_needs_a_login(database):
    def not_set_up(**kwargs):
        raise auth.NotConfiguredError("not set up yet")

    assert "angemeldet" in Api(database, client_factory=not_set_up).start_upload()["error"]


def test_shutdown_without_a_run_returns_at_once(api):
    api._shutdown(timeout=0.1)
    assert api.job()["running"] is False


# --- Download ------------------------------------------------------------------------


def album_with_media(api, flickr, title="2026-06 Portugal"):
    """An album on (fake) Flickr with a photo and a video, mirrored into the DB."""
    album = flickr.add_album(title)
    flickr.sets[album].photos.clear()
    flickr.add_media(album, "IMG_0001", original=b"jpeg")
    flickr.add_media(album, "MVI_0002", media="video", original=None, converted=b"mp4")
    api.refresh_albums()
    return next(a["id"] for a in api.flickr_albums()["albums"] if a["title"] == title)


def choose_target(api, path):
    """Pick `path` in the (fake) folder dialog as the download folder."""
    path.mkdir(parents=True, exist_ok=True)
    api._attach(Window(path))
    return api.choose_download_target()


def test_download_target_is_never_in_the_archive_and_remembered(api, photos, tmp_path):
    api.add_root()  # the photo archive: Bilder
    assert "Foto-Archiv" in choose_target(api, photos / "2026-06 Portugal")["error"]
    assert api.settings()["download_target"] is None
    target = tmp_path / "Downloads"
    assert choose_target(api, target) == {"path": str(target)}
    api._attach(Window(None))
    assert api.choose_download_target() == {"cancelled": True}
    assert api.settings()["download_target"] == str(target)
    assert api.settings()["download_target_problem"] is None


def test_download_runs_and_reports(api, flickr, tmp_path):
    album_id = album_with_media(api, flickr)
    target = tmp_path / "Downloads"
    choose_target(api, target)
    assert api.start_download([{"album_id": album_id, "media": "videos"}]) == {}
    job = api.job()
    run = job["run"]
    assert (run["kind"], run["status"], run["items_total"], run["items_done"]) == (
        "download",
        "completed",
        1,
        1,
    )
    assert (job["report"]["downloaded"], job["report"]["not_original"]) == (1, 1)
    assert sorted(path.name for path in (target / "2026-06 Portugal").iterdir()) == ["MVI_0002.mp4"]


def test_download_choices_are_checked_first(api, flickr, photos, tmp_path, database):
    album_id = album_with_media(api, flickr)
    everything = [{"album_id": album_id, "media": "all"}]
    assert "mindestens ein Album" in api.start_download([])["error"]
    assert "Zielordner" in api.start_download(everything)["error"]
    choose_target(api, tmp_path / "Downloads")
    assert "Albenliste" in api.start_download([{"album_id": album_id, "media": "x"}])["error"]
    (tmp_path / "Downloads").rmdir()
    assert "gibt es nicht" in api.start_download(everything)["error"]
    assert api.settings()["download_target_problem"] == "missing"
    # Remembered before the archive was opened, inside it afterwards.
    choose_target(api, photos / "Downloads")
    api._attach(Window(photos))
    api.add_root()
    assert "Foto-Archiv" in api.start_download(everything)["error"]
    assert api.settings()["download_target_problem"] == "archive"
    with database.connect() as conn:
        assert conn.execute("SELECT count(*) FROM runs").fetchone()[0] == 0


def test_upload_and_download_never_run_together(database, flickr, tmp_path):
    later = []
    api = Api(database, client_factory=lambda **kwargs: flickr, background=later.append)
    album_id = album_with_media(api, flickr)
    choose_target(api, tmp_path / "Downloads")
    assert api.start_upload() == {}
    assert "läuft schon" in api.start_download([{"album_id": album_id, "media": "all"}])["error"]


def test_open_folder(api, tmp_path, shown):
    assert api.open_folder(str(tmp_path)) == {}
    assert shown == [str(tmp_path)]
    assert "nicht" in api.open_folder(str(tmp_path / "gone"))["error"]


# --- Settings and info -------------------------------------------------------------


def test_settings_defaults_and_changes(api):
    assert api.settings() == {
        "download_target": None,
        "download_media": "all",
        "sync_albums_at_start": True,
        "flickr_account": None,
        "download_target_problem": None,
    }
    assert api.set_setting("download_media", "videos") == {}
    assert api.set_setting("sync_albums_at_start", False) == {}
    assert "passt nicht" in api.set_setting("download_media", "raw")["error"]
    # The download folder only comes from the dialog, which checks it.
    assert "nicht ändern" in api.set_setting("download_target", r"D:\Bilder")["error"]
    assert "nicht ändern" in api.set_setting("theme", "xp")["error"]
    settings = api.settings()
    assert (settings["download_media"], settings["sync_albums_at_start"]) == ("videos", False)
    assert settings["download_target"] is None


def test_info_and_backup_now(api, database):
    info = api.info()
    assert info["database"] == str(database.path)
    assert info["database_folder"] == str(database.path.parent)
    assert info["backup_folder"] == str(database.backup_dir)
    assert (info["backups"], info["latest_backup"], info["keep_backups"]) == (0, None, 10)
    assert info["version"]

    made = api.backup_now()
    assert made["latest_backup"].endswith("+00:00")
    info = api.info()
    assert (info["backups"], info["latest_backup"]) == (1, made["latest_backup"])


# --- Flickr access from the settings tab ---------------------------------------------

KEY = "a" * 32
SECRET = "b" * 16
CODE = "123-456-789"


class FakeLoginFlow:
    """Stands in for auth.LoginFlow: no network; Flickr accepts only CODE."""

    url = "https://www.flickr.com/services/oauth/authorize?oauth_token=fake"
    nsid = ME
    username = "someone"

    def __init__(self, credentials):
        if credentials.key != KEY:
            raise auth.InvalidCredentialsError("Flickr rejected the API key or secret")
        self.credentials = credentials

    def finish(self, code):
        if code.strip() != CODE:
            raise auth.InvalidCredentialsError("Flickr did not accept the code")
        return auth.AccessToken(
            "token", "token-secret", "write", username=self.username, user_nsid=self.nsid
        )


def logged_in_client(store, flickr):
    """Like FlickrClient.from_store: only with a stored login, as its account."""

    def factory(**kwargs):
        token = store.load_token()
        if store.load_api_credentials() is None or token is None:
            raise auth.NotConfiguredError("not set up yet")
        flickr.user_nsid = token.user_nsid
        return flickr

    return factory


@pytest.fixture
def setup_api(database, flickr, store, opened):
    """An Api whose Flickr login is set up from the page, into an in-memory store."""
    return Api(
        database,
        client_factory=logged_in_client(store, flickr),
        store=store,
        login_flow=FakeLoginFlow,
        background=lambda work: work(),
        open_url=opened.append,
    )


def test_login_from_the_settings_tab(setup_api, store, opened):
    assert setup_api.account()["state"] == "missing"
    assert setup_api.status()["logged_in"] is False

    assert setup_api.start_login(f" {KEY.upper()} ", SECRET) == {}
    assert opened == [FakeLoginFlow.url]
    assert setup_api.account()["pending"] is True
    assert store.load_api_credentials() is None  # nothing stored before the login worked

    assert "nicht angenommen" in setup_api.finish_login("000-000-000")["error"]
    assert store.load_token() is None
    assert setup_api.finish_login(f" {CODE} ") == {"username": "someone"}

    account = setup_api.account()
    assert (account["state"], account["pending"], account["username"]) == (
        "ready",
        False,
        "someone",
    )
    assert account["access_level"] == "write"
    assert setup_api.status()["logged_in"] is True
    assert store.load_api_credentials() == auth.ApiCredentials(KEY, SECRET)


def test_secrets_never_reach_the_page_or_the_database(setup_api, database):
    setup_api.start_login(KEY, SECRET)
    setup_api.finish_login(CODE)
    answers = [setup_api.account(), setup_api.settings(), setup_api.status()]
    for text in map(str, answers):
        assert KEY not in text and SECRET not in text and "token-secret" not in text
    with database.connect() as conn:
        dump = "\n".join(conn.iterdump())
    assert KEY not in dump and SECRET not in dump and "token-secret" not in dump


def test_bad_key_or_secret_are_explained(setup_api, opened):
    assert "32 Zeichen" in setup_api.start_login("abc", SECRET)["error"]
    assert "32 Zeichen" in setup_api.start_login(KEY, "")["error"]
    assert "kennt diesen API-Key" in setup_api.start_login("c" * 32, SECRET)["error"]
    assert "zuerst API-Key" in setup_api.finish_login(CODE)["error"]
    assert opened == []


def test_flickr_unreachable_shows_no_url_or_key(database, store):
    def offline(credentials):
        raise ConnectionError(f"no route to https://api.flickr.com/?api_key={credentials.key}")

    api = Api(database, store=store, login_flow=offline)
    error = api.start_login(KEY, SECRET)["error"]
    assert "nicht erreichbar (ConnectionError)" in error
    assert KEY not in error and "http" not in error


def test_cancel_and_reopen_login(setup_api, opened):
    assert "zuerst" in setup_api.reopen_login_page()["error"]
    setup_api.start_login(KEY, SECRET)
    assert setup_api.reopen_login_page() == {}
    assert opened == [FakeLoginFlow.url, FakeLoginFlow.url]
    assert setup_api.cancel_login() == {}
    assert setup_api.account()["pending"] is False


def test_logout_removes_everything(setup_api, store):
    setup_api.start_login(KEY, SECRET)
    setup_api.finish_login(CODE)
    assert setup_api.logout() == {}
    assert (store.load_api_credentials(), store.load_token()) == (None, None)
    assert setup_api.account()["state"] == "missing"


def test_login_is_refused_while_a_job_runs(database, flickr, store):
    api = Api(database, client_factory=lambda **kwargs: flickr, store=store, background=list)
    api._job_running = True
    assert "anhalten" in api.start_login(KEY, SECRET)["error"]
    assert "anhalten" in api.logout()["error"]


def test_only_fixed_flickr_pages_can_be_opened(setup_api, opened):
    assert setup_api.open_flickr_page("create_key") == {}
    assert setup_api.open_flickr_page("authorized_apps") == {}
    assert opened == [auth.API_KEY_URL, auth.AUTHORIZED_APPS_URL]
    assert "Unbekannte" in setup_api.open_flickr_page("https://example.com")["error"]


def test_database_stays_with_its_flickr_account(setup_api, flickr, store):
    setup_api.start_login(KEY, SECRET)
    setup_api.finish_login(CODE)
    setup_api.refresh_albums()  # now the database holds albums of this account
    flickr.add_album("2026-06 Portugal")
    setup_api.refresh_albums()

    class OtherAccount(FakeLoginFlow):
        nsid = "99999999@N00"
        username = "somebody else"

    setup_api._login_flow = OtherAccount
    setup_api.start_login(KEY, SECRET)
    error = setup_api.finish_login(CODE)["error"]
    assert "anderes Flickr-Konto" in error and "„someone“" in error
    assert store.load_token().username == "someone"  # the old login stays


def test_empty_database_follows_a_new_account(database, store):
    flickr = FakeFlickr()
    api = Api(database, client_factory=lambda **kwargs: flickr, store=store)
    assert api.status()["logged_in"] is True  # binds the empty database to ME
    flickr.user_nsid = "99999999@N00"
    assert api.status()["logged_in"] is True  # nothing from Flickr in it yet: rebound
    assert api.settings()["flickr_account"]["nsid"] == "99999999@N00"


def test_another_account_is_refused_once_there_is_data(database):
    flickr = FakeFlickr()
    api = Api(database, client_factory=lambda **kwargs: flickr, background=lambda work: work())
    flickr.add_album("2026-06 Portugal")
    api.refresh_albums()
    flickr.user_nsid = "99999999@N00"
    status = api.status()
    assert status["logged_in"] is False
    assert "anderes Flickr-Konto" in status["message"]
    assert "anderes Flickr-Konto" in api.start_upload()["error"]
