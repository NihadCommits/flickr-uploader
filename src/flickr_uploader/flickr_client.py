"""The only place that talks to Flickr.

Wraps flickrapi (unmaintained since 2018) so that replacing it only touches
this module, and turns its mixed error signals into a few clear cases:

- FlickrUnavailableError: network trouble or Flickr overloaded. Try again later.
- FlickrAuthError: the login is no longer accepted. Stop and set up again.
- FlickrRejectedError: Flickr said no, with an error code. Retrying won't help.
- OutcomeUnknownError: a call that must not be repeated blindly (upload,
  creating an album) failed without an answer. Flickr may have done it anyway;
  look it up (hash tag, album list) before trying again.

Only calls that can safely be repeated are retried automatically. Every call
passes the rate limiter, and no error message carries request URLs (they can
contain the API key).
"""

import os
import re
import threading
import time
import types
from collections import deque
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from urllib.parse import urlparse

import flickrapi
import requests
from flickrapi.exceptions import FlickrError
from requests_toolbelt import MultipartEncoder

from flickr_uploader import auth, config, hashing
from flickr_uploader.hashing import CancelFlag

HASH_TAG_PREFIX = "flickruploader:hash="
# Waits before the 2nd and 3rd attempt of a repeatable call.
RETRY_DELAYS = (2.0, 8.0)
CALL_TIMEOUT = 60.0
# Covers waiting for Flickr to process a large video after it was sent.
UPLOAD_TIMEOUT = 900.0
# Connect, and wait for the next bytes of a file download.
DOWNLOAD_TIMEOUT = (30.0, 300.0)
DOWNLOAD_CHUNK = 1024 * 1024
PAGE_SIZE = 500
ALBUM_MEDIA = ("all", "photos", "videos")

_AUTH_CODES = frozenset({96, 97, 98, 99, 100})  # signature, login, permission, API key
_TRANSIENT_CODES = frozenset({105, 106})  # service unavailable, write operation failed
_PHOTO_NOT_FOUND = 1
_ALBUM_NOT_FOUND = 1
_ALREADY_IN_ALBUM = 3
_VISIBILITY_KEYS = ("ispublic", "isfriend", "isfamily")
_HTTP_STATUS = re.compile(r"Status code (\d{3})")


class FlickrClientError(Exception):
    """Base class for errors raised by this module."""


class FlickrUnavailableError(FlickrClientError):
    """Network trouble or Flickr overloaded; worth trying again later."""


class FlickrAuthError(FlickrClientError):
    """Flickr no longer accepts the login; setup has to be run again."""


class FlickrRejectedError(FlickrClientError):
    """Flickr refused the request; repeating it won't change that."""

    def __init__(self, message: str, code: int | None = None) -> None:
        super().__init__(message)
        self.code = code


class OutcomeUnknownError(FlickrClientError):
    """A non-repeatable call failed without an answer; it may have succeeded."""


class SearchTooBroadError(FlickrClientError):
    """The search has more results than Flickr returns; narrow it down."""


class CancelledError(FlickrClientError):
    """The caller asked to stop while waiting."""


def hash_tag(digest: str) -> str:
    """The machine tag that marks a photo with the hash of its file."""
    if not hashing.is_valid_digest(digest):
        # Anything else could inject further tags into the space-separated list.
        raise ValueError("not a valid digest")
    return HASH_TAG_PREFIX + digest


# Every upload is private (only the owner) and hidden from public searches.
# Deliberately not configurable: making photos visible is a decision the user
# takes later on Flickr, independent of this tool.
# Careful: "hide from search" is hidden=2 on upload, but hidden=1 in setSafetyLevel.
_PRIVATE_UPLOAD = {"is_public": "0", "is_friend": "0", "is_family": "0", "hidden": "2"}


@dataclass(frozen=True)
class PhotoInfo:
    id: str
    owner_nsid: str
    title: str
    taken: str | None
    # Digests of hash tags the owner set; tags by other members don't count.
    hash_digests: frozenset[str]
    # Visible to nobody but the owner (not public, not friends, not family).
    is_private: bool


@dataclass(frozen=True)
class FoundPhoto:
    id: str
    title: str
    taken: str | None  # "YYYY-MM-DD HH:MM:SS", camera local time
    width: int | None
    height: int | None


@dataclass(frozen=True)
class Album:
    id: str
    title: str
    description: str
    primary_photo_id: str
    photo_count: int
    video_count: int


@dataclass(frozen=True)
class MediaItem:
    """A photo or video of an album, as listed for a download."""

    id: str
    title: str
    media: str  # "photo" or "video"
    taken: str | None  # "YYYY-MM-DD HH:MM:SS", camera local time; None: Flickr doesn't know
    # Photos only (for videos url_o is a still image): the original file, if offered.
    original_url: str | None
    original_format: str | None  # e.g. "jpg"


@dataclass(frozen=True)
class MediaSize:
    """One version of a photo or video that Flickr offers (photos.getSizes)."""

    label: str  # e.g. "Original", "Large 2048", "Video Original", "1080p"
    url: str
    width: int | None
    height: int | None
    media: str  # "photo" or "video"; a video also has still images


class Download:
    """An open file download: what Flickr sent, and the content in chunks.

    Only the extension of the address is kept, never the address itself: it
    contains the photo's secret.
    """

    def __init__(self, response) -> None:
        self._response = response
        headers = response.headers
        self.content_type = headers.get("Content-Type", "").split(";")[0].strip().lower()
        length = headers.get("Content-Length", "")
        # A compressed transfer is unpacked on the way, so its length says nothing.
        plain = headers.get("Content-Encoding", "identity").lower() in ("", "identity")
        self.length = int(length) if length.isdigit() and plain else None
        self.extension = os.path.splitext(urlparse(response.url).path)[1].lower()

    def chunks(self, size: int = DOWNLOAD_CHUNK) -> Iterator[bytes]:
        try:
            yield from self._response.iter_content(size)
        except requests.RequestException as error:
            raise FlickrUnavailableError(
                f"connection lost during the download ({type(error).__name__})"
            ) from None


class RateLimiter:
    """At most `limit` calls in any `window` seconds (sliding window), thread-safe."""

    def __init__(
        self,
        limit: int = config.API_CALL_BUDGET_PER_HOUR,
        window: float = 3600.0,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.limit, self.window = limit, window
        self._clock, self._sleep = clock, sleep
        self._calls: deque[float] = deque()
        self._lock = threading.Lock()

    def acquire(self, cancel: CancelFlag | None = None) -> None:
        while True:
            with self._lock:
                now = self._clock()
                while self._calls and now - self._calls[0] >= self.window:
                    self._calls.popleft()
                if len(self._calls) < self.limit:
                    self._calls.append(now)
                    return
                wait = self.window - (now - self._calls[0])
            if cancel is not None and cancel.is_set():
                raise CancelledError("cancelled while waiting for the rate limit")
            # Short naps, so a cancel is noticed within a second.
            self._sleep(min(wait, 1.0))


class FlickrClient:
    def __init__(
        self,
        api,
        user_nsid: str,
        *,
        limiter: RateLimiter | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not user_nsid:
            raise ValueError("user_nsid is required to tell own tags from others'")
        if isinstance(api, flickrapi.FlickrAPI):
            _send_file_type(api)
        self._api = api
        self.user_nsid = user_nsid
        self._limiter = limiter if limiter is not None else RateLimiter()
        self._sleep = sleep
        self._http = requests.Session()  # for file downloads, which need no login

    @classmethod
    def from_store(cls, store: auth.CredentialStore | None = None, **kwargs) -> "FlickrClient":
        """Client logged in with the credentials from the Credential Manager."""
        store = store if store is not None else auth.CredentialStore()
        api = auth.flickr_api(store)
        token = store.load_token()
        if not token.user_nsid:
            raise auth.NotConfiguredError("the stored login lacks the user id; run setup again")
        return cls(api, token.user_nsid, **kwargs)

    # --- Photos ---------------------------------------------------------------

    def upload(
        self,
        path: str | os.PathLike,
        *,
        title: str,
        digest: str,
        cancel: CancelFlag | None = None,
    ) -> str:
        """Upload a file, private and hidden from search, tagged with its hash.

        Returns the new photo id. Never retried here: OutcomeUnknownError means
        the photo may exist on Flickr anyway, search for its hash tag before
        uploading again.
        """
        params = {"title": title, "tags": hash_tag(digest), **_PRIVATE_UPLOAD}
        self._limiter.acquire(cancel)
        # Opened here, not by flickrapi: it would never close the file, and a
        # local error (file gone) must not look like an unknown upload outcome.
        with open(config.filesystem_path(path), "rb") as file:
            try:
                response = self._api.upload(
                    _upload_name(path),
                    fileobj=file,
                    timeout=UPLOAD_TIMEOUT,
                    format="etree",  # the upload endpoint only answers in XML
                    **params,
                )
            except (FlickrError, requests.RequestException, OSError) as error:
                failure = _classify(error)
                if isinstance(failure, FlickrUnavailableError):
                    raise OutcomeUnknownError(
                        f"upload of {os.path.basename(path)} got no answer: {failure}"
                    ) from None
                raise failure from None
        return response.find("photoid").text

    def photo_info(self, photo_id: str) -> PhotoInfo | None:
        """Details of a photo, or None if it does not exist (any more)."""
        try:
            photo = self._call("photos.getInfo", photo_id=photo_id)["photo"]
        except FlickrRejectedError as error:
            if error.code == _PHOTO_NOT_FOUND:
                return None
            raise
        owner = photo["owner"]["nsid"]
        digests = set()
        for tag in photo.get("tags", {}).get("tag", []):
            raw = tag.get("raw", "")
            if tag.get("author") != owner or not raw.lower().startswith(HASH_TAG_PREFIX):
                continue
            digest = raw[len(HASH_TAG_PREFIX) :].lower()
            if hashing.is_valid_digest(digest):
                digests.add(digest)
        # A missing flag counts as "visible": never assume the safe case.
        visibility = photo.get("visibility", {})
        return PhotoInfo(
            id=photo["id"],
            owner_nsid=owner,
            title=photo.get("title", {}).get("_content", ""),
            taken=photo.get("dates", {}).get("taken"),
            hash_digests=frozenset(digests),
            is_private=all(int(visibility.get(key, 1)) == 0 for key in _VISIBILITY_KEYS),
        )

    def is_own_photo(self, info: PhotoInfo | None, digest: str) -> bool:
        """The photo exists, belongs to the user and carries the user's hash tag."""
        return (
            info is not None and info.owner_nsid == self.user_nsid and digest in info.hash_digests
        )

    def verify_upload(self, photo_id: str, digest: str) -> bool:
        """True if an upload by this tool arrived as it must: own, tagged and private.

        A photo that is own and tagged but not private can be fixed with
        make_private() and verified again.
        """
        info = self.photo_info(photo_id)
        return self.is_own_photo(info, digest) and info.is_private

    def make_private(self, photo_id: str) -> None:
        """Make one of the tool's own uploads private and hidden from search again.

        Only meant for uploads of this tool that did not arrive private; the
        tool never changes the visibility of photos it did not upload.
        """
        self._call("photos.setPerms", photo_id=photo_id, is_public=0, is_friend=0, is_family=0)
        self._call("photos.setSafetyLevel", photo_id=photo_id, hidden=1)  # 1 = hide here

    def find_by_hash(self, digest: str) -> list[str]:
        """Ids of the user's photos that carry the user's own hash tag for `digest`.

        Flickr's search index lags behind (measured: 26 s): a photo uploaded
        moments ago may not be found yet.
        """
        found = self._search(machine_tags=hash_tag(digest))
        return sorted(
            photo["id"]
            for photo in found
            if self.is_own_photo(self.photo_info(photo["id"]), digest)
        )

    def add_hash_tag(self, photo_id: str, digest: str) -> None:
        """Tag an existing photo (e.g. one uploaded before this tool) with its hash."""
        self._call("photos.addTags", photo_id=photo_id, tags=hash_tag(digest))

    def photos_taken_between(self, start: str, end: str) -> list[FoundPhoto]:
        """The user's photos taken in [start, end] ("YYYY-MM-DD HH:MM:SS")."""
        found = self._search(min_taken_date=start, max_taken_date=end, extras="date_taken,o_dims")
        return [
            FoundPhoto(
                id=photo["id"],
                title=photo.get("title", ""),
                taken=photo.get("datetaken"),
                width=_int_or_none(photo.get("o_width")),
                height=_int_or_none(photo.get("o_height")),
            )
            for photo in found
        ]

    def _search(self, **criteria) -> list[dict]:
        photos, page = [], 1
        while True:
            result = self._call(
                "photos.search",
                user_id="me",
                media="all",
                per_page=PAGE_SIZE,
                page=page,
                **criteria,
            )["photos"]
            if int(result["total"]) > config.MAX_SEARCH_RESULTS:
                raise SearchTooBroadError(
                    f"{result['total']} results, Flickr only returns {config.MAX_SEARCH_RESULTS}"
                )
            photos.extend(result["photo"])
            if page >= int(result["pages"]):
                return photos
            page += 1

    # --- Albums ---------------------------------------------------------------

    def albums(self) -> list[Album]:
        albums, page = [], 1
        while True:
            result = self._call("photosets.getList", per_page=PAGE_SIZE, page=page)["photosets"]
            albums.extend(
                Album(
                    id=album["id"],
                    title=album["title"]["_content"],
                    description=album["description"]["_content"],
                    primary_photo_id=album["primary"],
                    photo_count=int(album["photos"]),
                    video_count=int(album.get("videos", 0)),
                )
                for album in result["photoset"]
            )
            if page >= int(result["pages"]):
                return albums
            page += 1

    def album_exists(self, album_id: str) -> bool:
        """False only if Flickr says the album does not exist (any more).

        Meant to double-check an album missing from albums() before treating it
        as deleted; any other failure raises instead of answering False.
        """
        try:
            self._call("photosets.getInfo", photoset_id=album_id)
        except FlickrRejectedError as error:
            if error.code == _ALBUM_NOT_FOUND:
                return False
            raise
        return True

    def create_album(self, title: str, primary_photo_id: str, description: str = "") -> str:
        """Create an album and return its id. Flickr needs a first photo for it.

        Never retried: OutcomeUnknownError means the album may exist anyway;
        look for it (same title and primary photo) before creating it again.
        """
        try:
            result = self._call(
                "photosets.create",
                retry=False,
                title=title,
                description=description,
                primary_photo_id=primary_photo_id,
            )
        except FlickrUnavailableError as error:
            raise OutcomeUnknownError(f"creating album got no answer: {error}") from None
        return result["photoset"]["id"]

    def add_to_album(self, album_id: str, photo_id: str) -> bool:
        """Put a photo into an album. False if it was already there (not an error)."""
        try:
            self._call("photosets.addPhoto", photoset_id=album_id, photo_id=photo_id)
        except FlickrRejectedError as error:
            if error.code == _ALREADY_IN_ALBUM:
                return False
            raise
        return True

    def album_photo_ids(self, album_id: str) -> set[str]:
        ids, page = set(), 1
        while True:
            result = self._call(
                "photosets.getPhotos",
                photoset_id=album_id,
                user_id=self.user_nsid,
                media="all",
                per_page=PAGE_SIZE,
                page=page,
            )["photoset"]
            ids.update(photo["id"] for photo in result["photo"])
            if page >= int(result["pages"]):
                return ids
            page += 1

    def rename_album(self, album_id: str, title: str, description: str) -> None:
        """Rename an album. The description is always sent along, because the
        API docs leave open whether leaving it out clears it."""
        self._call("photosets.editMeta", photoset_id=album_id, title=title, description=description)

    # --- Downloads ------------------------------------------------------------

    def album_media(self, album_id: str, media: str = "all") -> list[MediaItem]:
        """The photos and/or videos of an album ("all", "photos" or "videos")."""
        if media not in ALBUM_MEDIA:
            raise ValueError(f"media must be one of {ALBUM_MEDIA}")
        items, page = [], 1
        while True:
            result = self._call(
                "photosets.getPhotos",
                photoset_id=album_id,
                user_id=self.user_nsid,
                media=media,
                extras="media,date_taken,original_format,url_o",
                per_page=PAGE_SIZE,
                page=page,
            )["photoset"]
            items.extend(_media_item(photo) for photo in result["photo"])
            if page >= int(result["pages"]):
                return items
            page += 1

    def media_sizes(self, photo_id: str) -> list[MediaSize]:
        """Every version of a photo or video Flickr offers, the original included."""
        sizes = self._call("photos.getSizes", photo_id=photo_id)["sizes"]["size"]
        return [
            MediaSize(
                label=size["label"],
                url=size["source"],
                width=_int_or_none(size.get("width")),
                height=_int_or_none(size.get("height")),
                media=size.get("media", "photo"),
            )
            for size in sizes
        ]

    @contextmanager
    def open_download(self, url: str) -> Iterator[Download]:
        """Stream a file from Flickr's servers (a URL from album_media or media_sizes).

        Not an API call, so not rate limited. Errors never carry the URL.
        """
        try:
            response = self._http.get(url, stream=True, timeout=DOWNLOAD_TIMEOUT)
        except requests.RequestException as error:
            raise FlickrUnavailableError(
                f"no connection to Flickr ({type(error).__name__})"
            ) from None
        with response:
            status = response.status_code
            if status == 429 or status >= 500:
                raise FlickrUnavailableError(f"Flickr answered HTTP {status}")
            if status != 200:
                raise FlickrRejectedError(f"Flickr answered HTTP {status}")
            yield Download(response)

    # --- Plumbing -------------------------------------------------------------

    def _call(self, method: str, *, retry: bool = True, cancel: CancelFlag | None = None, **params):
        target = self._api
        for part in method.split("."):
            target = getattr(target, part)
        delays = RETRY_DELAYS if retry else ()
        for attempt in range(len(delays) + 1):
            self._limiter.acquire(cancel)
            try:
                return target(timeout=CALL_TIMEOUT, **params)
            except (FlickrError, requests.RequestException, OSError) as error:
                failure = _classify(error)
                if not isinstance(failure, FlickrUnavailableError) or attempt == len(delays):
                    # "from None": the original may show a request URL.
                    raise failure from None
            if cancel is not None and cancel.is_set():
                raise CancelledError(f"cancelled while retrying {method}")
            self._sleep(delays[attempt])
        raise AssertionError("unreachable")  # pragma: no cover


def _classify(error: Exception) -> FlickrClientError:
    if isinstance(error, FlickrError):
        code = error.code
        if code is None:
            match = _HTTP_STATUS.search(str(error))
            status = int(match.group(1)) if match else None
            if status == 401:
                return FlickrAuthError("Flickr rejected the login (HTTP 401)")
            if status is not None and (status == 429 or status >= 500):
                return FlickrUnavailableError(f"Flickr answered HTTP {status}")
            return FlickrRejectedError(f"Flickr refused the request (HTTP {status})")
        if code in _AUTH_CODES:
            return FlickrAuthError(f"Flickr rejected the login (error {code}); run setup again")
        if code in _TRANSIENT_CODES:
            return FlickrUnavailableError(f"Flickr is temporarily unavailable (error {code})")
        return FlickrRejectedError(str(error), code=code)
    # requests' errors and socket errors: only the type, never the message,
    # which can contain the request URL.
    return FlickrUnavailableError(f"no connection to Flickr ({type(error).__name__})")


def _media_item(photo: dict) -> MediaItem:
    media = photo.get("media", "photo")
    unknown = str(photo.get("datetakenunknown", "0")) == "1"
    return MediaItem(
        id=photo["id"],
        title=photo.get("title", ""),
        media=media,
        # Without a known date Flickr reports the upload time here; that is no taken date.
        taken=None if unknown else (photo.get("datetaken") or None),
        original_url=photo.get("url_o") if media == "photo" else None,
        original_format=photo.get("originalformat"),
    )


def _int_or_none(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _upload_name(path: str | os.PathLike) -> str:
    """The file name sent with an upload: plain ASCII, with the real extension.

    Flickr tells the file type by the extension where the content alone is not
    enough. The real name is not needed (the title is sent separately) and
    would only add a chance for encoding trouble.
    """
    return "upload" + os.path.splitext(os.fspath(path))[1].lower()


def _send_file_type(api: flickrapi.FlickrAPI) -> None:
    """Make this flickrapi instance send the file name along with an upload.

    flickrapi sends every file as 'dummy name', so Flickr has to guess the type
    from the content. For some videos it can't: MTS files fail with error 5,
    "Filetype was not recognised", although Flickr's web uploader, which sends
    the name, takes them.
    """
    api.flickr_oauth.do_upload = types.MethodType(_do_upload, api.flickr_oauth)


def _do_upload(self, filename, url, params=None, fileobj=None, timeout=None):
    """flickrapi 2.4.0's OAuthFlickrInterface.do_upload, but naming the file `filename`."""
    if fileobj is None:
        raise ValueError("the caller opens and closes the file")
    # Flickr expects the file to be left out of the OAuth signature: sign a
    # request without it, then send the real one with that signature.
    signed = requests.Request("POST", url, data=params, auth=self.oauth).prepare()
    body = MultipartEncoder(
        fields={**params, "photo": (filename, fileobj, "application/octet-stream")}
    )
    headers = {
        "Authorization": signed.headers.get("Authorization"),
        "Content-Type": body.content_type,
    }
    response = self.session.post(
        url, data=body, headers=headers, timeout=timeout or self.default_timeout
    )
    if response.status_code != 200:
        # Same wording as flickrapi, which _classify() reads the status from.
        raise FlickrError(f"do_upload: Status code {response.status_code} received")
    return response.content
