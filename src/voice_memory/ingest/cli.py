"""voice-memory-ingest FILE: transcribe, summarize and store one local audio file."""
from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import os
import sys
import time
from pathlib import Path

from ..database import PROJECT_ROOT
from . import config
from .audio import InvalidAudioError
from .pipeline import IngestError, process_file
from .summarize import ClaudeSummarizer
from .transcribe import WhisperTranscriber


def _setup_logging() -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    try:
        log_dir = PROJECT_ROOT / "logs"
        log_dir.mkdir(exist_ok=True)
        handlers.append(logging.handlers.RotatingFileHandler(
            log_dir / "ingest.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8"))
    except OSError:
        pass
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", handlers=handlers)
    for noisy in ("httpx", "httpcore", "huggingface_hub"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="voice-memory-ingest", description=__doc__)
    ap.add_argument("file", type=Path)
    ap.add_argument("--no-summary", action="store_true",
                    help="skip Claude: store transcript only (no ANTHROPIC_API_KEY needed)")
    ap.add_argument("--whisper-model", help="faster-whisper model (default: env VOICE_MEMORY_WHISPER_MODEL or small)")
    ap.add_argument("--claude-model", help="default: env VOICE_MEMORY_CLAUDE_MODEL or " + config.DEFAULT_CLAUDE_MODEL)
    ap.add_argument("--beam-size", type=int, default=1)
    args = ap.parse_args(argv)

    _setup_logging()
    config.load_dotenv()
    whisper = args.whisper_model or os.environ.get("VOICE_MEMORY_WHISPER_MODEL", config.DEFAULT_WHISPER_MODEL)
    claude = args.claude_model or os.environ.get("VOICE_MEMORY_CLAUDE_MODEL", config.DEFAULT_CLAUDE_MODEL)
    summarizer = None if args.no_summary else ClaudeSummarizer(claude)
    t0 = time.monotonic()
    try:
        result = process_file(args.file, WhisperTranscriber(whisper, beam_size=args.beam_size), summarizer)
    except (InvalidAudioError, IngestError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    result["elapsed_sec"] = round(time.monotonic() - t0, 1)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
