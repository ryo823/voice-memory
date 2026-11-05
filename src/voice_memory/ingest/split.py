"""Split one long recording into conversation chunks at pauses in speech.

Pauses come from the VAD speech spans (TimedTranscript.speech), not from Whisper segment gaps: Whisper
segments run back to back and swallow silence. Rules, in order:
  1. cut wherever two speech spans are at least `gap` seconds apart
  2. a chunk longer than `max_chunk_sec` is cut again, at the largest pause of at least `max_split_gap`
     seconds inside the first `max_chunk_sec`, else at the span boundary nearest to `max_chunk_sec`
  3. each Whisper segment goes to the chunk holding most of its speech
  4. a chunk shorter than `min_chunk_sec` OR with fewer than `min_chunk_chars` characters is merged into the
     previous chunk (into the next one when it is the first)
Every segment's text lands in exactly one chunk, so the chunk texts together equal the full transcript.
All thresholds can be overridden with VOICE_MEMORY_SPLIT_* environment variables.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from .transcribe import TimedTranscript

METHOD = "silence-gap-v1"
JOIN_EPS = 0.5  # spans closer than this are one span (a span cut at a 30-minute decode boundary)


@dataclass(frozen=True)
class SplitConfig:
    enabled: bool = True
    min_duration: float = 600.0     # files shorter than this stay one memory
    gap: float = 45.0
    min_chunk_sec: float = 30.0
    min_chunk_chars: int = 50
    max_chunk_sec: float = 900.0
    max_split_gap: float = 3.0

    @classmethod
    def from_env(cls, env=None) -> "SplitConfig":
        env = os.environ if env is None else env
        d = cls()

        def num(name: str, default, cast):
            raw = env.get(f"VOICE_MEMORY_SPLIT_{name}")
            return default if raw in (None, "") else cast(raw)

        return cls(
            enabled=env.get("VOICE_MEMORY_SPLIT", "1").strip().lower() not in ("0", "false", "off", "no"),
            min_duration=num("MIN_DURATION_SEC", d.min_duration, float),
            gap=num("GAP_SEC", d.gap, float),
            min_chunk_sec=num("MIN_CHUNK_SEC", d.min_chunk_sec, float),
            min_chunk_chars=num("MIN_CHUNK_CHARS", d.min_chunk_chars, int),
            max_chunk_sec=num("MAX_CHUNK_SEC", d.max_chunk_sec, float),
            max_split_gap=num("MAX_SPLIT_GAP_SEC", d.max_split_gap, float),
        )


@dataclass
class Chunk:
    start: float
    end: float
    text: str

    @property
    def duration(self) -> float:
        return self.end - self.start


def _merge_spans(spans: list[tuple[float, float]]) -> list[tuple[float, float]]:
    out: list[list[float]] = []
    for a, b in sorted(spans):
        if out and a - out[-1][1] <= JOIN_EPS:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return [(a, b) for a, b in out]


def _cut_long(group: list[tuple[float, float]], cfg: SplitConfig) -> list[list[tuple[float, float]]]:
    """Cut a group of spans that lasts longer than max_chunk_sec."""
    parts = []
    while len(group) > 1 and group[-1][1] - group[0][0] > cfg.max_chunk_sec:
        limit = group[0][0] + cfg.max_chunk_sec
        bounds = range(len(group) - 1)  # i: boundary between group[i] and group[i + 1]
        gaps = {i: group[i + 1][0] - group[i][1] for i in bounds}
        inside = [i for i in bounds if group[i][1] <= limit and gaps[i] >= cfg.max_split_gap]
        if inside:
            cut = max(inside, key=lambda i: (gaps[i], i))
        else:
            cut = min(bounds, key=lambda i: (abs(group[i][1] - limit), i))
        parts.append(group[: cut + 1])
        group = group[cut + 1:]
    parts.append(group)
    return parts


def _overlap(a0: float, a1: float, b0: float, b1: float) -> float:
    return max(0.0, min(a1, b1) - max(a0, b0))


def split_transcript(timed: TimedTranscript, cfg: SplitConfig) -> list[Chunk]:
    """Return the chunks in time order; a single chunk means "do not split"."""
    segs = [s for s in timed.segments if s[2].strip()]
    if not segs:
        return []
    spans = _merge_spans(list(timed.speech))
    if not spans:
        return [Chunk(segs[0][0], segs[-1][1], "\n".join(t for _, _, t in segs))]

    groups: list[list[tuple[float, float]]] = [[spans[0]]]
    for sp in spans[1:]:
        if sp[0] - groups[-1][-1][1] >= cfg.gap:
            groups.append([sp])
        else:
            groups[-1].append(sp)
    pieces = [p for g in groups for p in _cut_long(g, cfg)]

    texts: list[list[str]] = [[] for _ in pieces]
    for s0, s1, text in segs:
        score = [sum(_overlap(s0, s1, a, b) for a, b in p) for p in pieces]
        if max(score) > 0:
            i = score.index(max(score))
        else:  # no speech under the segment: nearest chunk by distance
            mid = (s0 + s1) / 2
            i = min(range(len(pieces)), key=lambda k: min(abs(mid - pieces[k][0][0]), abs(mid - pieces[k][-1][1])))
        texts[i].append(text)
    chunks = [Chunk(p[0][0], p[-1][1], "\n".join(t)) for p, t in zip(pieces, texts)]

    def small(c: Chunk) -> bool:
        return c.duration < cfg.min_chunk_sec or len(c.text) < cfg.min_chunk_chars

    while len(chunks) > 1:
        i = next((k for k, c in enumerate(chunks) if small(c)), None)
        if i is None:
            break
        j = i - 1 if i > 0 else i + 1
        a, b = sorted((i, j))
        left, right = chunks[a], chunks[b]
        chunks[a:b + 1] = [Chunk(left.start, max(left.end, right.end),
                                 "\n".join(t for t in (left.text, right.text) if t))]
    return chunks
