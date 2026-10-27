from contextlib import closing

from voice_memory import database as db, digest
from test_database import _days_ago, save


def test_format_recorded_jst():
    assert digest.format_recorded("2026-10-25T00:30:00+00:00") == "2026-10-25 09:30 (日) JST"
    assert digest.format_recorded("2026-10-25T09:30:00+09:00") == "2026-10-25 09:30 (日) JST"
    assert digest.format_recorded("garbage") == "garbage"


def test_mentions_quote_and_classify_without_resolving():
    ms = digest.find_mentions("来週の火曜の15時に歯医者。それとそのうち10月30日に連絡する。3日間かかる。")
    kinds = [k for m in ms for k, _ in m["found"]]
    phrases = [p for m in ms for _, p in m["found"]]
    assert kinds[:3] == ["relative", "time", "vague"] and "来週の火曜" in phrases and "15時" in phrases
    assert "10月30日" in phrases and "3日間" not in " ".join(phrases)
    assert all(len(m["quote"]) < 60 for m in ms)


def test_mentions_month_part_and_durations():
    phrases = lambda t: [p for m in digest.find_mentions(t) for _, p in m["found"]]
    assert phrases("4月末までに提出") == ["4月末"]
    assert phrases("出血は2、3日続く。最初1日は安静") == []
    assert phrases("30日に会議") == ["30日"]


def test_mentions_none_and_capped():
    assert digest.find_mentions("特に予定の話はない") == []
    assert len(digest.find_mentions(("明日。" + "あ" * 100) * 20)) == digest.MAX_MENTION_ENTRIES


def test_block_flags_year_less_date_and_missing_mentions(db_path):
    a = save(db_path, transcript="10月30日に会議", summary="会議の件", recorded_at=_days_ago(1))
    b = save(db_path, transcript="雑談", summary="", recorded_at=_days_ago(2))
    body, ids = digest.build(db_path)
    assert set(ids) == {a["id"], b["id"]}
    assert "明示日付・年の記載なし「10月30日」" in body
    assert "検出なし" in body
    assert "要約: (なし) 冒頭の抜粋: 雑談" in body


def test_full_transcript_not_in_body(db_path):
    long = "明日の会議。" + "秘密の本文" * 200
    save(db_path, transcript=long, summary="要約あり", recorded_at=_days_ago(1))
    body, _ = digest.build(db_path)
    assert "秘密の本文" * 20 not in body


def test_related_needs_rare_or_two_shared_tags(db_path):
    old = save(db_path, title="過去", tags=["案件X", "営業"], recorded_at=_days_ago(30))
    for i in range(digest.SPECIFIC_TAG_USES):  # "営業" is common enough that sharing it alone means little
        save(db_path, title="無関係", tags=["営業"], recorded_at=_days_ago(31 + i))
    new = save(db_path, title="現在", tags=["案件X", "営業"], recorded_at=_days_ago(1))
    with closing(db.connect(db_path)) as conn:
        rel = digest.related_memories(conn, new["id"], db.get_memory(new["id"], db_path)["recorded_at"])
    assert [r["id"] for r in rel] == [old["id"]]


def test_generic_tag_and_future_records_ignored(db_path):
    for i in range(digest.GENERIC_TAG_USES + 1):
        save(db_path, tags=["雑多"], recorded_at=_days_ago(10 + i))
    new = save(db_path, tags=["雑多"], recorded_at=_days_ago(1))
    later = save(db_path, tags=["雑多"], recorded_at=_days_ago(0))
    with closing(db.connect(db_path)) as conn:
        for m in (new, later):
            assert digest.related_memories(conn, m["id"], db.get_memory(m["id"], db_path)["recorded_at"]) == []


def test_rare_single_tag_is_related(db_path):
    old = save(db_path, title="過去", tags=["希少"], recorded_at=_days_ago(20))
    new = save(db_path, tags=["希少"], recorded_at=_days_ago(1))
    body, _ = digest.build(db_path)
    assert old["id"] in body and "共通タグ: 希少" in body


def test_build_skips_mailed_and_caps_size(db_path, monkeypatch):
    a = save(db_path, recorded_at=_days_ago(1))
    db.mark_suggested([a["id"]], db_path)
    assert digest.build(db_path) == ("", [])
    for _ in range(3):
        save(db_path, recorded_at=_days_ago(1))
    monkeypatch.setattr(digest, "MAX_BODY_CHARS", len(digest.HEADER) + 400)
    body, ids = digest.build(db_path)
    assert 1 <= len(ids) < 3


def test_month_only_is_not_a_date(db_path):
    save(db_path, transcript="4月末までに提出", summary="提出", recorded_at=_days_ago(1))
    body, _ = digest.build(db_path)
    assert "月のみ・日は未確定「4月末」" in body
