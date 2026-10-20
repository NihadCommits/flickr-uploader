"""An in-memory Flickr for the album, uploader and downloader tests.

Implements the part of FlickrClient that albums.py, precheck.py, uploader.py
and downloader.py use, with Flickr's behavior where it matters: new ids, hash
tags, visibility, albums that need a cover photo, "already in the album" being
fine, originals next to converted versions, file URLs carrying a secret.

Trouble is scripted per method:
- fail_before[method]: errors raised before anything happens on "Flickr"
- lose_answer[method]: how many calls do their work, then raise
  OutcomeUnknownError as if the answer got lost
"""

import os
from collections import defaultdict
from contextlib import contextmanager
from dataclasses import dataclass, field
from urllib.parse import urlparse

from flickr_uploader import config
from flickr_uploader.flickr_client import (
    Album,
    FlickrClient,
    FlickrRejectedError,
    FlickrUnavailableError,
    MediaItem,
    MediaSize,
    OutcomeUnknownError,
    PhotoInfo,
)

ME = "12345678@N00"
SECRET = "s3cr3t"  # in every file URL; must never show up in a log


@dataclass
class FakePhoto:
    id: str
    title: str
    digests: set[str]
    private: bool = True
    owner: str = ME
    media: str = "photo"
    taken: str | None = None
    original_url: str | None = None
    original_format: str = "jpg"
    sizes: list[MediaSize] = field(default_factory=list)


@dataclass
class FakeFile:
    """A file on Flickr's servers."""

    content: bytes
    content_type: str
    length: int | None = None  # announced length, if not len(content)
    cut: bool = False  # the connection drops after the first chunk


class FakeDownload:
    def __init__(self, url, served):
        self._served = served
        self.content_type = served.content_type
        self.length = served.length if served.length is not None else len(served.content)
        self.extension = os.path.splitext(urlparse(url).path)[1].lower()

    def chunks(self, size=None):
        content = self._served.content
        middle = len(content) // 2
        yield content[:middle]
        if self._served.cut:
            raise FlickrUnavailableError("connection lost during the download (ConnectionError)")
        yield content[middle:]


@dataclass
class FakeAlbum:
    id: str
    title: str
    primary: str
    photos: list[str]
    description: str = ""


@dataclass
class FakeFlickr:
    user_nsid: str = ME
    photos: dict[str, FakePhoto] = field(default_factory=dict)
    sets: dict[str, FakeAlbum] = field(default_factory=dict)
    old_photos: list = field(default_factory=list)  # FoundPhoto, for the pre-check
    calls: list[str] = field(default_factory=list)
    fail_before: dict[str, list[BaseException]] = field(default_factory=lambda: defaultdict(list))
    lose_answer: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    upload_public: int = 0  # this many uploads arrive public (Flickr misbehaving)
    on_upload: object = None  # callback(photo_id), e.g. to cancel a run midway
    served: dict[str, FakeFile] = field(default_factory=dict)  # URL -> file
    on_download: object = None  # callback(url)
    _next_id: int = 5000

    # The real ownership test: it only needs user_nsid.
    is_own_photo = FlickrClient.is_own_photo

    # --- Photos -------------------------------------------------------------------

    def upload(self, path, *, title, digest, cancel=None):
        self._enter("upload")
        with open(config.filesystem_path(path), "rb") as file:
            file.read()
        photo_id = self._new_id()
        public = self.upload_public > 0
        self.upload_public -= public
        self.photos[photo_id] = FakePhoto(photo_id, title, {digest}, private=not public)
        if self.on_upload is not None:
            self.on_upload(photo_id)
        self._answer("upload")
        return photo_id

    def photo_info(self, photo_id):
        self._enter("photo_info")
        photo = self.photos.get(photo_id)
        if photo is None:
            return None
        return PhotoInfo(
            id=photo.id,
            owner_nsid=photo.owner,
            title=photo.title,
            taken=None,
            hash_digests=frozenset(photo.digests),
            is_private=photo.private,
        )

    def make_private(self, photo_id):
        self._enter("make_private")
        self.photos[photo_id].private = True

    def find_by_hash(self, digest):
        self._enter("find_by_hash")
        return sorted(p.id for p in self.photos.values() if digest in p.digests and p.owner == ME)

    def add_hash_tag(self, photo_id, digest):
        self._enter("add_hash_tag")
        self._photo(photo_id).digests.add(digest)

    def photos_taken_between(self, start, end):
        self._enter("photos_taken_between")
        return [p for p in self.old_photos if start <= p.taken <= end]

    # --- Albums -------------------------------------------------------------------

    def albums(self):
        self._enter("albums")
        return [
            Album(
                a.id,
                a.title,
                a.description,
                a.primary,
                sum(self.photos[p].media == "photo" for p in a.photos if p in self.photos),
                sum(self.photos[p].media == "video" for p in a.photos if p in self.photos),
            )
            for a in self.sets.values()
        ]

    def album_exists(self, album_id):
        self._enter("album_exists")
        return album_id in self.sets

    def create_album(self, title, primary_photo_id, description=""):
        self._enter("create_album")
        self._photo(primary_photo_id)
        album_id = self._new_id(prefix="72157")
        self.sets[album_id] = FakeAlbum(album_id, title, primary_photo_id, [primary_photo_id])
        self._answer("create_album")
        return album_id

    def add_to_album(self, album_id, photo_id):
        self._enter("add_to_album")
        album = self.sets.get(album_id)
        if album is None:
            raise FlickrRejectedError("Error: 1: Photoset not found", code=1)
        self._photo(photo_id)
        if photo_id in album.photos:
            return False
        album.photos.append(photo_id)
        return True

    def album_photo_ids(self, album_id):
        self._enter("album_photo_ids")
        return set(self.sets[album_id].photos)

    def rename_album(self, album_id, title, description):
        self._enter("rename_album")
        album = self.sets[album_id]
        album.title, album.description = title, description

    # --- Downloads ----------------------------------------------------------------

    def album_media(self, album_id, media="all"):
        self._enter("album_media")
        album = self.sets.get(album_id)
        if album is None:
            raise FlickrRejectedError("Error: 1: Photoset not found", code=1)
        wanted = {"all": {"photo", "video"}, "photos": {"photo"}, "videos": {"video"}}[media]
        return [
            MediaItem(
                p.id,
                p.title,
                p.media,
                p.taken,
                p.original_url if p.media == "photo" else None,
                p.original_format,
            )
            for p in (self.photos[i] for i in album.photos)
            if p.media in wanted
        ]

    def media_sizes(self, photo_id):
        self._enter("media_sizes")
        return list(self._photo(photo_id).sizes)

    @contextmanager
    def open_download(self, url):
        self._enter("open_download")
        if self.on_download is not None:
            self.on_download(url)
        served = self.served.get(url)
        if served is None:
            raise FlickrRejectedError("Flickr answered HTTP 404")
        yield FakeDownload(url, served)

    # --- Helpers for tests ----------------------------------------------------------

    def add_media(
        self,
        album_id,
        title,
        *,
        media="photo",
        taken="2026-06-01 10:00:00",
        original=b"original",
        converted=b"converted",
        original_type=None,
    ) -> str:
        """A photo or video in an album, with the versions Flickr would offer.

        original=None: Flickr offers no original, only converted versions.
        """
        photo_id = self._new_id()
        photo = FakePhoto(photo_id, title, set(), media=media, taken=taken)
        base = f"https://live.staticflickr.com/65535/{photo_id}_{SECRET}"
        if media == "photo":
            large = self._serve(f"{base}_k.jpg", converted, "image/jpeg")
            medium = self._serve(f"{base}_z.jpg", b"medium", "image/jpeg")
            photo.sizes = [
                MediaSize("Medium 640", medium, 640, 480, "photo"),
                MediaSize("Large 2048", large, 2048, 1536, "photo"),
            ]
            if original is not None:
                photo.original_url = self._serve(
                    f"{base}_o.jpg", original, original_type or "image/jpeg"
                )
                photo.sizes.append(MediaSize("Original", photo.original_url, 4000, 3000, "photo"))
        else:
            video = f"https://live.staticflickr.com/video/{photo_id}/{SECRET}"
            photo.sizes = [
                MediaSize(
                    "Video Player",
                    f"https://www.flickr.com/apps/video/{photo_id}",
                    640,
                    360,
                    "video",
                ),
                MediaSize(
                    "Large",
                    self._serve(f"{base}_b.jpg", b"still", "image/jpeg"),
                    1024,
                    576,
                    "photo",
                ),
                MediaSize(
                    "Site MP4",
                    self._serve(f"{video}/site.mp4?s=1", b"small", "video/mp4"),
                    640,
                    360,
                    "video",
                ),
                MediaSize(
                    "1080p",
                    self._serve(f"{video}/1080p.mp4?s=1", converted, "video/mp4"),
                    1920,
                    1080,
                    "video",
                ),
            ]
            if original is not None:
                # The real "play" URL redirects; its address has no extension.
                url = f"https://www.flickr.com/photos/{ME}/{photo_id}/play/orig/{SECRET}/"
                self._serve(url, original, original_type or "video/quicktime")
                photo.sizes.append(MediaSize("Video Original", url, None, None, "video"))
        self.photos[photo_id] = photo
        self.sets[album_id].photos.append(photo_id)
        return photo_id

    def _serve(self, url, content, content_type):
        self.served[url] = FakeFile(content, content_type)
        return url

    def add_photo(self, title="old", digests=(), private=True, owner=ME) -> str:
        photo_id = self._new_id()
        self.photos[photo_id] = FakePhoto(photo_id, title, set(digests), private, owner)
        return photo_id

    def add_album(self, title, primary=None, description="") -> str:
        primary = primary or self.add_photo()
        album_id = self._new_id(prefix="72157")
        self.sets[album_id] = FakeAlbum(album_id, title, primary, [primary], description)
        return album_id

    def count(self, method) -> int:
        return self.calls.count(method)

    def _enter(self, method):
        self.calls.append(method)
        if self.fail_before[method]:
            raise self.fail_before[method].pop(0)

    def _answer(self, method):
        if self.lose_answer[method] > 0:
            self.lose_answer[method] -= 1
            raise OutcomeUnknownError(f"{method} got no answer")

    def _photo(self, photo_id):
        if photo_id not in self.photos:
            raise FlickrRejectedError("Error: 1: Photo not found", code=1)
        return self.photos[photo_id]

    def _new_id(self, prefix=""):
        self._next_id += 1
        return f"{prefix}{self._next_id}"
