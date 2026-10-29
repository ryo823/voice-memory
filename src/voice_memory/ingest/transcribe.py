"""Local speech-to-text with faster-whisper (CPU). Swappable via the Transcriber protocol."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Protocol

log = logging.getLogger("voice_memory.ingest")


class TranscribeError(RuntimeError):
    pass


class Transcriber(Protocol):
    def transcribe(self, path: Path) -> str: ...


SAMPLE_RATE = 16000
CHUNK_SECONDS = 1800  # decoding a whole multi-hour file at once exhausts the Pi's 8 GB of RAM


def iter_audio_chunks(path: Path, seconds: int = CHUNK_SECONDS):
    """Yield mono 16 kHz float32 arrays of at most `seconds`, decoding the file as a stream."""
    import av
    import numpy as np

    limit = seconds * SAMPLE_RATE
    parts: list = []
    size = 0
    with av.open(str(path)) as container:
        resampler = av.AudioResampler(format="s16", layout="mono", rate=SAMPLE_RATE)
        frames = (f for frame in container.decode(container.streams.audio[0])
                  for f in resampler.resample(frame))
        for f in frames:
            pcm = f.to_ndarray().reshape(-1)
            parts.append(pcm)
            size += pcm.size
            if size >= limit:
                yield np.concatenate(parts).astype(np.float32) / 32768.0
                parts, size = [], 0
    if size:
        yield np.concatenate(parts).astype(np.float32) / 32768.0


class WhisperTranscriber:
    def __init__(self, model: str, language: str = "ja", beam_size: int = 1, threads: int = 4):
        self.model_name, self.language, self.beam_size, self.threads = model, language, beam_size, threads
        self._model = None

    def _load(self):
        if self._model is None:
            from faster_whisper import WhisperModel  # heavy import, keep lazy

            log.info("loading whisper model %s (first run downloads it)", self.model_name)
            self._model = WhisperModel(
                self.model_name, device="cpu", compute_type="int8", cpu_threads=self.threads
            )
        return self._model

    def transcribe(self, path: Path) -> str:
        try:
            model = self._load()
            lines: list[str] = []
            for audio in iter_audio_chunks(path):
                segments, _info = model.transcribe(
                    audio, language=self.language, beam_size=self.beam_size, vad_filter=True
                )
                lines.extend(s.text.strip() for s in segments if s.text.strip())
            return "\n".join(lines)
        except Exception as e:  # model download/decoding failures vary widely
            raise TranscribeError(f"transcription failed: {type(e).__name__}: {e}") from e
