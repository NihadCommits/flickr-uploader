"""Tests for the Flickr wrapper (flickr_uploader.flickr_client).

flickrapi is replaced by FakeApi, which plays back scripted answers and
errors; nothing reaches the network (enforced in conftest.py).
"""

import inspect
import xml.etree.ElementTree as ElementTree

import pytest
import requests
from flickrapi.exceptions import FlickrError

from flickr_uploader import auth, flickr_client
from flickr_uploader.flickr_client import (
    FlickrAuthError,
    FlickrClient,
    FlickrRejectedError,
    FlickrUnavailableError,
    MediaItem,
    MediaSize,
    OutcomeUnknownError,
    RateLimiter,
)

DIGEST = "3898db5d5b4a87468b544a01c6d30e1c"
OTHER_DIGEST = "99aa06d3014798d86001c324468d497f"
ME = "12345678@N00"
SOMEONE_ELSE = "99999999@N00"
UPLOAD_OK = '<rsp stat="ok"><photoid>555</photoid></rsp>'
# Like a requests error: the message contains the request URL with the key.
LEAKY = requests.ConnectionError("Max retries exceeded with url: /?oauth_consumer_key=" + "k" * 32)


class FakeApi:
    """Stand-in for flickrapi.FlickrAPI: api.photos.getInfo(...) runs handlers["photos.getInfo"]."""

    def __init__(self):
        self.calls = []
        self.handlers = {}
        self.uploads = []
        self.upload_outcomes = [ElementTree.fromstring(UPLOAD_OK)]

    def __getattr__(self, name):
        return _Method(self, name)

    def upload(self, filename, fileobj=None, timeout=None, **params):
        self.uploads.append(
            {"filename": filename, "file": fileobj, "data": fileobj.read(), "timeout": timeout}
            | params
        )
        return _play(self.upload_outcomes)


class _Method:
    def __init__(self, api, path):
        self._api, self._path = api, path

    def __getattr__(self, name):
        return _Method(self._api, f"{self._path}.{name}")

    def __call__(self, **params):
        self._api.calls.append((self._path, params))
        return _play(self._api.handlers[self._path])


def _play(outcomes):
    """Next scripted outcome (the last one repeats); exceptions are raised."""
    outcome = outcomes.pop(0) if len(outcomes) > 1 else outcomes[0]
    if isinstance(outcome, BaseException):
        raise outcome
    return outcome


class CountingLimiter:
    def __init__(self):
        self.acquired = 0

    def acquire(self, cancel=None):
        self.acquired += 1


@pytest.fixture
def api():
    return FakeApi()


@pytest.fixture
def sleeps():
    return []


@pytest.fixture
def limiter():
    return CountingLimiter()


@pytest.fixture
def client(api, sleeps, limiter):
    return FlickrClient(api, ME, limiter=limiter, sleep=sleeps.append)


PRIVATE = {"ispublic": 0, "isfriend": 0, "isfamily": 0}


def info(photo_id="555", owner=ME, tags=(), visibility=PRIVATE):
    photo = {
        "id": photo_id,
        "owner": {"nsid": owner},
        "title": {"_content": "IMG_0001"},
        "dates": {"taken": "2026-06-01 10:00:00"},
        "tags": {"tag": [{"author": author, "raw": raw} for author, raw in tags]},
    }
    if visibility is not None:
        photo["visibility"] = visibility
    return {"photo": photo}


def own_hash_tag(digest=DIGEST, author=ME):
    return (author, "flickruploader:hash=" + digest)


def calls_to(api, method):
    return [params for path, params in api.calls if path == method]


# --- Hash tag and privacy -----------------------------------------------------


def test_hash_tag():
    assert flickr_client.hash_tag(DIGEST) == "flickruploader:hash=" + DIGEST


@pytest.mark.parametrize("bad", [DIGEST.upper(), DIGEST[:-1], f"{DIGEST[:8]} evil:tag=1", ""])
def test_hash_tag_refuses_anything_but_a_digest(bad):
    with pytest.raises(ValueError):
        flickr_client.hash_tag(bad)


def test_there_is_no_way_to_upload_anything_but_private():
    parameters = set(inspect.signature(FlickrClient.upload).parameters)
    assert parameters == {"self", "path", "title", "digest", "cancel"}


# --- Rate limiter -------------------------------------------------------------


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def test_rate_limiter_uses_a_sliding_window():
    clock = FakeClock()
    limiter = RateLimiter(limit=3, window=10.0, clock=clock, sleep=clock.sleep)
    for moment in (0.0, 4.0, 5.0):
        clock.now = moment
        limiter.acquire()
    assert clock.now == 5.0  # three calls, no waiting

    limiter.acquire()  # 4th call: waits until the first one leaves the window
    assert clock.now == pytest.approx(10.0)


def test_rate_limiter_notices_a_cancel_while_waiting():
    clock = FakeClock()
    limiter = RateLimiter(limit=1, window=3600.0, clock=clock, sleep=clock.sleep)
    limiter.acquire()

    class CancelAfterAFewSeconds:
        def is_set(self):
            return clock.now >= 3.0

    with pytest.raises(flickr_client.CancelledError):
        limiter.acquire(cancel=CancelAfterAFewSeconds())
    assert clock.now < 5.0  # did not sleep for an hour


def test_every_attempt_passes_the_rate_limiter(client, api, limiter):
    api.handlers["photos.addTags"] = [LEAKY, LEAKY, {}]
    client.add_hash_tag("555", DIGEST)
    assert limiter.acquired == 3


# --- Errors and retries -------------------------------------------------------


def test_transient_errors_are_retried(client, api, sleeps):
    api.handlers["photos.getInfo"] = [LEAKY, FlickrError("Error: 105", code=105), info()]
    assert client.photo_info("555").id == "555"
    assert sleeps == [2.0, 8.0]


def test_retries_give_up_without_leaking_the_url(client, api, sleeps):
    api.handlers["photos.getInfo"] = [LEAKY]
    with pytest.raises(FlickrUnavailableError) as error:
        client.photo_info("555")
    assert len(calls_to(api, "photos.getInfo")) == 3
    assert "k" * 32 not in str(error.value)
    assert error.value.__cause__ is None
    assert error.value.__suppress_context__


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (FlickrError("Error: 98: Invalid auth token", code=98), FlickrAuthError),
        (FlickrError("Error: 100: Invalid API Key", code=100), FlickrAuthError),
        (FlickrError("do_request: Status code 401 received"), FlickrAuthError),
        (FlickrError("do_request: Status code 404 received"), FlickrRejectedError),
        (FlickrError("Error: 2: Photo not found", code=2), FlickrRejectedError),
    ],
)
def test_permanent_errors_are_not_retried(client, api, sleeps, error, expected):
    api.handlers["photos.addTags"] = [error]
    with pytest.raises(expected):
        client.add_hash_tag("555", DIGEST)
    assert len(calls_to(api, "photos.addTags")) == 1
    assert sleeps == []


@pytest.mark.parametrize("status", [429, 500, 502, 503])
def test_overload_and_server_errors_are_retried(client, api, status):
    api.handlers["photos.addTags"] = [FlickrError(f"do_request: Status code {status}"), {}]
    client.add_hash_tag("555", DIGEST)
    assert len(calls_to(api, "photos.addTags")) == 2


def test_rejected_errors_keep_their_code(client, api):
    api.handlers["photos.addTags"] = [FlickrError("Error: 2: Photo not found", code=2)]
    with pytest.raises(FlickrRejectedError) as error:
        client.add_hash_tag("555", DIGEST)
    assert error.value.code == 2


def test_bugs_are_not_disguised_as_flickr_errors(client, api):
    api.handlers["photos.getInfo"] = [{"unexpected": "shape"}]
    with pytest.raises(KeyError):
        client.photo_info("555")


# --- Upload -------------------------------------------------------------------


@pytest.fixture
def photo(tmp_path):
    path = tmp_path / "Größe ä IMG_0001.JPG"
    path.write_bytes(b"jpeg bytes")
    return path


def test_upload(client, api, photo):
    photo_id = client.upload(photo, title="Größe ä IMG_0001", digest=DIGEST)

    assert photo_id == "555"
    (sent,) = api.uploads
    assert sent["data"] == b"jpeg bytes"
    assert sent["filename"] == "upload.jpg"  # extension only, see _upload_name()
    assert sent["title"] == "Größe ä IMG_0001"
    assert sent["tags"] == "flickruploader:hash=" + DIGEST
    assert sent["format"] == "etree"  # the upload endpoint only answers in XML
    assert sent["timeout"] == flickr_client.UPLOAD_TIMEOUT
    assert sent["file"].closed  # flickrapi would leave it open
    # Always private (only the owner) and hidden from search; "2" = hide on upload.
    assert (sent["is_public"], sent["is_friend"], sent["is_family"]) == ("0", "0", "0")
    assert sent["hidden"] == "2"


def test_visibility_cannot_be_smuggled_in(client, photo):
    with pytest.raises(TypeError):
        client.upload(photo, title="x", digest=DIGEST, is_public="1")


def test_upload_beyond_260_characters(client, api, tmp_path):
    deep = tmp_path.joinpath(*[f"2026-06 Portugal Urlaub Teil {i}" for i in range(10)])
    deep.mkdir(parents=True)
    path = deep / "IMG_0001.JPG"
    path.write_bytes(b"deep")
    assert len(str(path)) > 300
    client.upload(path, title="IMG_0001", digest=DIGEST)
    assert api.uploads[0]["data"] == b"deep"


@pytest.mark.parametrize(
    "error",
    [
        LEAKY,
        requests.Timeout("read timed out"),
        FlickrError("do_request: Status code 502 received"),
    ],
)
def test_upload_without_an_answer_has_an_unknown_outcome(client, api, photo, sleeps, error):
    api.upload_outcomes = [error]
    with pytest.raises(OutcomeUnknownError) as raised:
        client.upload(photo, title="x", digest=DIGEST)
    assert len(api.uploads) == 1  # never repeated blindly
    assert sleeps == []
    assert "k" * 32 not in str(raised.value)


def test_upload_refused_by_flickr(client, api, photo):
    api.upload_outcomes = [FlickrError("Error: 5: Filetype was not recognised", code=5)]
    with pytest.raises(FlickrRejectedError) as error:
        client.upload(photo, title="x", digest=DIGEST)
    assert error.value.code == 5


def test_upload_with_rejected_login(client, api, photo):
    api.upload_outcomes = [FlickrError("do_request: Status code 401 received")]
    with pytest.raises(FlickrAuthError):
        client.upload(photo, title="x", digest=DIGEST)


def test_missing_file_is_a_local_error_not_an_unknown_upload(client, api, tmp_path):
    with pytest.raises(FileNotFoundError):
        client.upload(tmp_path / "gone.jpg", title="x", digest=DIGEST)
    assert api.uploads == []


def test_upload_checks_the_digest_before_anything(client, api, photo, limiter):
    with pytest.raises(ValueError):
        client.upload(photo, title="x", digest="not a digest")
    assert limiter.acquired == 0


# --- Photo info and hash tags -------------------------------------------------


def test_photo_info_counts_only_the_owners_own_hash_tags(client, api):
    api.handlers["photos.getInfo"] = [
        info(
            tags=[
                own_hash_tag(DIGEST),
                own_hash_tag(OTHER_DIGEST, author=SOMEONE_ELSE),  # added by someone else
                (ME, "flickruploader:hash=nonsense"),  # not a digest
                (ME, "upcoming:event=123"),  # some other machine tag
                (ME, "urlaub"),
            ]
        )
    ]
    result = client.photo_info("555")
    assert result.hash_digests == {DIGEST}
    assert result.is_private
    assert (result.owner_nsid, result.title, result.taken) == (
        ME,
        "IMG_0001",
        "2026-06-01 10:00:00",
    )


def test_photo_info_accepts_the_tag_in_any_case(client, api):
    api.handlers["photos.getInfo"] = [info(tags=[(ME, "FlickrUploader:hash=" + DIGEST.upper())])]
    assert client.photo_info("555").hash_digests == {DIGEST}


def test_photo_info_of_a_missing_photo(client, api):
    api.handlers["photos.getInfo"] = [FlickrError("Error: 1: Photo not found", code=1)]
    assert client.photo_info("555") is None


@pytest.mark.parametrize(
    ("answer", "verified"),
    [
        (info(tags=[own_hash_tag()]), True),
        (info(tags=[]), False),  # tag missing
        (info(tags=[own_hash_tag(OTHER_DIGEST)]), False),  # other file's hash
        (info(owner=SOMEONE_ELSE, tags=[own_hash_tag(author=SOMEONE_ELSE)]), False),
        (FlickrError("Error: 1: Photo not found", code=1), False),
        # Own and tagged, but not private: an upload of this tool must be.
        (info(tags=[own_hash_tag()], visibility={**PRIVATE, "ispublic": 1}), False),
        (info(tags=[own_hash_tag()], visibility={**PRIVATE, "isfamily": "1"}), False),
        (info(tags=[own_hash_tag()], visibility=None), False),  # unknown: not private
    ],
)
def test_verify_upload(client, api, answer, verified):
    api.handlers["photos.getInfo"] = [answer]
    assert client.verify_upload("555", DIGEST) is verified


def test_visibility_given_as_strings(client, api):
    api.handlers["photos.getInfo"] = [
        info(visibility={"ispublic": "0", "isfriend": "0", "isfamily": "0"})
    ]
    assert client.photo_info("555").is_private


def test_make_private(client, api):
    api.handlers["photos.setPerms"] = [{}]
    api.handlers["photos.setSafetyLevel"] = [{}]
    client.make_private("555")
    (perms,) = calls_to(api, "photos.setPerms")
    assert (perms["is_public"], perms["is_friend"], perms["is_family"]) == (0, 0, 0)
    (safety,) = calls_to(api, "photos.setSafetyLevel")
    assert safety["hidden"] == 1  # "hide from search" is 1 here, unlike upload's 2


def test_find_by_hash_trusts_only_the_users_own_tag(client, api):
    api.handlers["photos.search"] = [
        {"photos": {"page": 1, "pages": 1, "total": "2", "photo": [{"id": "1"}, {"id": "2"}]}}
    ]
    api.handlers["photos.getInfo"] = [
        info("1", tags=[own_hash_tag()], visibility={**PRIVATE, "ispublic": 1}),
        info("2", tags=[own_hash_tag(author=SOMEONE_ELSE)]),
    ]
    # Found although public: recovery must find own uploads whatever their state.
    assert client.find_by_hash(DIGEST) == ["1"]
    (search,) = calls_to(api, "photos.search")
    assert search["user_id"] == "me"
    assert search["machine_tags"] == "flickruploader:hash=" + DIGEST


def test_add_hash_tag(client, api):
    api.handlers["photos.addTags"] = [{}]
    client.add_hash_tag("555", DIGEST)
    assert calls_to(api, "photos.addTags") == [
        {"photo_id": "555", "tags": "flickruploader:hash=" + DIGEST, "timeout": 60.0}
    ]


def test_photos_taken_between(client, api):
    api.handlers["photos.search"] = [
        {
            "photos": {
                "page": 1,
                "pages": 2,
                "total": 2,
                "photo": [
                    {
                        "id": "1",
                        "title": "IMG_0001",
                        "datetaken": "2026-06-01 10:00:00",
                        "o_width": "6000",
                        "o_height": "4000",
                    }
                ],
            }
        },
        {"photos": {"page": 2, "pages": 2, "total": 2, "photo": [{"id": "2", "title": "x"}]}},
    ]
    found = client.photos_taken_between("2026-06-01 00:00:00", "2026-06-30 23:59:59")
    assert found == [
        flickr_client.FoundPhoto("1", "IMG_0001", "2026-06-01 10:00:00", 6000, 4000),
        flickr_client.FoundPhoto("2", "x", None, None, None),
    ]
    first = calls_to(api, "photos.search")[0]
    assert first["min_taken_date"] == "2026-06-01 00:00:00"
    assert first["extras"] == "date_taken,o_dims"


def test_search_that_flickr_would_cut_off(client, api):
    api.handlers["photos.search"] = [
        {"photos": {"page": 1, "pages": 9, "total": "4001", "photo": []}}
    ]
    with pytest.raises(flickr_client.SearchTooBroadError):
        client.photos_taken_between("2016-01-01 00:00:00", "2026-12-31 23:59:59")


# --- Albums -------------------------------------------------------------------


def album(album_id, title):
    return {
        "id": album_id,
        "title": {"_content": title},
        "description": {"_content": "Beschreibung"},
        "primary": "555",
        "photos": "12",
        "videos": 1,
    }


def test_albums_are_read_page_by_page(client, api):
    api.handlers["photosets.getList"] = [
        {"photosets": {"page": 1, "pages": "2", "photoset": [album("1", "2026-06 Portugal")]}},
        {"photosets": {"page": 2, "pages": "2", "photoset": [album("2", "2026-07 Wandern")]}},
    ]
    albums = client.albums()
    assert [a.title for a in albums] == ["2026-06 Portugal", "2026-07 Wandern"]
    assert albums[0] == flickr_client.Album("1", "2026-06 Portugal", "Beschreibung", "555", 12, 1)


def test_album_exists(client, api):
    api.handlers["photosets.getInfo"] = [{"photoset": {"id": "72157"}}]
    assert client.album_exists("72157") is True
    api.handlers["photosets.getInfo"] = [FlickrError("Error: 1: Photoset not found", code=1)]
    assert client.album_exists("72157") is False


def test_album_exists_never_guesses_on_failure(client, api, sleeps):
    api.handlers["photosets.getInfo"] = [LEAKY]
    with pytest.raises(FlickrUnavailableError):
        client.album_exists("72157")


def test_create_album(client, api):
    api.handlers["photosets.create"] = [{"photoset": {"id": "72157"}}]
    assert client.create_album("2026-06 Portugal", "555") == "72157"
    (params,) = calls_to(api, "photosets.create")
    assert (params["title"], params["primary_photo_id"]) == ("2026-06 Portugal", "555")


def test_create_album_is_never_repeated_blindly(client, api, sleeps):
    api.handlers["photosets.create"] = [LEAKY, {"photoset": {"id": "72157"}}]
    with pytest.raises(OutcomeUnknownError):
        client.create_album("2026-06 Portugal", "555")
    assert len(calls_to(api, "photosets.create")) == 1
    assert sleeps == []


def test_add_to_album(client, api):
    api.handlers["photosets.addPhoto"] = [{}]
    assert client.add_to_album("72157", "555") is True


def test_add_to_album_twice_is_fine(client, api):
    api.handlers["photosets.addPhoto"] = [FlickrError("Error: 3: Photo already in set", code=3)]
    assert client.add_to_album("72157", "555") is False


def test_add_to_missing_album(client, api):
    api.handlers["photosets.addPhoto"] = [FlickrError("Error: 1: Photoset not found", code=1)]
    with pytest.raises(FlickrRejectedError):
        client.add_to_album("72157", "555")


def test_album_photo_ids(client, api):
    api.handlers["photosets.getPhotos"] = [
        {"photoset": {"page": 1, "pages": 2, "photo": [{"id": "1"}, {"id": "2"}]}},
        {"photoset": {"page": 2, "pages": 2, "photo": [{"id": "3"}]}},
    ]
    assert client.album_photo_ids("72157") == {"1", "2", "3"}
    assert calls_to(api, "photosets.getPhotos")[0]["user_id"] == ME


# --- Downloads ----------------------------------------------------------------


def test_album_media(client, api):
    api.handlers["photosets.getPhotos"] = [
        {
            "photoset": {
                "page": 1,
                "pages": 2,
                "photo": [
                    {
                        "id": "1",
                        "title": "IMG_0001",
                        "media": "photo",
                        "datetaken": "2026-06-01 10:00:00",
                        "datetakenunknown": "0",
                        "url_o": "https://live.staticflickr.com/1_s_o.jpg",
                        "originalformat": "jpg",
                    },
                    {
                        "id": "2",
                        "title": "MVI_0002",
                        "media": "video",
                        "datetaken": "2026-06-02 10:00:00",
                        "datetakenunknown": 0,
                        "url_o": "https://live.staticflickr.com/2_s_o.jpg",
                    },
                ],
            }
        },
        {
            "photoset": {
                "page": 2,
                "pages": 2,
                "photo": [
                    {
                        "id": "3",
                        "title": "",
                        "media": "photo",
                        "datetaken": "2026-10-12 08:00:00",
                        "datetakenunknown": "1",
                    }
                ],
            }
        },
    ]
    items = client.album_media("72157", "all")
    assert items[0] == MediaItem(
        "1",
        "IMG_0001",
        "photo",
        "2026-06-01 10:00:00",
        "https://live.staticflickr.com/1_s_o.jpg",
        "jpg",
    )
    assert items[1].original_url is None  # a video's url_o is only a still image
    assert items[2].taken is None  # Flickr's stand-in is the upload date
    params = calls_to(api, "photosets.getPhotos")[0]
    assert (params["media"], params["user_id"]) == ("all", ME)
    assert {"media", "date_taken", "url_o"} <= set(params["extras"].split(","))


def test_album_media_knows_only_three_filters(client):
    with pytest.raises(ValueError):
        client.album_media("72157", "everything")


def test_media_sizes(client, api):
    api.handlers["photos.getSizes"] = [
        {
            "sizes": {
                "size": [
                    {
                        "label": "Large",
                        "width": "1024",
                        "height": 576,
                        "source": "https://a/b.jpg",
                        "media": "photo",
                    },
                    {
                        "label": "Video Original",
                        "width": "",
                        "height": "",
                        "source": "https://a/orig/",
                        "media": "video",
                    },
                ]
            }
        }
    ]
    sizes = client.media_sizes("555")
    assert sizes[0] == MediaSize("Large", "https://a/b.jpg", 1024, 576, "photo")
    assert (sizes[1].label, sizes[1].width, sizes[1].media) == ("Video Original", None, "video")


SECRET_URL = "https://live.staticflickr.com/video/555/topsecret/orig.mov?s=signature"


class FakeResponse:
    def __init__(self, status=200, chunks=(b"ab", b"cd"), headers=None, error=None):
        self.status_code = status
        self.headers = {"Content-Type": "video/quicktime; codecs=x", "Content-Length": "4"}
        self.headers.update(headers or {})
        self.url = SECRET_URL  # after following the redirect of a "play" address
        self._chunks, self._error = chunks, error
        self.closed = False

    def iter_content(self, size):
        yield from self._chunks
        if self._error:
            raise self._error

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.closed = True


def test_open_download(client, monkeypatch):
    response = FakeResponse()
    requested = {}

    def get(url, **kwargs):
        requested.update(kwargs, url=url)
        return response

    monkeypatch.setattr(client._http, "get", get)
    with client.open_download(SECRET_URL) as download:
        assert (download.content_type, download.length, download.extension) == (
            "video/quicktime",
            4,
            ".mov",
        )
        assert b"".join(download.chunks()) == b"abcd"
    assert response.closed
    assert requested["stream"] is True and requested["timeout"] == flickr_client.DOWNLOAD_TIMEOUT


def test_compressed_download_has_no_length_to_compare(client, monkeypatch):
    response = FakeResponse(headers={"Content-Encoding": "gzip"})
    monkeypatch.setattr(client._http, "get", lambda url, **kwargs: response)
    with client.open_download(SECRET_URL) as download:
        assert download.length is None


@pytest.mark.parametrize(
    "status, expected",
    [
        (404, FlickrRejectedError),
        (403, FlickrRejectedError),
        (429, FlickrUnavailableError),
        (503, FlickrUnavailableError),
    ],
)
def test_download_errors_never_show_the_address(client, monkeypatch, status, expected):
    monkeypatch.setattr(client._http, "get", lambda url, **kwargs: FakeResponse(status=status))
    with pytest.raises(expected) as raised, client.open_download(SECRET_URL):
        pass
    assert "topsecret" not in str(raised.value)


def test_lost_connection_never_shows_the_address(client, monkeypatch):
    leaky = requests.ConnectionError(f"Max retries exceeded with url: {SECRET_URL}")
    monkeypatch.setattr(client._http, "get", lambda url, **kwargs: (_ for _ in ()).throw(leaky))
    with pytest.raises(FlickrUnavailableError) as raised, client.open_download(SECRET_URL):
        pass
    assert "topsecret" not in str(raised.value)

    response = FakeResponse(error=requests.exceptions.ChunkedEncodingError(SECRET_URL))
    monkeypatch.setattr(client._http, "get", lambda url, **kwargs: response)
    with (
        pytest.raises(FlickrUnavailableError) as raised,
        client.open_download(SECRET_URL) as download,
    ):
        b"".join(download.chunks())
    assert "topsecret" not in str(raised.value)


def test_rename_album_sends_the_description_along(client, api):
    api.handlers["photosets.editMeta"] = [{}]
    client.rename_album("72157", "2026-06 Portugal", "Beschreibung")
    (params,) = calls_to(api, "photosets.editMeta")
    assert (params["title"], params["description"]) == ("2026-06 Portugal", "Beschreibung")


# --- Construction -------------------------------------------------------------


def test_client_needs_the_user_id(api):
    with pytest.raises(ValueError):
        FlickrClient(api, "")


def test_from_store(store):
    store.save_api_credentials(auth.ApiCredentials("a" * 32, "b" * 16))
    store.save_token(auth.AccessToken("fake-token", "fake-secret", "write", user_nsid=ME))
    assert FlickrClient.from_store(store).user_nsid == ME


class CapturedPost:
    """Stands in for the upload POST of the real flickrapi session."""

    def __init__(self, status=200, content=UPLOAD_OK):
        self.status, self.content = status, content.encode()
        self.headers = self.body = None

    def __call__(self, url, data=None, headers=None, timeout=None):
        self.url, self.headers, self.timeout = url, headers, timeout
        self.body = data.to_string()  # read while the file is still open
        return type("Response", (), {"status_code": self.status, "content": self.content})()


@pytest.fixture
def real_client(store, monkeypatch):
    """A client on a real flickrapi object; only the upload POST is replaced."""
    store.save_api_credentials(auth.ApiCredentials("a" * 32, "b" * 16))
    store.save_token(auth.AccessToken("fake-token", "fake-secret", "write", user_nsid=ME))
    client = FlickrClient.from_store(store, limiter=CountingLimiter())
    post = CapturedPost()
    monkeypatch.setattr(client._api.flickr_oauth.session, "post", post)
    return client, post


def test_upload_sends_the_file_extension(real_client, tmp_path):
    client, post = real_client
    video = tmp_path / "00012 Müll.MTS"
    video.write_bytes(b"\x00\x00\x00\x00G mpeg transport stream")
    assert client.upload(video, title="00012 Müll", digest=DIGEST) == "555"
    # flickrapi itself would send 'dummy name', and Flickr can't place an MTS
    # file without its extension (error 5).
    assert b'filename="upload.mts"' in post.body
    assert b"dummy name" not in post.body
    assert b"G mpeg transport stream" in post.body
    assert post.url == client._api.UPLOAD_URL
    assert post.timeout == flickr_client.UPLOAD_TIMEOUT
    signature = post.headers["Authorization"]  # bytes, as flickrapi sends it
    assert signature.startswith(b"OAuth ") and b"oauth_signature=" in signature
    for name, value in [("is_public", b"0"), ("is_friend", b"0"), ("hidden", b"2")]:
        assert f'name="{name}"'.encode() + b"\r\n\r\n" + value in post.body
    assert ("flickruploader:hash=" + DIGEST).encode() in post.body


def test_real_upload_refused_by_flickr(real_client, tmp_path):
    client, post = real_client
    post.content = b'<rsp stat="fail"><err code="5" msg="Filetype was not recognised" /></rsp>'
    (tmp_path / "x.mts").write_bytes(b"x")
    with pytest.raises(FlickrRejectedError) as error:
        client.upload(tmp_path / "x.mts", title="x", digest=DIGEST)
    assert error.value.code == 5


def test_real_upload_with_rejected_login(real_client, tmp_path):
    client, post = real_client
    post.status = 401
    (tmp_path / "x.jpg").write_bytes(b"x")
    with pytest.raises(FlickrAuthError):
        client.upload(tmp_path / "x.jpg", title="x", digest=DIGEST)


def test_from_store_without_user_id(store):
    store.save_api_credentials(auth.ApiCredentials("a" * 32, "b" * 16))
    store.save_token(auth.AccessToken("fake-token", "fake-secret", "write"))
    with pytest.raises(auth.NotConfiguredError):
        FlickrClient.from_store(store)
