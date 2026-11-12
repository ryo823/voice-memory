import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from voice_memory import database as db
from voice_memory import web


@pytest.fixture
def base(db_path):
    db.save_memory("認証方式", "Firebaseで認証する<script>", summary="要約です", tags=["firebase", "仕事"], db_path=db_path)
    db.save_memory("買い物", "牛乳を買う", summary="", tags=["生活"], db_path=db_path)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), web.make_handler(db_path))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def get(url):
    try:
        with urllib.request.urlopen(url) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def test_index_lists_memories_and_tags(base):
    status, body = get(base + "/")
    assert status == 200 and "認証方式" in body and "買い物" in body and "firebase" in body


def test_tag_filter_and_search(base):
    _, body = get(base + "/?tag=" + urllib.request.quote("生活"))
    assert "買い物" in body and "<h2>認証方式" not in body
    _, body = get(base + "/?q=" + urllib.request.quote("Firebase"))
    assert "認証方式" in body


def test_detail_escapes_html(base):
    _, body = get(base + "/?q=" + urllib.request.quote("Firebase"))
    href = body.split('href="/m/')[1].split('"')[0]
    status, detail = get(base + "/m/" + href)
    assert status == 200 and "<script>" not in detail and "&lt;script&gt;" in detail


def test_missing_and_unknown(base):
    assert get(base + "/m/nope")[0] == 404
    assert get(base + "/zzz")[0] == 404


def test_pagination(db_path, monkeypatch):
    monkeypatch.setattr(web, "PAGE_LIMIT", 2)
    for i in range(5):
        db.save_memory(f"m{i}", "t", recorded_at=f"2026-01-0{i + 1}T00:00:00+09:00", tags=["x"], db_path=db_path)
    page = lambda off: web.render_index(offset=off, db_path=db_path).decode()
    p0, p1, p2 = page(0), page(2), page(4)
    assert "m4" in p0 and "m3" in p0 and "m2" not in p0 and "古い方へ" in p0 and "新しい方へ" not in p0
    assert "m2" in p1 and "m1" in p1 and "古い方へ" in p1 and "新しい方へ" in p1
    assert "m0" in p2 and "古い方へ" not in p2 and "新しい方へ" in p2
    assert "offset=2" in web.render_index(tag="x", db_path=db_path).decode()


def post(url, origin=None):
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None
    req = urllib.request.Request(url, data=b"", method="POST")
    if origin:
        req.add_header("Origin", origin)
    try:
        with urllib.request.build_opener(NoRedirect).open(req) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


def test_delete_flow(db_path, tmp_path):
    rec = tmp_path / "rec"
    (rec / "2026").mkdir(parents=True)
    (rec / "2026" / "a.m4a").write_bytes(b"x")
    keep = db.save_memory("残す", "keep", tags=["k"], db_path=db_path)
    gone = db.save_memory("消す", "gone", tags=["g"], audio_path="2026/a.m4a", db_path=db_path)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), web.make_handler(db_path, recordings=rec))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        status, body = get(f"{base}/m/{gone['id']}/delete")
        assert status == 200 and "削除する" in body
        assert post(f"{base}/m/{gone['id']}/delete") == 403  # no Origin
        assert post(f"{base}/m/{gone['id']}/delete", "http://evil.example") == 403
        assert (rec / "2026" / "a.m4a").exists()
        assert post(f"{base}/m/{gone['id']}/delete", base) == 303
    finally:
        srv.shutdown()
    assert not (rec / "2026" / "a.m4a").exists()
    assert [m["title"] for m in db.recent_memories(db_path=db_path)] == ["残す"]
    assert [t["name"] for t in db.list_tags(db_path)] == ["k"]
    assert db.search_memories("gone", db_path=db_path) == []
    with pytest.raises(db.NotFoundError):
        db.delete_memory(gone["id"], recordings=rec, db_path=db_path)
    assert keep["id"]


def test_delete_keeps_shared_audio_and_blocks_traversal(db_path, tmp_path):
    rec = tmp_path / "rec"
    rec.mkdir()
    (rec / "s.m4a").write_bytes(b"x")
    outside = tmp_path / "outside.txt"
    outside.write_text("keep me")
    a = db.save_memory("a", "t", audio_path="s.m4a", db_path=db_path)
    db.save_memory("b", "t", audio_path="s.m4a", db_path=db_path)
    assert db.delete_memory(a["id"], recordings=rec, db_path=db_path)["audio_deleted"] is False
    assert (rec / "s.m4a").exists()
    import sqlite3
    c = sqlite3.connect(db_path)
    c.execute("UPDATE memories SET audio_path = '../outside.txt' WHERE title = 'b'")
    c.commit(); c.close()
    b = db.recent_memories(db_path=db_path)[0]
    assert db.delete_memory(b["id"], recordings=rec, db_path=db_path)["audio_deleted"] is False
    assert outside.exists()


def test_deleted_file_is_not_reingested(db_path):
    import sqlite3
    from voice_memory.ingest import pipeline
    m = db.save_memory("x", "t", db_path=db_path)
    c = sqlite3.connect(db_path)
    c.executescript(pipeline.SCHEMA)
    c.execute("INSERT INTO ingested_files (sha256, original_name, stored_path, size_bytes, duration_sec, status, "
              "transcript, error, memory_id, created_at, updated_at) "
              "VALUES ('sha','n','p',1,1.0,'done','secret',NULL,?,'t','t')", (m["id"],))
    c.commit(); c.close()
    db.delete_memory(m["id"], recordings="/nonexistent", db_path=db_path)
    c = sqlite3.connect(db_path)
    c.row_factory = sqlite3.Row
    row = c.execute("SELECT * FROM ingested_files").fetchone()
    assert row["status"] == "deleted" and row["transcript"] is None and row["memory_id"] is None
    assert pipeline._claim(c, "sha", "n", "p", type("I", (), {"size_bytes": 1, "duration_sec": 1.0})()) is None


def test_date_filter_uses_jst_day(db_path):
    # 15:55 UTC is 00:55 JST the next day; the JST calendar day must win.
    db.save_memory("深夜の会話", "t", recorded_at="2026-11-11T15:55:43+00:00", db_path=db_path)
    db.save_memory("昼の会話", "t", recorded_at="2026-11-11T03:00:00+00:00", db_path=db_path)
    body12 = web.render_index(date="2026-11-12", db_path=db_path).decode()
    assert "深夜の会話" in body12 and "昼の会話" not in body12
    assert "2026-11-12 00:55" in body12  # JST display, not the raw UTC string
    body11 = web.render_index(date="2026-11-11", db_path=db_path).decode()
    assert "昼の会話" in body11 and "深夜の会話" not in body11


def test_date_view_is_oldest_first_and_shows_daybar(db_path):
    db.save_memory("あさの会話", "t", recorded_at="2026-11-09T00:30:00+09:00", db_path=db_path)
    db.save_memory("よるの会話", "t", recorded_at="2026-11-09T20:30:00+09:00", db_path=db_path)
    body = web.render_index(date="2026-11-09", db_path=db_path).decode()
    assert body.index("あさの会話") < body.index("よるの会話")
    assert "2026年11月9日（月） 2件" in body
    assert "date=2026-11-08" in body and "date=2026-11-10" in body  # prev / next day


def test_date_combines_with_tag_and_search(db_path):
    db.save_memory("タグ付き", "本文アルファ", recorded_at="2026-11-09T10:00:00+09:00", tags=["x"], db_path=db_path)
    db.save_memory("タグなし", "本文アルファ", recorded_at="2026-11-09T11:00:00+09:00", db_path=db_path)
    db.save_memory("別日タグ付き", "本文アルファ", recorded_at="2026-11-10T10:00:00+09:00", tags=["x"], db_path=db_path)
    by_tag = web.render_index(tag="x", date="2026-11-09", db_path=db_path).decode()
    assert "タグ付き" in by_tag and "タグなし" not in by_tag and "別日タグ付き" not in by_tag
    by_q = web.render_index(query="アルファ", date="2026-11-10", db_path=db_path).decode()
    assert "別日タグ付き" in by_q and "<h2>タグ付き" not in by_q


def test_daily_counts_exclude_archived(db_path):
    import sqlite3
    db.save_memory("a", "t", recorded_at="2026-11-11T15:55:43+00:00", db_path=db_path)
    db.save_memory("b", "t", recorded_at="2026-11-11T16:55:43+00:00", db_path=db_path)
    gone = db.save_memory("c", "t", recorded_at="2026-11-11T17:55:43+00:00", db_path=db_path)
    c = sqlite3.connect(db_path)
    c.execute("UPDATE memories SET archived = 1 WHERE id = ?", (gone["id"],))
    c.commit(); c.close()
    assert db.daily_counts("2026-11", db_path) == {"2026-11-12": 2}
    with pytest.raises(db.ValidationError):
        db.daily_counts("2026-13", db_path)
    with pytest.raises(db.ValidationError):
        db.memories_by_date("2026-02-30", db_path=db_path)


def test_calendar_api_and_index_chrome(db_path):
    db.save_memory("a", "t", recorded_at="2026-11-09T10:00:00+09:00", db_path=db_path)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), web.make_handler(db_path))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        status, body = get(base + "/api/calendar?month=2026-11")
        assert status == 200
        import json
        assert json.loads(body) == {"month": "2026-11", "counts": {"2026-11-09": 1}}
        assert get(base + "/api/calendar?month=bad")[0] == 400
        assert get(base + "/?date=2026-13-01")[0] == 400
        status, body = get(base + "/")
        assert status == 200 and 'id="cal-btn"' in body and "/api/calendar" in body and 'id="cal-overlay"' in body
        status, body = get(base + "/?date=2026-11-09")
        assert "2026年11月9日（月） 1件" in body
    finally:
        srv.shutdown()
