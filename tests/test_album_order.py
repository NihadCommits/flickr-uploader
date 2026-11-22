"""Tests for renaming and ordering the albums on Flickr from the albums tab."""

import pytest

from fake_flickr import FakeFlickr
from flickr_uploader import albums
from flickr_uploader.flickr_client import FlickrUnavailableError
from flickr_uploader.gui.api import Api


@pytest.fixture
def flickr():
    flickr = FakeFlickr()
    for title in ("Instagram", "2024-06-03 - Porto", "Familie", "2020-08-01 - Kroatien"):
        flickr.add_album(title)
    return flickr


@pytest.fixture
def api(database, flickr):
    api = Api(database, client_factory=lambda **kwargs: flickr, background=lambda work: work())
    api.refresh_albums()
    return api


def titles(flickr):
    return [album.title for album in flickr.sets.values()]


def ids(api):
    return {album["title"]: album["id"] for album in api.flickr_albums()["albums"]}


def test_the_list_shows_the_order_on_flickr(api, flickr):
    assert [a["title"] for a in api.flickr_albums()["albums"]] == titles(flickr)
    assert [a["position"] for a in api.flickr_albums()["albums"]] == [0, 1, 2, 3]


def test_rename_and_order_in_one_go(api, flickr):
    by = ids(api)
    order = [by["Instagram"], by["2024-06-03 - Porto"], by["2020-08-01 - Kroatien"], by["Familie"]]
    result = api.save_albums(
        [{"album_id": by["Familie"], "title": "2019-12-24 - Familie"}],
        order,
        [by["Instagram"]],
    )
    assert result == {"renamed": 1, "failed": [], "ordered": True}
    assert titles(flickr) == [
        "Instagram",
        "2024-06-03 - Porto",
        "2020-08-01 - Kroatien",
        "2019-12-24 - Familie",
    ]
    listed = api.flickr_albums()["albums"]
    assert [a["title"] for a in listed] == titles(flickr)
    assert [a["pinned"] for a in listed] == [True, False, False, False]


def test_only_renaming_leaves_the_order(api, flickr):
    by = ids(api)
    before = titles(flickr)
    result = api.save_albums([{"album_id": by["Familie"], "title": " Familie neu "}], None, [])
    assert result == {"renamed": 1, "failed": [], "ordered": False}
    assert titles(flickr) == [t if t != "Familie" else "Familie neu" for t in before]
    assert "order_albums" not in flickr.calls


def test_albums_left_out_follow_at_the_end(conn, api, flickr):
    by = ids(api)
    albums.set_order(conn, flickr, [by["Familie"]])
    assert titles(flickr)[0] == "Familie"
    assert len(titles(flickr)) == 4


def test_rename_waits_when_flickr_is_away(api, flickr):
    by = ids(api)
    flickr.fail_before["rename_album"].append(FlickrUnavailableError("offline"))
    result = api.save_albums([{"album_id": by["Familie"], "title": "Familie 2"}], None, [])
    assert result["failed"] == ["Familie 2"]
    api.refresh_albums()  # the pending rename is sent with the next sync
    assert "Familie 2" in titles(flickr)


def test_album_dates_from_the_oldest_photo(api, flickr):
    porto = next(a for a in flickr.sets.values() if a.title == "2024-06-03 - Porto")
    porto.photos.clear()
    flickr.add_media(porto.id, "IMG_2", taken="2024-06-05 10:00:00", original=b"jpeg")
    flickr.add_media(porto.id, "IMG_1", taken="2024-06-03 09:00:00", original=b"jpeg")
    assert api.album_dates()["read"] == 4
    dates = {a["title"]: a["taken"] for a in api.flickr_albums()["albums"]}
    assert dates["2024-06-03 - Porto"] == "2024-06-03 09:00:00"
    assert api.album_dates()["read"] == 0  # remembered


def test_saving_is_refused_while_a_job_runs(api):
    api._job_running = True
    assert "anhalten" in api.save_albums([], None, [])["error"]
