"""Settings from environment variables, optionally seeded from <project root>/.env.

Secrets are never logged. The .env file should be chmod 600.
"""
from __future__ import annotations

import logging
import os
import stat
from pathlib import Path

from ..database import PROJECT_ROOT

log = logging.getLogger("voice_memory.ingest")

DEFAULT_WHISPER_MODEL = "small"
DEFAULT_CLAUDE_MODEL = "claude-haiku-5-5"


def load_dotenv(path: Path | None = None) -> None:
    """Load KEY=VALUE lines into os.environ without overriding existing variables."""
    path = path or PROJECT_ROOT / ".env"
    if not path.is_file():
        return
    if path.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        log.warning("%s is accessible by other users; run: chmod 600 %s", path, path)
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip().removeprefix("export ").strip()
        value = value.strip().strip("'\"")
        if key and key not in os.environ:
            os.environ[key] = value


def recordings_dir() -> Path:
    env = os.environ.get("VOICE_MEMORY_RECORDINGS")
    if env:
        return Path(env).expanduser().resolve()
    return PROJECT_ROOT / "data" / "recordings"
