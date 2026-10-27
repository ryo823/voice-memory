"""Mail body for the daily Muse proposal mail, built from the DB without any model.

Everything here is a deterministic read of stored facts: the record's own timestamp, its summary, date-like
phrases found in the transcript (shown as quoted text, never resolved to a date), and past records that share
a tag. Interpretation (what is a TODO, which date is meant) is left to Muse.
"""
from __future__ import annotations

import re
from contextlib import closing
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from . import database as db

JST = ZoneInfo("Asia/Tokyo")
WEEKDAYS = "月火水木金土日"

MAX_BODY_CHARS = 60_000
SUMMARY_CHARS = 600
FALLBACK_EXCERPT_CHARS = 150
MAX_MENTION_ENTRIES = 5
MENTION_CONTEXT = 20
MAX_RELATED = 3
RELATED_DAYS = 365
GENERIC_TAG_USES = 15      # a tag used by more memories than this says nothing about relatedness
SPECIFIC_TAG_USES = 3      # one shared tag counts as related only if it is this rare

HEADER = (
    "以下の音声メモの要約から、Googleカレンダーに入れる予定とTODOの候補を表で出してください。"
    "種別・内容・日時/期限・元メモID。\n"
    "- 「録音日時」はメモを記録した時刻、「言及された日時表現」は会話の中で語られた日時の原文です。別物として扱ってください。\n"
    "- 日時表現は文字列の機械検出で、日付には変換していません。相対表現は各メモの録音日(JST)を基準に解決し、"
    "曖昧なもの・検出されなかったものは推測せず確認してください。\n"
    "- 所要時間が不明なら1時間、時刻が不明なら終日で『時刻未確定』と書いてください。\n"
    "- 「採用済みのTODO」は既に登録済みです。新しい候補と区別し、重複して提案しないでください。\n"
    "- 関連する過去の記録はタグが共通するものを機械的に選んだだけで、内容の一致は保証しません。"
)

# Alternatives are tried in order at each position, so longer phrases win over their parts.
_PATTERNS = [
    ("explicit", r"(?:\d{4}年)?\d{1,2}月\d{1,2}日|\d{1,2}月(?:上旬|中旬|下旬|末|初)|"
                 r"(?<![\d、,])\d{1,2}日(?=に|まで|から|の)"),
    ("relative", r"(?:今週|来週|再来週)の?[月火水木金土日]曜日?|[月火水木金土日]曜日|"
                 r"今日|明日|明後日|あさって|昨日|今朝|今晩|今夜|明朝|今週末|週末|今週|来週|再来週|"
                 r"今月|来月|再来月|月末|月初|年末|年明け|今年|来年"),
    ("time", r"(?:午前|午後)?\d{1,2}時(?:\d{1,2}分|半)?"),
    ("vague", r"そのうち|今度|近いうち|いつか|落ち着いたら|早めに|なるべく早く|できるだけ早く|"
              r"(?:今週|今月|来月)中"),
]
_MENTION_RE = re.compile("|".join(f"(?P<{n}>{p})" for n, p in _PATTERNS))
_LABELS = {
    "explicit": "明示日付",
    "relative": "相対表現(録音日基準で要解決)",
    "time": "時刻",
    "vague": "曖昧(期限として確定しない)",
}


def format_recorded(recorded_at: str) -> str:
    """'2026-10-25 14:03 (日) JST'. Naive timestamps are read as UTC, like the rest of the DB code."""
    try:
        dt = datetime.fromisoformat(recorded_at)
    except ValueError:
        return recorded_at
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(JST)
    return f"{dt:%Y-%m-%d %H:%M} ({WEEKDAYS[dt.weekday()]}) JST"


def _one_line(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def find_mentions(transcript: str) -> list[dict]:
    """Date-like phrases in the transcript with a short quote. Nearby phrases share one entry. Nothing is resolved."""
    text = _one_line(transcript)
    entries: list[dict] = []
    for m in _MENTION_RE.finditer(text):
        kind = m.lastgroup
        if entries and m.start() <= entries[-1]["ctx_end"]:
            e = entries[-1]
            e["found"].append((kind, m.group()))
            e["ctx_end"] = min(len(text), m.end() + MENTION_CONTEXT)
            continue
        if len(entries) == MAX_MENTION_ENTRIES:
            break
        entries.append({"ctx_start": max(0, m.start() - MENTION_CONTEXT),
                        "ctx_end": min(len(text), m.end() + MENTION_CONTEXT),
                        "found": [(kind, m.group())]})
    out = []
    for e in entries:
        s, t = e["ctx_start"], e["ctx_end"]
        quote = ("…" if s else "") + text[s:t] + ("…" if t < len(text) else "")
        out.append({"quote": quote, "found": e["found"]})
    return out


def _explicit_note(kind: str, phrase: str) -> str:
    if kind == "explicit" and "月" in phrase and "日" not in phrase:
        return "月のみ・日は未確定"
    if kind == "explicit" and "月" in phrase and "年" not in phrase:
        return "明示日付・年の記載なし"
    if kind == "explicit" and "月" not in phrase:
        return "日のみ・月の記載なし"
    return _LABELS[kind]


def related_memories(conn, memory_id: str, recorded_at: str) -> list[dict]:
    """Past memories that share tags with this one, strongest first.

    A tag used by many memories is ignored. A single shared tag is accepted only when that tag is rare;
    otherwise two shared tags are needed. Nothing is listed when no memory meets that bar.
    """
    rows = conn.execute(
        "SELECT o.id, o.recorded_at, o.title, t.name AS tag, "
        "       (SELECT COUNT(*) FROM memory_tags x WHERE x.tag_id = t.id) AS uses "
        "FROM memory_tags me "
        "JOIN memory_tags mo ON mo.tag_id = me.tag_id AND mo.memory_id != me.memory_id "
        "JOIN memories o ON o.id = mo.memory_id "
        "JOIN tags t ON t.id = me.tag_id "
        "WHERE me.memory_id = ? AND julianday(o.recorded_at) < julianday(?) "
        "  AND julianday(o.recorded_at) >= julianday(?) - ?",
        (memory_id, recorded_at, recorded_at, RELATED_DAYS),
    ).fetchall()
    found: dict[str, dict] = {}
    for r in rows:
        if r["uses"] > GENERIC_TAG_USES:
            continue
        e = found.setdefault(r["id"], {"id": r["id"], "recorded_at": r["recorded_at"],
                                       "title": r["title"], "tags": [], "rare": False})
        e["tags"].append(r["tag"])
        e["rare"] = e["rare"] or r["uses"] <= SPECIFIC_TAG_USES
    ok = [e for e in found.values() if len(e["tags"]) >= 2 or e["rare"]]
    ok.sort(key=lambda e: (len(e["tags"]), e["recorded_at"]), reverse=True)
    return ok[:MAX_RELATED]


def memo_block(conn, memo: dict) -> str:
    """One memo as a labeled text block. `memo` is a full get_memory() record."""
    lines = [f"■ メモID: {memo['id']}",
             f"  録音日時: {format_recorded(memo['recorded_at'])}",
             f"  タイトル: {_one_line(memo['title'])}"]
    summary = _one_line(memo["summary"])
    if summary:
        lines.append(f"  要約: {summary[:SUMMARY_CHARS]}")
    else:
        excerpt = _one_line(memo["transcript"])[:FALLBACK_EXCERPT_CHARS]
        lines.append(f"  要約: (なし) 冒頭の抜粋: {excerpt}")
    if memo["tags"]:
        lines.append(f"  タグ: {', '.join(memo['tags'])}")
    mentions = find_mentions(memo["transcript"])
    if mentions:
        lines.append("  言及された日時表現(原文より機械検出。日付には変換していません):")
        for e in mentions:
            found = " / ".join(f"{_explicit_note(k, p)}「{p}」" for k, p in e["found"])
            lines.append(f"    - 「{e['quote']}」 → {found}")
    else:
        lines.append("  言及された日時表現: 検出なし(機械検出のため漏れの可能性あり。推測しないこと)")
    related = related_memories(conn, memo["id"], memo["recorded_at"])
    if related:
        lines.append("  関連する過去の記録(共通タグによる機械選択):")
        for e in related:
            lines.append(f"    - {e['id']} ({format_recorded(e['recorded_at'])[:10]}) "
                         f"{_one_line(e['title'])} [共通タグ: {', '.join(e['tags'])}]")
    return "\n".join(lines)


def build(db_path=None, limit: int = db.MAX_LIMIT) -> tuple[str, list[str]]:
    """Return (mail body, ids of the memos in it) for memos not yet mailed. ('', []) when there are none.

    Memos that would push the body past MAX_BODY_CHARS are left for the next run (not in the returned ids).
    """
    pending = db.pending_memories(limit=limit, db_path=db_path, exclude_suggested=True)
    parts, ids = [HEADER], []
    size = len(HEADER)
    with closing(db.connect(db_path)) as conn:
        for p in pending:
            block = memo_block(conn, db.get_memory(p["id"], db_path))
            if ids and size + len(block) > MAX_BODY_CHARS:
                break
            parts.append(block)
            ids.append(p["id"])
            size += len(block)
    if not ids:
        return "", []
    return "\n\n".join(parts), ids
