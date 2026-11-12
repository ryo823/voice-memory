"""Mobile web viewer for voice memories, with delete (bind to a Tailscale IP, never 0.0.0.0)."""
from __future__ import annotations

import argparse
import html
import json
import os
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlparse
from zoneinfo import ZoneInfo

from . import database as db

DEFAULT_PORT = 8765
PAGE_LIMIT = db.MAX_LIMIT
JST = ZoneInfo("Asia/Tokyo")
WEEKDAYS_JA = "月火水木金土日"  # index: date.weekday() (Mon=0)

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
button.danger{background:#b3261e;color:#fff}a.btn{padding:10px 14px;border-radius:8px;border:1px solid var(--line);color:var(--fg);text-decoration:none}.row{display:flex;gap:8px;align-items:center;margin-top:20px}
h3{font-size:.9rem;color:var(--mut);margin:20px 0 6px}.body{white-space:pre-wrap;word-break:break-word}.empty{color:var(--mut)}
.head{display:flex;align-items:center;gap:8px;margin:0 0 12px}.head h1{margin:0}
.calbtn{margin-left:auto;background:var(--card);color:var(--fg);border:1px solid var(--line);font-size:1.15rem;padding:7px 13px;line-height:1.3}
.daybar{display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin:0 0 14px}.daybar strong{margin:0 2px}
#cal-overlay{position:fixed;inset:0;background:rgba(0,0,0,.45);display:flex;align-items:flex-start;justify-content:center;padding:24px 12px;z-index:10}
#cal-overlay[hidden]{display:none}
.cal{background:var(--bg);border-radius:12px;padding:16px;width:min(420px,100%)}
.cal-head{display:flex;align-items:center;justify-content:space-between;margin-bottom:8px}.cal-head div{font-weight:700}
.cal-head button,.cal-foot button{background:var(--card);color:var(--fg);border:1px solid var(--line)}
.cal-week{display:grid;grid-template-columns:repeat(7,1fr);gap:4px;text-align:center;color:var(--mut);font-size:.8rem;margin-bottom:4px}
#cal-grid{display:grid;grid-template-columns:repeat(7,1fr);gap:4px}
.cal-day{background:var(--card);color:var(--fg);border:1px solid var(--line);border-radius:8px;padding:5px 0;display:flex;flex-direction:column;align-items:center;line-height:1.35;font-size:.95rem}
.cal-day small{color:var(--acc);font-size:.62rem;min-height:1em}
.cal-day.today{border-color:var(--acc);font-weight:700}
.cal-day.sel{background:var(--acc);color:var(--bg)}.cal-day.sel small{color:var(--bg)}
.cal-foot{display:flex;gap:8px;justify-content:flex-end;margin-top:12px}
"""

CAL_HTML = """
<div id="cal-overlay" hidden><div class="cal" role="dialog" aria-label="カレンダー">
<div class="cal-head"><button type="button" id="cal-prev" aria-label="前月">‹</button><div id="cal-title"></div><button type="button" id="cal-next" aria-label="翌月">›</button></div>
<div class="cal-week"><span>日</span><span>月</span><span>火</span><span>水</span><span>木</span><span>金</span><span>土</span></div>
<div id="cal-grid"></div>
<div class="cal-foot"><button type="button" id="cal-today">今日へ</button><button type="button" id="cal-close">閉じる</button></div>
</div></div>
"""

CAL_JS = """
<script>
const overlay=document.getElementById('cal-overlay'),grid=document.getElementById('cal-grid'),calTitle=document.getElementById('cal-title');
let calCur=null;
const pad=n=>String(n).padStart(2,'0');
const dayStr=d=>d.getFullYear()+'-'+pad(d.getMonth()+1)+'-'+pad(d.getDate());
function goDay(ds){const p=new URLSearchParams(location.search);p.set('date',ds);p.delete('offset');location.href='/?'+p.toString();}
async function drawCal(){
  calTitle.textContent=calCur.getFullYear()+'年'+(calCur.getMonth()+1)+'月';
  grid.innerHTML='';
  let counts={};
  try{const r=await fetch('/api/calendar?month='+calCur.getFullYear()+'-'+pad(calCur.getMonth()+1));if(r.ok)counts=(await r.json()).counts||{};}catch(e){}
  const y=calCur.getFullYear(),mo=calCur.getMonth(),start=new Date(y,mo,1).getDay(),days=new Date(y,mo+1,0).getDate();
  const todayStr=dayStr(new Date()),sel=new URLSearchParams(location.search).get('date');
  for(let i=0;i<start;i++)grid.appendChild(document.createElement('span'));
  for(let d=1;d<=days;d++){
    const ds=y+'-'+pad(mo+1)+'-'+pad(d),b=document.createElement('button');
    b.type='button';b.className='cal-day';
    if(ds===todayStr)b.classList.add('today');
    if(ds===sel)b.classList.add('sel');
    const c=counts[ds],s=document.createElement('small');
    s.textContent=c?('● '+c):'';
    b.append(d,s);
    b.addEventListener('click',()=>goDay(ds));
    grid.appendChild(b);
  }
}
document.getElementById('cal-btn').addEventListener('click',()=>{
  const sel=new URLSearchParams(location.search).get('date');
  const base=sel?new Date(sel+'T00:00:00'):new Date();
  calCur=new Date(base.getFullYear(),base.getMonth(),1);
  overlay.hidden=false;drawCal();
});
document.getElementById('cal-close').addEventListener('click',()=>{overlay.hidden=true;});
document.getElementById('cal-today').addEventListener('click',()=>goDay(dayStr(new Date())));
document.getElementById('cal-prev').addEventListener('click',()=>{calCur.setMonth(calCur.getMonth()-1);drawCal();});
document.getElementById('cal-next').addEventListener('click',()=>{calCur.setMonth(calCur.getMonth()+1);drawCal();});
overlay.addEventListener('click',e=>{if(e.target===overlay)overlay.hidden=true;});
document.addEventListener('keydown',e=>{if(e.key==='Escape')overlay.hidden=true;});
</script>
"""


def _e(s) -> str:
    return html.escape(str(s or ""), quote=True)


def _fmt_dt(recorded_at) -> str:
    """Stored ISO timestamp (any offset) -> '2026-11-12 00:55' in Asia/Tokyo."""
    try:
        dt = datetime.fromisoformat(str(recorded_at))
    except ValueError:
        return str(recorded_at or "")
    if dt.tzinfo is None:
        return str(recorded_at or "")
    return dt.astimezone(JST).strftime("%Y-%m-%d %H:%M")


def _parse_day(day: str):
    """Validate a YYYY-MM-DD day string; raises db.ValidationError like the db layer."""
    try:
        return datetime.strptime(day, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        raise db.ValidationError("date must be YYYY-MM-DD") from None


def _fmt_day(day: str) -> str:
    d = _parse_day(day)
    return f"{d.year}年{d.month}月{d.day}日（{WEEKDAYS_JA[d.weekday()]}）"


def _page(title: str, body: str, calendar_ui: bool = False) -> bytes:
    cal_btn = '<button type="button" class="calbtn" id="cal-btn" aria-label="カレンダー">📅</button>' if calendar_ui else ""
    cal_ui = (CAL_HTML + CAL_JS) if calendar_ui else ""
    return (
        f'<!doctype html><html lang="ja"><head><meta charset="utf-8">'
        f'<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{_e(title)}</title><style>{CSS}</style></head><body><main>"
        f'<div class="head"><h1><a href="/">Voice Memory</a></h1>{cal_btn}</div>{body}</main>{cal_ui}</body></html>'
    ).encode()


def _tag_chips(tags: list[dict], active: str = "", date: str = "") -> str:
    suffix = f'&amp;date={quote(date)}' if date else ""
    chips = "".join(
        f'<a class="tag{" on" if t["name"] == active else ""}" href="/?tag={quote(t["name"])}{suffix}">'
        f'{_e(t["name"])} <small>{t["count"]}</small></a>'
        for t in tags
    )
    return f'<div class="tags">{chips}</div>' if chips else ""


def _card(m: dict) -> str:
    tags = " ".join(f"#{_e(t)}" for t in m["tags"])
    text = m["summary"] or m["excerpt"]
    return (
        f'<a class="card" href="/m/{quote(m["id"])}"><h2>{_e(m["title"])}</h2>'
        f'<div class="meta">{_e(_fmt_dt(m["recorded_at"]))}{" · " + tags if tags else ""}</div>'
        f"<p>{_e(text)}</p></a>"
    )


def _href(q: str, tag: str, offset: int, date: str = "") -> str:
    params = [f"{k}={quote(v)}" for k, v in (("q", q), ("tag", tag), ("date", date)) if v]
    if offset:
        params.append(f"offset={offset}")
    return "/?" + "&amp;".join(params) if params else "/"


def render_index(query: str = "", tag: str = "", offset: int = 0, db_path=None, date: str = "") -> bytes:
    # fetch one extra row to know whether a next page exists
    n = PAGE_LIMIT + 1
    if date:
        day = _parse_day(date)  # ValidationError -> 400 in the handler
        date = day.isoformat()
        if query:
            items = db.search_memories(query, n, db_path=db_path, offset=offset, on_date=date)
        elif tag:
            items = db.memories_by_tag(tag, n, db_path=db_path, offset=offset, on_date=date)
        else:
            items = db.memories_by_date(date, n, db_path=db_path, offset=offset)
    elif query:
        items = db.search_memories(query, n, db_path=db_path, offset=offset)
    elif tag:
        items = db.memories_by_tag(tag, n, db_path=db_path, offset=offset)
    else:
        items = db.recent_memories(n, db_path=db_path, offset=offset)
    has_next = len(items) > PAGE_LIMIT
    items = items[:PAGE_LIMIT]
    hidden_date = f'<input type="hidden" name="date" value="{_e(date)}">' if date else ""
    form = (
        f'<form action="/"><input type="search" name="q" value="{_e(query)}" placeholder="検索">{hidden_date}'
        f"<button>検索</button></form>"
    )
    daybar = ""
    if date:
        prev_day = (day - timedelta(days=1)).isoformat()
        next_day = (day + timedelta(days=1)).isoformat()
        total = ""
        if not query and not tag:
            count = db.daily_counts(date[:7], db_path).get(date, 0)
            total = f" {count}件"
        daybar = (
            f'<div class="daybar"><a class="tag" href="{_href(query, tag, 0, prev_day)}">← 前日</a>'
            f"<strong>{_e(_fmt_day(date))}{total}</strong>"
            f'<a class="tag" href="{_href(query, tag, 0, next_day)}">翌日 →</a>'
            f'<a class="tag" href="{_href(query, tag, 0)}">× 解除</a></div>'
        )
    empty = '<p class="empty">この日の記録はありません。</p>' if date else '<p class="empty">該当するメモはありません。</p>'
    cards = "".join(_card(m) for m in items) or empty
    prev_label, next_label = ("← 前へ", "次へ →") if date else ("← 新しい方へ", "古い方へ →")
    links = []
    if offset:
        links.append(f'<a class="tag" href="{_href(query, tag, max(0, offset - PAGE_LIMIT), date)}">{prev_label}</a>')
    if has_next:
        links.append(f'<a class="tag" href="{_href(query, tag, offset + PAGE_LIMIT, date)}">{next_label}</a>')
    pager = f'<div class="tags">{"".join(links)}</div>' if links else ""
    return _page("Voice Memory", form + daybar + _tag_chips(db.list_tags(db_path), tag, date) + cards + pager,
                 calendar_ui=True)


def _position(m: dict) -> str:
    a, b = m.get("audio_start_sec"), m.get("audio_end_sec")
    if a is None or b is None:
        return ""
    mm = lambda x: f"{int(x) // 3600}:{int(x) % 3600 // 60:02d}:{int(x) % 60:02d}"
    return f" · 録音内 {mm(a)}〜{mm(b)}" + (" · アーカイブ済み" if m.get("archived") else "")


def render_detail(memory_id: str, db_path=None) -> bytes:
    m = db.get_memory(memory_id, db_path=db_path)
    tags = " ".join(f'<a class="tag" href="/?tag={quote(t)}">{_e(t)}</a>' for t in m["tags"])
    body = (
        f'<h2>{_e(m["title"])}</h2><div class="meta">{_e(_fmt_dt(m["recorded_at"]))} · {_e(m["source"])}{_position(m)}</div>'
        f'<div class="tags" style="margin-top:8px">{tags}</div>'
        f'<h3>要約</h3><div class="body">{_e(m["summary"]) or "（なし）"}</div>'
        f'<h3>文字起こし</h3><div class="body">{_e(m["transcript"])}</div>'
        f'<div class="row"><a class="btn" href="/m/{quote(m["id"])}/delete">このメモを削除…</a></div>'
    )
    return _page(m["title"], body)


def render_delete_confirm(memory_id: str, db_path=None) -> bytes:
    m = db.get_memory(memory_id, db_path=db_path)
    audio = "保存済みの音声ファイルも削除されます。" if m["audio_path"] else "音声ファイルはありません。"
    body = (
        f'<h2>削除の確認</h2><div class="card"><h2>{_e(m["title"])}</h2>'
        f'<div class="meta">{_e(_fmt_dt(m["recorded_at"]))}</div></div>'
        f"<p>このメモ（要約・文字起こし・タグ）を削除します。{audio}元に戻せません。</p>"
        f'<form class="row" method="post" action="/m/{quote(m["id"])}/delete">'
        f'<button class="danger">削除する</button><a class="btn" href="/m/{quote(m["id"])}">やめる</a></form>'
    )
    return _page("削除の確認", body)


def make_handler(db_path=None, recordings=None):
    class Handler(BaseHTTPRequestHandler):
        def _send(self, status: int, payload: bytes, content_type: str = "text/html; charset=utf-8") -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(payload)

        def _send_json(self, status: int, obj) -> None:
            self._send(status, json.dumps(obj).encode(), "application/json; charset=utf-8")

        def do_GET(self):  # noqa: N802
            url = urlparse(self.path)
            qs = parse_qs(url.query)
            try:
                if url.path == "/api/calendar":
                    month = qs.get("month", [""])[0].strip()
                    try:
                        counts = db.daily_counts(month, db_path)
                    except db.ValidationError:
                        self._send_json(400, {"error": "month must be YYYY-MM"})
                        return
                    self._send_json(200, {"month": month, "counts": counts})
                elif url.path == "/":
                    raw = qs.get("offset", ["0"])[0]
                    offset = int(raw) if raw.isdigit() else 0
                    self._send(200, render_index(
                        qs.get("q", [""])[0].strip(), qs.get("tag", [""])[0].strip(), offset, db_path,
                        qs.get("date", [""])[0].strip()))
                elif url.path.startswith("/m/") and url.path.endswith("/delete"):
                    self._send(200, render_delete_confirm(url.path[3:-7], db_path))
                elif url.path.startswith("/m/"):
                    self._send(200, render_detail(url.path[3:], db_path))
                else:
                    self._send(404, _page("Not found", '<p class="empty">ページがありません。</p>'))
            except db.NotFoundError:
                self._send(404, _page("Not found", '<p class="empty">メモが見つかりません。</p>'))
            except db.ValidationError:
                self._send(400, _page("Bad request", '<p class="empty">入力が不正です。</p>'))

        def do_POST(self):  # noqa: N802
            url = urlparse(self.path)
            try:
                # No auth here, so refuse cross-site form posts: the browser must say the page came from this host.
                origin = urlparse(self.headers.get("Origin") or "")
                if origin.netloc != self.headers.get("Host"):
                    self._send(403, _page("Forbidden", '<p class="empty">リクエストを拒否しました。</p>'))
                elif url.path.startswith("/m/") and url.path.endswith("/delete"):
                    db.delete_memory(url.path[3:-7], recordings=recordings, db_path=db_path)
                    self.send_response(303)
                    self.send_header("Location", "/")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
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
    ap = argparse.ArgumentParser(description="Mobile viewer (with delete) for voice memories.")
    ap.add_argument("--host", default=os.environ.get("VOICE_MEMORY_WEB_HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("VOICE_MEMORY_WEB_PORT", DEFAULT_PORT)))
    args = ap.parse_args()
    if args.host in ("0.0.0.0", "::"):
        ap.error("refusing to bind all interfaces; use 127.0.0.1 or a Tailscale IP")
    db.init_db()
    ThreadingHTTPServer((args.host, args.port), make_handler()).serve_forever()


if __name__ == "__main__":
    main()
