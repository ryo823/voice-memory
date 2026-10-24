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
            segments, _info = model.transcribe(
                str(path), language=self.language, beam_size=self.beam_size, vad_filter=True
            )
            return "\n".join(s.text.strip() for s in segments if s.text.strip())
        except Exception as e:  # model download/decoding failures vary widely
            raise TranscribeError(f"transcription failed: {type(e).__name__}: {e}") from e
