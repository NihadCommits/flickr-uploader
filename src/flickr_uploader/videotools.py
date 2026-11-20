"""ffmpeg for videos Flickr would not take as they are (spec, "Zu große und lange Videos").

- probe(): length, picture size and whether there is sound, from `ffmpeg -i`
- compress(): H.264 + AAC in MP4, sized to fit under Flickr's limit
- cut(): one piece of a video without re-encoding (stream copy)

ffmpeg comes with the imageio-ffmpeg package. It runs without a console window
and is stopped at once when the run is paused. Inputs are only read; outputs
go wherever the caller says (the work folder, never the photo archive).
"""

import math
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from flickr_uploader import config
from flickr_uploader.hashing import CancelFlag

AUDIO_BITRATE = 128_000
# Below this many bits per pixel and frame, 1080p and up looks bad: go to 720p.
MIN_BITS_PER_PIXEL = 0.05
SMALLER_HEIGHT = 720
POLL_SECONDS = 0.5

_DURATION = re.compile(r"Duration: (\d+):(\d\d):(\d\d(?:\.\d+)?)")
_VIDEO = re.compile(r"Stream #\S+.*?: Video: .*?(\d{2,5})x(\d{2,5})")
_FPS = re.compile(r"(\d+(?:\.\d+)?) fps")
_AUDIO = re.compile(r"Stream #\S+.*?: Audio: ")


class VideoToolError(Exception):
    """ffmpeg is missing or could not do the job; the message says why."""


class VideoCancelledError(VideoToolError):
    """The run was paused while ffmpeg worked; the half-done output is gone."""


@dataclass(frozen=True)
class VideoInfo:
    duration: float  # seconds
    width: int | None
    height: int | None
    fps: float | None
    has_audio: bool


def ffmpeg_exe() -> str:
    try:
        import imageio_ffmpeg
    except ImportError:
        raise VideoToolError("ffmpeg is not installed (package imageio-ffmpeg)") from None
    try:
        return imageio_ffmpeg.get_ffmpeg_exe()
    except RuntimeError as error:
        raise VideoToolError(f"ffmpeg not found: {error}") from None


def probe(path: str | os.PathLike) -> VideoInfo:
    """Length and picture of a video; VideoToolError if ffmpeg can't read it."""
    result = subprocess.run(
        [ffmpeg_exe(), "-hide_banner", "-nostdin", "-i", config.filesystem_path(path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=_NO_WINDOW,
        check=False,  # exits with 1 without an output file; the header is on stderr
        timeout=120,
    )
    text = result.stderr
    duration = _DURATION.search(text)
    if duration is None:
        raise VideoToolError(f"not readable as video ({_last_line(text)})")
    hours, minutes, seconds = duration.groups()
    picture = _VIDEO.search(text)
    fps = _FPS.search(text)
    return VideoInfo(
        duration=int(hours) * 3600 + int(minutes) * 60 + float(seconds),
        width=int(picture.group(1)) if picture else None,
        height=int(picture.group(2)) if picture else None,
        fps=float(fps.group(1)) if fps else None,
        has_audio=bool(_AUDIO.search(text)),
    )


def split_count(size_bytes: int, duration: float, max_part_bytes: int, max_seconds: float) -> int:
    """How many equal pieces keep each under both limits (at least 1)."""
    by_size = math.ceil(size_bytes / max_part_bytes)
    by_length = math.ceil(duration / max_seconds) if duration > 0 else 1
    return max(1, by_size, by_length)


def compress(
    source: str | os.PathLike,
    target: str | os.PathLike,
    info: VideoInfo,
    target_bytes: int,
    *,
    cancel: CancelFlag | None = None,
) -> None:
    """Re-encode into an MP4 of about `target_bytes` (H.264 + AAC)."""
    if info.duration <= 0:
        raise VideoToolError("the video has no length")
    audio = AUDIO_BITRATE if info.has_audio else 0
    video = int(target_bytes * 8 / info.duration) - audio
    if video < 200_000:
        raise VideoToolError("too long to fit in one upload with a usable picture; split it")
    arguments = [
        "-map", "0:v:0", "-map", "0:a:0?", "-map_metadata", "0",
        "-c:v", "libx264", "-preset", "fast", "-pix_fmt", "yuv420p",
        "-b:v", str(video), "-maxrate", str(int(video * 1.5)), "-bufsize", str(video * 2),
        "-c:a", "aac", "-b:a", str(AUDIO_BITRATE),
        "-movflags", "+faststart",
    ]  # fmt: skip
    if _needs_smaller_picture(info, video):
        arguments += ["-vf", f"scale=-2:{SMALLER_HEIGHT}"]
    _run(["-i", config.filesystem_path(source), *arguments, "-f", "mp4"], target, cancel)


def cut(
    source: str | os.PathLike,
    target: str | os.PathLike,
    start: float,
    length: float | None,
    *,
    cancel: CancelFlag | None = None,
) -> None:
    """Copy `length` seconds from `start` without re-encoding (None: to the end)."""
    arguments = ["-ss", f"{start:.3f}", "-i", config.filesystem_path(source)]
    if length is not None:
        arguments += ["-t", f"{length:.3f}"]
    arguments += [
        "-map", "0:v?", "-map", "0:a?", "-map_metadata", "0",
        "-c", "copy", "-avoid_negative_ts", "make_zero",
    ]  # fmt: skip
    _run(arguments, target, cancel)


def _needs_smaller_picture(info: VideoInfo, video_bitrate: int) -> bool:
    if not info.width or not info.height or info.height <= SMALLER_HEIGHT:
        return False
    bits_per_pixel = video_bitrate / (info.width * info.height * (info.fps or 30))
    return bits_per_pixel < MIN_BITS_PER_PIXEL


def _run(arguments: list[str], target: str | os.PathLike, cancel: CancelFlag | None) -> None:
    """Run ffmpeg into `target` (via a .part file); stop it when `cancel` is set."""
    target = Path(target)
    # The real extension stays last: ffmpeg picks the container from it.
    partial = target.with_name(f"{target.stem}.part{target.suffix}")
    location = config.filesystem_path(partial)
    command = [ffmpeg_exe(), "-hide_banner", "-nostdin", "-y", "-loglevel", "error"]
    process = subprocess.Popen(  # noqa: S603 - fixed program, arguments built here
        [*command, *arguments, location],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        creationflags=_NO_WINDOW,
    )
    errors: list[bytes] = []
    try:
        while process.poll() is None:
            if cancel is not None and cancel.is_set():
                process.kill()
                process.wait()
                raise VideoCancelledError("stopped by the user")
            time.sleep(POLL_SECONDS)
        errors.append(process.stderr.read() if process.stderr else b"")
        if process.returncode != 0:
            message = errors[0].decode("utf-8", "replace")
            raise VideoToolError(f"ffmpeg failed ({_last_line(message)})")
        os.replace(location, config.filesystem_path(target))
    finally:
        if process.stderr:
            process.stderr.close()
        try:
            os.remove(location)
        except FileNotFoundError:
            pass


def _last_line(text: str) -> str:
    lines = [line.strip() for line in text.strip().splitlines() if line.strip()]
    return lines[-1] if lines else "no message"


_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
