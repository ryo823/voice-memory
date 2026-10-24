"""Audio file validation (PyAV; no ffmpeg binary needed)."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

import av


class InvalidAudioError(ValueError):
    """The file is missing, empty, unreadable or not decodable audio."""


@dataclass(frozen=True)
class AudioInfo:
    size_bytes: int
    duration_sec: float
    sample_rate: int
    channels: int
    codec: str


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def probe(path: Path) -> AudioInfo:
    if not path.is_file():
        raise InvalidAudioError(f"not a file: {path}")
    size = path.stat().st_size
    if size == 0:
        raise InvalidAudioError(f"empty file: {path}")
    try:
        with av.open(str(path)) as container:
            if not container.streams.audio:
                raise InvalidAudioError(f"no audio stream: {path}")
            stream = container.streams.audio[0]
            if container.duration:
                duration = container.duration / av.time_base
            elif stream.duration and stream.time_base:
                duration = float(stream.duration * stream.time_base)
            else:
                duration = 0.0
            info = AudioInfo(size, duration, stream.rate or 0, stream.channels or 0, stream.codec_context.name)
    except InvalidAudioError:
        raise
    except (av.error.FFmpegError, OSError, ValueError) as e:
        raise InvalidAudioError(f"unsupported or corrupt audio ({path.name}): {e}") from e
    if info.duration_sec <= 0:
        raise InvalidAudioError(f"audio has no duration: {path}")
    return info
