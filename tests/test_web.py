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
