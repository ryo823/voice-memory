"""Read-only mobile web viewer for voice memories (bind to a Tailscale IP, never 0.0.0.0)."""
from __future__ import annotations

import argparse
import html
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlparse

from . import database as db

DEFAULT_PORT = 8765
PAGE_LIMIT = db.MAX_LIMIT

CSS = """
:root{color-scheme:light dark;--bg:#fafaf8;--fg:#1d1d1b;--mut:#6b6b66;--card:#fff;--line:#e4e4de;--acc:#2d6a4f}
@media(prefers-color-scheme:dark){:root{--bg:#161614;--fg:#ececE6;--mut:#9a9a92;--card:#1f1f1c;--line:#33332f;--acc:#74c69d}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.6 system-ui,sans-serif}
main{max-width:720px;margin:0 auto;padding:16px}h1{font-size:1.2rem;margin:0 0 12px}h1 a{color:inherit;text-decoration:none}
form{display:flex;gap:8px;margin-bottom:12px}input[type=search]{flex:1;font:inherit;padding:10px;border:1px solid var(--line);border-radius:8px;background:var(--card);color:var(--fg)}
button{font:inherit;padding:10px 14px;border:0;border-radius:8px;background:var(--acc);color:var(--bg)}
.tags{display:flex;flex-wrap:wrap;gap:6px;margin:0 0 14px}.tag{padding:3px 10px;border:1px solid var(--line);border-radius:99px;font-size:.85rem;color:var(--fg);text-decoration:none;background:var(--card)}
.tag.on{background:var(--acc);color:var(--bg);border-color:var(--acc)}.tag small{color:var(--mut)}
.card{display:block;background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 14px;margin-bottom:10px;color:inherit;text-decoration:none}
.card h2{font-size:1rem;margin:0 0 2px}.meta{color:var(--mut);font-size:.8rem}.card p{margin:6px 0 0;font-size:.92rem}
h3{font-size:.9rem;color:var(--mut);margin:20px 0 6px}.body{white-space:pre-wrap;word-break:break-word}.empty{color:var(--mut)}
"""


def _e(s) -> str:
    return html.escape(str(s or ""), quote=True)


def _page(title: str, body: str) -> bytes:
    return (
        f'<!doctype html><html lang="ja"><head><meta charset="utf-8">'
        f'<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{_e(title)}</title><style>{CSS}</style></head><body><main>"
        f'<h1><a href="/">Voice Memory</a></h1>{body}</main></body></html>'
    ).encode()


def _tag_chips(tags: list[dict], active: str = "") -> str:
    chips = "".join(
        f'<a class="tag{" on" if t["name"] == active else ""}" href="/?tag={quote(t["name"])}">'
        f'{_e(t["name"])} <small>{t["count"]}</small></a>'
        for t in tags
    )
    return f'<div class="tags">{chips}</div>' if chips else ""


def _card(m: dict) -> str:
    tags = " ".join(f"#{_e(t)}" for t in m["tags"])
    text = m["summary"] or m["excerpt"]
    return (
        f'<a class="card" href="/m/{quote(m["id"])}"><h2>{_e(m["title"])}</h2>'
        f'<div class="meta">{_e(m["recorded_at"])}{" · " + tags if tags else ""}</div>'
        f"<p>{_e(text)}</p></a>"
    )


def _href(q: str, tag: str, offset: int) -> str:
    params = [f"{k}={quote(v)}" for k, v in (("q", q), ("tag", tag)) if v]
    if offset:
        params.append(f"offset={offset}")
    return "/?" + "&amp;".join(params) if params else "/"


def render_index(query: str = "", tag: str = "", offset: int = 0, db_path=None) -> bytes:
    # fetch one extra row to know whether a next page exists
    n = PAGE_LIMIT + 1
    if query:
        items = db.search_memories(query, n, db_path=db_path, offset=offset)
    elif tag:
        items = db.memories_by_tag(tag, n, db_path=db_path, offset=offset)
    else:
        items = db.recent_memories(n, db_path=db_path, offset=offset)
    has_next = len(items) > PAGE_LIMIT
    items = items[:PAGE_LIMIT]
    form = (
        f'<form action="/"><input type="search" name="q" value="{_e(query)}" placeholder="検索">'
        f"<button>検索</button></form>"
    )
    cards = "".join(_card(m) for m in items) or '<p class="empty">該当するメモはありません。</p>'
    links = []
    if offset:
        links.append(f'<a class="tag" href="{_href(query, tag, max(0, offset - PAGE_LIMIT))}">← 新しい方へ</a>')
    if has_next:
        links.append(f'<a class="tag" href="{_href(query, tag, offset + PAGE_LIMIT)}">古い方へ →</a>')
    pager = f'<div class="tags">{"".join(links)}</div>' if links else ""
    return _page("Voice Memory", form + _tag_chips(db.list_tags(db_path), tag) + cards + pager)


def render_detail(memory_id: str, db_path=None) -> bytes:
    m = db.get_memory(memory_id, db_path=db_path)
    tags = " ".join(f'<a class="tag" href="/?tag={quote(t)}">{_e(t)}</a>' for t in m["tags"])
    body = (
        f'<h2>{_e(m["title"])}</h2><div class="meta">{_e(m["recorded_at"])} · {_e(m["source"])}</div>'
        f'<div class="tags" style="margin-top:8px">{tags}</div>'
        f'<h3>要約</h3><div class="body">{_e(m["summary"]) or "（なし）"}</div>'
        f'<h3>文字起こし</h3><div class="body">{_e(m["transcript"])}</div>'
    )
    return _page(m["title"], body)


def make_handler(db_path=None):
    class Handler(BaseHTTPRequestHandler):
        def _send(self, status: int, payload: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):  # noqa: N802
            url = urlparse(self.path)
            qs = parse_qs(url.query)
            try:
                if url.path == "/":
                    raw = qs.get("offset", ["0"])[0]
                    offset = int(raw) if raw.isdigit() else 0
                    self._send(200, render_index(
                        qs.get("q", [""])[0].strip(), qs.get("tag", [""])[0].strip(), offset, db_path))
                elif url.path.startswith("/m/"):
                    self._send(200, render_detail(url.path[3:], db_path))
                else:
                    self._send(404, _page("Not found", '<p class="empty">ページがありません。</p>'))
            except db.NotFoundError:
                self._send(404, _page("Not found", '<p class="empty">メモが見つかりません。</p>'))
            except db.ValidationError:
                self._send(400, _page("Bad request", '<p class="empty">入力が不正です。</p>'))

        def log_message(self, fmt, *args):  # keep request paths (search terms) out of the journal
            pass

    return Handler


def main() -> None:
    ap = argparse.ArgumentParser(description="Read-only mobile viewer for voice memories.")
    ap.add_argument("--host", default=os.environ.get("VOICE_MEMORY_WEB_HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("VOICE_MEMORY_WEB_PORT", DEFAULT_PORT)))
    args = ap.parse_args()
    if args.host in ("0.0.0.0", "::"):
        ap.error("refusing to bind all interfaces; use 127.0.0.1 or a Tailscale IP")
    db.init_db()
    ThreadingHTTPServer((args.host, args.port), make_handler()).serve_forever()


if __name__ == "__main__":
    main()
