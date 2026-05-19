"""FastAPI web server — real-time AI news dashboard with WebSocket push."""

import json
import logging
import time
from typing import Set

import httpx
from fastapi import Depends, FastAPI, Form, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

import storage
import config
import auth
import subscribers

logger = logging.getLogger(__name__)

app = FastAPI(title="YunFlow")

_CACHE_TTL = 30 * 60  # 30 minutes
_MARKETS_TTL = 30     # 30 seconds (Yahoo only feeds the day-over-day baseline; live ticks come from Pyth on the frontend)
_markets_cache: dict = {}
_markets_cache_at: float = 0
_briefing_cache: dict = {}   # keyed by lang
_briefing_cache_at: dict = {}
_digest_cache: dict = {}     # keyed by lang
_digest_cache_at: dict = {}
_joke_cache: list = []
_joke_cache_at: float = 0

# ── WebSocket connection manager ───────────────────────────────────────────
class ConnectionManager:
    def __init__(self):
        self._connections: Set[WebSocket] = set()

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self._connections.add(ws)
        logger.debug("WS client connected (%d total)", len(self._connections))

    def disconnect(self, ws: WebSocket):
        self._connections.discard(ws)

    async def broadcast(self, data: dict):
        dead = set()
        for ws in self._connections:
            try:
                await ws.send_text(json.dumps(data))
            except Exception:
                dead.add(ws)
        self._connections -= dead

manager = ConnectionManager()


async def push_new_item(item: dict, item_type: str):
    """Called by monitors when a new item arrives — pushes to all WS clients."""
    await manager.broadcast({"type": item_type, "data": item})


# ── REST API ───────────────────────────────────────────────────────────────

def _lazy_translate(posts: list[dict], tweets: list[dict]):
    """Fill missing _zh fields via DeepSeek and persist back to SQLite."""
    if not config.DEEPSEEK_API_KEY:
        return
    import ai_processor

    targets = []  # (kind, obj, field, original_text)
    for p in posts:
        if not p.get("title_zh") and p.get("title"):
            targets.append(("post", p, "title_zh", p["title"]))
        if p.get("category") == "polymarket":
            continue  # summary contains Yes/No odds — keep in English
        if (not p.get("summary_zh") and p.get("summary")
                and p["summary"].strip() != p.get("title", "").strip()):
            targets.append(("post", p, "summary_zh", p["summary"][:500]))
    for t in tweets:
        if not t.get("text_zh") and t.get("text"):
            targets.append(("tweet", t, "text_zh", t["text"]))

    if not targets:
        return

    try:
        translated = ai_processor.translate_texts([x[3] for x in targets])
    except Exception as e:
        logger.warning("lazy translate failed: %s", e)
        return

    for (kind, obj, field, orig), zh in zip(targets, translated):
        if zh and zh != orig:
            obj[field] = zh

    # Persist (dedup by id to avoid duplicate UPDATEs when both fields filled)
    seen_posts, seen_tweets = set(), set()
    for kind, obj, _f, _o in targets:
        oid = obj.get("id")
        if not oid:
            continue
        try:
            if kind == "post" and oid not in seen_posts:
                seen_posts.add(oid)
                storage.update_post_translation(
                    oid, obj.get("title_zh", ""), obj.get("summary_zh", "")
                )
            elif kind == "tweet" and oid not in seen_tweets and obj.get("text_zh"):
                seen_tweets.add(oid)
                storage.update_tweet_translation(oid, obj["text_zh"])
        except Exception as e:
            logger.warning("persist translation failed: %s", e)


@app.get("/api/news")
def get_news(limit: int = 30, source: str = None, category: str = None):
    # Papers mode: skip tweets, return is_paper rows sorted by paper_score (not by date).
    if category == "papers":
        posts = storage.get_trending_papers(hours=24 * 30, limit=limit)
        _lazy_translate(posts, [])
        return [{"type": "post", "date": p.get("published", ""), "data": p} for p in posts]

    # Trump mode: merge news + X tweets, sort by date (newest first).
    if category == "trump":
        posts  = storage.get_latest_posts_by_category("trump", limit=limit)
        tweets = storage.get_latest_tweets(limit=10, category="trump")
        _lazy_translate(posts, tweets)
        items  = [{"type": "tweet", "date": t["created_at"], "data": t} for t in tweets]
        items += [{"type": "post",  "date": p.get("published", ""), "data": p} for p in posts]
        items.sort(key=lambda x: x["date"], reverse=True)
        return items[:limit]

    # Polymarket mode: posts only, sorted by fetched_at (most recently refreshed first).
    if category == "polymarket":
        posts = storage.get_latest_posts_by_category("polymarket", limit=limit)
        _lazy_translate(posts, [])
        return [{"type": "post", "date": p.get("published", ""), "data": p} for p in posts]

    # Venture / Web3 / US Stock / Geopolitics mode: fetch by category directly.
    if category in ("venture", "web3", "us_stock", "geopolitics"):
        posts = storage.get_latest_posts_by_category(category, limit=limit)
        _lazy_translate(posts, [])
        return [{"type": "post", "date": p.get("published", ""), "data": p} for p in posts]

    tweets = storage.get_latest_tweets(limit=limit)
    posts  = storage.get_latest_posts(limit=limit, source=source)
    _lazy_translate(posts, tweets)
    items  = []
    for t in tweets:
        items.append({"type": "tweet", "date": t["created_at"], "data": t})
    for p in posts:
        items.append({"type": "post", "date": p.get("published", ""), "data": p})
    items.sort(key=lambda x: x["date"], reverse=True)
    return items[:limit]


@app.get("/api/search")
def search(q: str, limit: int = 20):
    return storage.search_news(query=q, limit=limit)


@app.get("/api/stats")
def stats():
    return storage.get_stats()


@app.get("/api/health")
def health():
    return {"status": "ok", "feeds": len(config.RSS_FEEDS)}


@app.get("/api/visit-stats")
def visit_stats():
    return storage.get_visit_stats()


_MARKET_SYMBOLS = [
    {"symbol": "QQQ",       "name": "QQQ",    "name_en": "QQQ"},
    {"symbol": "SPY",       "name": "SPY",    "name_en": "SPY"},
    {"symbol": "DIA",       "name": "DOW",    "name_en": "DOW"},
    {"symbol": "BTC-USD",   "name": "BTC",    "name_en": "BTC"},
    {"symbol": "GC=F",      "name": "GOLD",   "name_en": "Gold"},
    {"symbol": "SI=F",      "name": "SILVER", "name_en": "Silver"},
    {"symbol": "CL=F",      "name": "WTI",    "name_en": "WTI"},
    {"symbol": "DX-Y.NYB",  "name": "DXY",    "name_en": "DXY"},
]

_YF_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json",
}

def _fetch_one_market(sym: str, name: str, name_en: str) -> dict:
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}?interval=1m&range=1d"
    resp = httpx.get(url, timeout=8, headers=_YF_HEADERS)
    meta = resp.json()["chart"]["result"][0]["meta"]
    price = meta.get("regularMarketPrice")
    prev  = meta.get("chartPreviousClose") or meta.get("previousClose") or price
    change = round(price - prev, 4) if price is not None and prev else None
    pct    = round(change / prev * 100, 2) if change is not None and prev else None
    return {
        "symbol":  sym,
        "name":    name,
        "name_en": name_en,
        "price":   round(price, 2) if price is not None else None,
        "change":  change,
        "pct":     pct,
        "ts":      meta.get("regularMarketTime"),
    }


@app.get("/api/markets")
def get_markets():
    global _markets_cache, _markets_cache_at
    if _markets_cache and time.time() - _markets_cache_at < _MARKETS_TTL:
        return _markets_cache

    result = []
    for m in _MARKET_SYMBOLS:
        try:
            result.append(_fetch_one_market(m["symbol"], m["name"], m["name_en"]))
        except Exception as e:
            logger.warning("market fetch failed for %s: %s", m["symbol"], e)
            # keep last known value for this symbol if available
            existing = next((x for x in _markets_cache if x["symbol"] == m["symbol"]), None)
            if existing:
                result.append(existing)

    if result:
        _markets_cache = result
        _markets_cache_at = time.time()
    return result


@app.get("/api/podcasts")
def get_podcasts():
    podcasts = [f for f in config.RSS_FEEDS if f.get("podcast")]
    result = []
    for p in podcasts:
        recent = storage.get_latest_posts(limit=5, source=p["name"])
        result.append({
            "name": p["name"],
            "site": p.get("site", ""),
            "feed": p["url"],
            "alert": p.get("alert", False),
            "recent": recent,
        })
    return result


@app.get("/api/digest-summary")
def digest_summary(lang: str = "zh"):
    """Return cached AI digest; regenerate if cache is older than 30 min."""
    if _digest_cache.get(lang) is not None and time.time() - _digest_cache_at.get(lang, 0) < _CACHE_TTL:
        return {"summary": _digest_cache[lang]}
    import ai_processor
    # Each category contributes a fixed quota so the digest stays balanced
    posts_by_cat = storage.get_recent_posts_by_category(hours=24, limit_per_category=15)
    CAT_QUOTA = {"us_stock": 8, "trump": 7, "geopolitics": 7, "venture": 6,
                 "polymarket": 5, "ai": 5, "papers": 3, "web3": 3}
    def _quality(p):
        return (int(p.get("hn_score") or 0)
                + int(p.get("hf_upvotes") or 0) * 2
                + float(p.get("paper_score") or 0))
    all_posts = []
    for cat, posts in posts_by_cat.items():
        quota = CAT_QUOTA.get(cat, 4)
        top = sorted(posts, key=_quality, reverse=True)[:quota]
        all_posts.extend(top)
    items = [{"type": "post", "data": p} for p in all_posts]
    result = ai_processor.generate_digest_summary(items, lang=lang)
    if result:
        _digest_cache[lang] = result
        _digest_cache_at[lang] = time.time()
    return {"summary": result}


@app.get("/api/joke")
def get_joke(refresh: bool = False):
    """Return cached daily jokes grounded in today's news; regenerate every 2 hours."""
    global _joke_cache, _joke_cache_at
    _JOKE_TTL = 30 * 60  # 30 minutes
    if not refresh and _joke_cache and time.time() - _joke_cache_at < _JOKE_TTL:
        return {"jokes": _joke_cache}
    import ai_processor
    posts_by_cat = storage.get_recent_posts_by_category(hours=24, limit_per_category=10)
    all_posts = []
    for posts in posts_by_cat.values():
        all_posts.extend(posts[:5])
    items = [{"type": "post", "data": p} for p in all_posts]
    result = ai_processor.generate_joke(items)
    if result:
        _joke_cache = result
        _joke_cache_at = time.time()
    return {"jokes": _joke_cache}


@app.get("/api/briefing")
def daily_briefing(lang: str = "zh", refresh: bool = False):
    """Return cached daily briefing; regenerate if cache is older than 30 min."""
    if not refresh and _briefing_cache.get(lang) is not None and time.time() - _briefing_cache_at.get(lang, 0) < _CACHE_TTL:
        return _briefing_cache[lang]
    import ai_processor
    posts_by_cat = storage.get_recent_posts_by_category(hours=24, limit_per_category=30)
    result = ai_processor.generate_daily_briefing(posts_by_cat, lang=lang)
    if result.get("sections"):
        _briefing_cache[lang] = result
        _briefing_cache_at[lang] = time.time()
    return result


# ── WebSocket ──────────────────────────────────────────────────────────────

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await manager.connect(ws)
    try:
        while True:
            await ws.receive_text()  # keep connection alive
    except WebSocketDisconnect:
        manager.disconnect(ws)


# ── Dashboard HTML ─────────────────────────────────────────────────────────

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="zh" data-theme="auto">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>YunFlow</title>
<style>
  /* ── Tokens ── */
  :root {
    --bg: #f5f6f8; --surface: #ffffff; --surface2: #f0f1f4;
    --border: #e2e4e9; --border2: #ced0d6;
    --text: #111318; --text2: #4a4f5c; --muted: #8a8fa0;
    --accent: #2563eb; --accent-bg: #eff4ff;
    --green: #16a34a; --green-bg: #f0fdf4;
    --red: #dc2626; --tweet: #1d9bf0; --tweet-bg: #eff8ff;
    --shadow: 0 1px 3px rgba(0,0,0,.08);
  }
  [data-theme="dark"] {
    --bg: #0f1117; --surface: #181c25; --surface2: #1e2330;
    --border: #2a2f3d; --border2: #363c4e;
    --text: #e8eaf0; --text2: #9ba3b8; --muted: #5c6378;
    --accent: #4f8ef7; --accent-bg: #1a2540;
    --green: #34d399; --green-bg: #0d2418;
    --red: #f87171; --tweet: #38bdf8; --tweet-bg: #0c1f2e;
    --shadow: 0 1px 4px rgba(0,0,0,.3);
  }
  @media (prefers-color-scheme: dark) {
    [data-theme="auto"] {
      --bg: #0f1117; --surface: #181c25; --surface2: #1e2330;
      --border: #2a2f3d; --border2: #363c4e;
      --text: #e8eaf0; --text2: #9ba3b8; --muted: #5c6378;
      --accent: #4f8ef7; --accent-bg: #1a2540;
      --green: #34d399; --green-bg: #0d2418;
      --red: #f87171; --tweet: #38bdf8; --tweet-bg: #0c1f2e;
      --shadow: 0 1px 4px rgba(0,0,0,.3);
    }
  }

  * { box-sizing: border-box; margin: 0; padding: 0; }
  html { -webkit-text-size-adjust: 100%; text-size-adjust: 100%; }
  body { background: var(--bg); color: var(--text); font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', 'PingFang SC', sans-serif; font-size: 14px; line-height: 1.5; }

  /* ── Layout ── */
  .layout { display: flex; height: 100vh; overflow: hidden; }
  .sidebar { width: 220px; background: var(--surface); border-right: 1px solid var(--border); display: flex; flex-direction: column; flex-shrink: 0; overflow-y: auto; }
  .main { flex: 1; display: flex; flex-direction: column; overflow: hidden; min-width: 0; }

  /* ── Sidebar ── */
  .sidebar-logo { padding: 18px 16px 14px; border-bottom: 1px solid var(--border); display: flex; align-items: center; justify-content: space-between; }
  .sidebar-logo h1 { font-size: 14px; font-weight: 700; color: var(--text); letter-spacing: -.01em; }
  .status-dot { width: 7px; height: 7px; border-radius: 50%; background: var(--green); display: inline-block; margin-right: 7px; animation: pulse 2.5s infinite; flex-shrink: 0; }
  @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:.35} }
  .theme-btn { background: none; border: 1px solid var(--border); border-radius: 6px; color: var(--muted); font-size: 14px; width: 28px; height: 28px; cursor: pointer; display: flex; align-items: center; justify-content: center; flex-shrink: 0; }
  .theme-btn:hover { border-color: var(--accent); color: var(--accent); }

  .nav-section { padding: 14px 12px 4px; font-size: 10px; font-weight: 700; color: var(--muted); text-transform: uppercase; letter-spacing: .1em; }
  .nav-btn { display: flex; align-items: center; gap: 8px; width: 100%; padding: 7px 12px; background: none; border: none; color: var(--text2); font-size: 13px; cursor: pointer; text-align: left; border-radius: 6px; margin: 1px 4px; width: calc(100% - 8px); transition: background .12s, color .12s; }
  .nav-btn:hover { background: var(--surface2); color: var(--text); }
  .nav-btn.active { background: var(--accent-bg); color: var(--accent); font-weight: 600; }
  .nav-btn .cnt { margin-left: auto; font-size: 11px; color: var(--muted); background: var(--surface2); padding: 1px 6px; border-radius: 8px; }
  .nav-btn.active .cnt { background: var(--accent-bg); color: var(--accent); }

  .stats-box { margin: auto 12px 16px; background: var(--surface2); border-radius: 8px; padding: 12px; }
  .stat-row { display: flex; justify-content: space-between; font-size: 12px; padding: 3px 0; }
  .stat-label { color: var(--muted); }
  .stat-val { color: var(--text); font-weight: 500; font-variant-numeric: tabular-nums; }
  .status-text { font-size: 11px; color: var(--muted); margin-top: 8px; padding-top: 8px; border-top: 1px solid var(--border); }

  /* ── Topbar ── */
  .topbar { padding: 12px 20px; border-bottom: 1px solid var(--border); display: flex; align-items: center; gap: 10px; background: var(--surface); flex-shrink: 0; }
  .search-wrap { flex: 1; position: relative; }
  .search-wrap input { width: 100%; background: var(--surface2); border: 1px solid var(--border); border-radius: 8px; padding: 7px 12px 7px 34px; color: var(--text); font-size: 13px; outline: none; transition: border-color .15s; }
  .search-wrap input::placeholder { color: var(--muted); }
  .search-wrap input:focus { border-color: var(--accent); background: var(--surface); }
  .search-icon { position: absolute; left: 10px; top: 50%; transform: translateY(-50%); color: var(--muted); font-size: 14px; pointer-events: none; }
  .new-pill { background: var(--red); color: #fff; font-size: 11px; font-weight: 600; padding: 3px 10px; border-radius: 20px; display: none; cursor: pointer; white-space: nowrap; }
  .new-pill.show { display: block; }

  /* ── Feed ── */
  .feed { flex: 1; overflow-y: auto; padding: 16px 20px; }
  .feed::-webkit-scrollbar { width: 5px; }
  .feed::-webkit-scrollbar-thumb { background: var(--border2); border-radius: 3px; }

  /* ── Briefing ── */
  .briefing-panel { background: var(--surface); border: 1px solid var(--border); border-radius: 12px; padding: 16px 18px; margin-bottom: 14px; box-shadow: var(--shadow); }
  .panel-header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 14px; }
  .panel-title { font-size: 13px; font-weight: 700; color: var(--text); display: flex; align-items: center; gap: 6px; }
  .panel-actions { display: flex; align-items: center; gap: 8px; }
  .panel-meta { font-size: 11px; color: var(--muted); }
  .refresh-btn { background: none; border: 1px solid var(--border); border-radius: 5px; color: var(--muted); font-size: 12px; padding: 3px 9px; cursor: pointer; transition: border-color .12s, color .12s; }
  .refresh-btn:hover { border-color: var(--accent); color: var(--accent); }

  .briefing-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 10px; }
  @media (max-width: 1100px) { .briefing-grid { grid-template-columns: repeat(2, 1fr); } }
  @media (max-width: 719px)  { .briefing-grid { grid-template-columns: 1fr; } }

  .b-section { background: var(--surface2); border-radius: 8px; padding: 12px 13px; border: 1px solid var(--border); }
  .b-title { font-size: 14px; font-weight: 700; color: var(--text); margin-bottom: 9px; }
  .b-list { list-style: none; }
  .b-list li { font-size: 14px; color: var(--text2); line-height: 1.5; padding: 4px 0 4px 14px; position: relative; border-bottom: 1px solid var(--border); }
  .b-list li a { color: inherit; text-decoration: none; }
  .b-list li a:hover { color: var(--accent); text-decoration: underline; }
  .b-list li:last-child { border-bottom: none; padding-bottom: 0; }
  .b-list li::before { content: "·"; position: absolute; left: 3px; color: var(--accent); font-weight: 900; font-size: 17px; line-height: 1.4; }
  .panel-loading { font-size: 14px; color: var(--muted); padding: 4px 0; }

  /* ── Digest ── */
  .joke-panel { background: var(--surface); border: 1px solid var(--border); border-left: 3px solid #f59e0b; border-radius: 12px; padding: 14px 18px; margin-bottom: 14px; }
  .joke-item { font-size: 14px; color: var(--text); line-height: 1.8; white-space: pre-wrap; padding: 10px 0; }
  .joke-item + .joke-item { border-top: 1px solid var(--border); }
  .digest-panel { background: var(--accent-bg); border: 1px solid var(--border); border-radius: 12px; padding: 14px 18px; margin-bottom: 14px; }
  .digest-text { font-size: 16px; color: var(--text2); line-height: 1.75; }
  .digest-text a { color: inherit; text-decoration: none; }
  .digest-text a:hover { color: var(--accent); text-decoration: underline; }

  /* ── Cards ── */
  .card { background: var(--surface); border: 1px solid var(--border); border-radius: 10px; padding: 14px 16px; margin-bottom: 10px; box-shadow: var(--shadow); transition: border-color .15s, box-shadow .15s; }
  .card:hover { border-color: var(--border2); box-shadow: 0 2px 8px rgba(0,0,0,.1); }
  .card.new-item { animation: slideIn .25s ease; border-color: var(--accent); }
  @keyframes slideIn { from { opacity:0; transform:translateY(-6px); } to { opacity:1; transform:translateY(0); } }

  .card-meta { display: flex; align-items: center; gap: 6px; margin-bottom: 8px; flex-wrap: wrap; }
  .tag { font-size: 13px; font-weight: 600; padding: 2px 7px; border-radius: 4px; white-space: nowrap; }
  .tag-post  { background: var(--accent-bg); color: var(--accent); }
  .tag-tweet { background: var(--tweet-bg); color: var(--tweet); }
  .tag-t1    { background: var(--green-bg); color: var(--green); }
  .tag-cat   { background: var(--surface2); color: var(--muted); }
  .tag-polymarket  { background: #f0fdf4; color: #15803d; }
  [data-theme="dark"] .tag-polymarket { background: #052e16; color: #4ade80; }
  .tag-paper-uv    { background: #fef3c7; color: #b45309; }
  .tag-paper-arxiv { background: #ede9fe; color: #6d28d9; font-family: ui-monospace, monospace; }
  .tag-paper-co    { background: #fce7f3; color: #be185d; }
  [data-theme="dark"] .tag-paper-uv    { background: #3a2e0a; color: #fbbf24; }
  [data-theme="dark"] .tag-paper-arxiv { background: #2a1f4a; color: #c4b5fd; }
  [data-theme="dark"] .tag-paper-co    { background: #3a1228; color: #f9a8d4; }
  .card-paper-footer { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; font-size: 14px; color: var(--muted); margin-top: 8px; }
  .card-paper-footer .authors { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .card-paper-footer .pdf-link { color: var(--accent); text-decoration: none; font-weight: 500; }
  .card-paper-footer .pdf-link:hover { text-decoration: underline; }
  .card-date { font-size: 13px; color: var(--muted); margin-left: auto; }

  .card-title { font-size: 18px; font-weight: 600; line-height: 1.4; color: var(--text); margin-bottom: 4px; }
  .card-title a { color: inherit; text-decoration: none; }
  .card-title a:hover { color: var(--accent); }
  .card-title-zh { font-size: 16px; color: var(--text2); margin-bottom: 8px; font-weight: 400; }

  .card-summary-zh { font-size: 16px; color: var(--text2); line-height: 1.6; margin-bottom: 4px; }
  .card-summary-en { font-size: 14px; color: var(--muted); line-height: 1.5; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; margin-bottom: 10px; }

  .card-footer { display: flex; align-items: center; gap: 10px; margin-top: 10px; padding-top: 8px; border-top: 1px solid var(--border); }
  .card-link { font-size: 14px; color: var(--accent); text-decoration: none; font-weight: 500; }
  .card-link:hover { text-decoration: underline; }
  .card-eng { font-size: 14px; color: var(--muted); margin-left: auto; }

  .tweet-text { font-size: 17px; color: var(--text); line-height: 1.6; margin-bottom: 2px; }
  .tweet-text-zh { font-size: 16px; color: var(--text2); line-height: 1.6; margin-top: 4px; margin-bottom: 4px; }

  .empty { text-align: center; padding: 50px 20px; color: var(--muted); }
  .empty-icon { font-size: 43px; margin-bottom: 10px; }
  .loading-text { text-align: center; padding: 20px; color: var(--muted); font-size: 16px; }

  /* ── Podcast panel ── */
  .podcast-card { background: var(--surface); border: 1px solid var(--border); border-radius: 10px; padding: 18px 20px; margin-bottom: 14px; box-shadow: var(--shadow); }
  .podcast-header { display: flex; align-items: flex-start; gap: 14px; margin-bottom: 14px; }
  .podcast-icon { font-size: 43px; flex-shrink: 0; }
  .podcast-info { flex: 1; min-width: 0; }
  .podcast-name { font-size: 20px; font-weight: 700; color: var(--text); margin-bottom: 5px; }
  .podcast-links { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
  .pod-link { font-size: 14px; font-weight: 500; padding: 3px 10px; border-radius: 5px; text-decoration: none; border: 1px solid var(--border); color: var(--text2); transition: border-color .12s, color .12s, background .12s; display: inline-flex; align-items: center; gap: 4px; }
  .pod-link:hover { border-color: var(--accent); color: var(--accent); background: var(--accent-bg); }
  .pod-link.alert-badge { border-color: #7c3aed44; color: #7c3aed; background: #f5f3ff; }
  [data-theme="dark"] .pod-link.alert-badge { color: #a78bfa; background: #2d1b69; border-color: #7c3aed66; }
  .podcast-episodes { border-top: 1px solid var(--border); padding-top: 12px; }
  .ep-label { font-size: 13px; font-weight: 700; color: var(--muted); text-transform: uppercase; letter-spacing: .08em; margin-bottom: 8px; }
  .ep-item { padding: 8px 0; border-bottom: 1px solid var(--border); display: flex; gap: 10px; align-items: baseline; }
  .ep-item:last-child { border-bottom: none; padding-bottom: 0; }
  .ep-date { font-size: 13px; color: var(--muted); white-space: nowrap; flex-shrink: 0; }
  .ep-title { font-size: 16px; color: var(--text); line-height: 1.4; }
  .ep-title a { color: inherit; text-decoration: none; }
  .ep-title a:hover { color: var(--accent); text-decoration: underline; }
  .ep-empty { font-size: 14px; color: var(--muted); font-style: italic; }

  /* ── Trump avatar ── */
  .trump-avatar { width: 18px; height: 18px; border-radius: 50%; object-fit: cover; vertical-align: middle; flex-shrink: 0; }
  .trump-bnav   { width: 24px; height: 24px; border-radius: 50%; object-fit: cover; }

  /* ── Trump Watch ── */
  .trump-panel { background: var(--surface); border: 2px solid #dc2626; border-radius: 12px; padding: 16px 18px; margin-bottom: 14px; box-shadow: 0 2px 12px rgba(220,38,38,.12); }
  [data-theme="dark"] .trump-panel { box-shadow: 0 2px 12px rgba(220,38,38,.2); }
  .trump-view-btn { background: none; border: 1px solid #dc2626; border-radius: 5px; color: #dc2626; font-size: 12px; font-weight: 700; padding: 3px 10px; cursor: pointer; transition: background .12s; }
  .trump-view-btn:hover { background: #fef2f2; }
  [data-theme="dark"] .trump-view-btn:hover { background: #3a0a0a; }
  .trump-list { display: flex; flex-direction: column; gap: 8px; }
  .trump-item { padding: 9px 11px; background: var(--surface2); border-radius: 7px; border-left: 3px solid #dc2626; }
  .trump-item-meta { display: flex; align-items: center; gap: 8px; margin-bottom: 3px; }
  .trump-src { font-size: 11px; font-weight: 700; color: #dc2626; }
  .trump-date { font-size: 11px; color: var(--muted); margin-left: auto; }
  .trump-title { font-size: 14px; color: var(--text); line-height: 1.4; }
  .trump-title a { color: inherit; text-decoration: none; }
  .trump-title a:hover { color: #dc2626; text-decoration: underline; }
  .nav-btn.trump-active { background: #fef2f2; color: #dc2626; font-weight: 600; }
  [data-theme="dark"] .nav-btn.trump-active { background: #3a0a0a; color: #f87171; }

  /* ── Mobile ── */
  .hamburger-btn { display: none; background: none; border: 1px solid var(--border); border-radius: 6px; color: var(--muted); font-size: 18px; width: 36px; height: 36px; align-items: center; justify-content: center; cursor: pointer; flex-shrink: 0; }
  .hamburger-btn:hover { border-color: var(--accent); color: var(--accent); }
  .lang-pill { display: none; align-items: center; justify-content: center; background: var(--accent); color: #fff; border: none; border-radius: 20px; font-size: 13px; font-weight: 700; padding: 0 14px; height: 36px; cursor: pointer; flex-shrink: 0; letter-spacing: .03em; transition: opacity .15s; }
  .lang-pill:hover { opacity: .85; }
  .overlay { display: none; position: fixed; inset: 0; background: rgba(0,0,0,.4); z-index: 199; }
  .overlay.show { display: block; }
  .bottom-nav { display: none; }
  @media (max-width: 719px) {
    .sidebar { position: fixed; top: 0; left: 0; height: 100%; width: 260px; z-index: 200; transform: translateX(-260px); transition: transform .25s ease; overflow-y: auto; }
    .sidebar.open { transform: translateX(0); box-shadow: 4px 0 24px rgba(0,0,0,.18); }
    .hamburger-btn { display: flex; }
    .lang-pill { display: flex; }
    .topbar { padding: 10px 12px; }
    .feed { padding: 10px 12px; padding-bottom: 70px; }
    .card { padding: 12px 14px; }
    .card-title { font-size: 17px; }
    .briefing-panel, .digest-panel, .joke-panel { padding: 12px 14px; }
    .bottom-nav { display: flex; position: fixed; bottom: 0; left: 0; right: 0; background: var(--surface); border-top: 1px solid var(--border); z-index: 100; padding-bottom: env(safe-area-inset-bottom); }
  }
  @media (min-width: 720px) { .bottom-nav { display: none !important; } }
  .bnav-btn { flex: 1; display: flex; flex-direction: column; align-items: center; gap: 1px; background: none; border: none; color: var(--muted); font-size: 10px; padding: 7px 2px; cursor: pointer; transition: color .12s; }
  .bnav-btn span { font-size: 18px; line-height: 1.2; }
  .bnav-btn label { cursor: pointer; }
  .bnav-btn.active { color: var(--accent); }


  /* ── Market Ticker ── */
  .market-bar { display: flex; align-items: stretch; gap: 0; background: var(--surface); border-bottom: 1px solid var(--border); flex-shrink: 0; min-width: 0; }
  .market-tiles { display: flex; flex: 1; min-width: 0; overflow-x: auto; scrollbar-width: none; scroll-snap-type: x proximity; -webkit-overflow-scrolling: touch; }
  .market-tiles::-webkit-scrollbar { display: none; }
  .market-tile { display: flex; flex-direction: column; align-items: center; justify-content: center; padding: 8px 18px; flex: 0 0 auto; min-width: 110px; border-right: 1px solid var(--border); cursor: default; transition: background .12s; position: relative; scroll-snap-align: start; }
  .market-tile:last-child { border-right: none; }
  .market-tile:hover { background: var(--surface2); }
  .market-tile-name { font-size: 11px; font-weight: 700; color: var(--muted); letter-spacing: .06em; text-transform: uppercase; margin-bottom: 2px; }
  .market-tile-price { font-size: 16px; font-weight: 700; color: var(--text); font-variant-numeric: tabular-nums; letter-spacing: -.01em; }
  .market-tile-change { font-size: 11px; font-weight: 600; font-variant-numeric: tabular-nums; margin-top: 1px; }
  .market-up   { color: var(--green); }
  .market-down { color: var(--red); }
  .market-flat { color: var(--muted); }
  .market-bar-footer { font-size: 10px; color: var(--muted); padding: 0 12px; display: flex; align-items: center; gap: 6px; white-space: nowrap; flex-shrink: 0; border-left: 1px solid var(--border); }
  .market-refresh-btn { background: none; border: none; color: var(--muted); font-size: 12px; cursor: pointer; padding: 2px 4px; border-radius: 4px; }
  .market-refresh-btn:hover { color: var(--accent); }
  @media (max-width: 719px) {
    .market-tile { padding: 6px 12px; min-width: 88px; }
    .market-tile-name { font-size: 10px; }
    .market-tile-price { font-size: 13px; letter-spacing: -.03em; }
    .market-tile-change { font-size: 10px; }
    .market-bar-footer { display: none; }
  }
</style>
</head>
<body>
<div class="layout">
  <div class="overlay" id="overlay" onclick="closeSidebar()"></div>

  <!-- Sidebar -->
  <aside class="sidebar">
    <div class="sidebar-logo">
      <h1><span class="status-dot" id="statusDot"></span>YunFlow</h1>
      <div style="display:flex;gap:5px;align-items:center">
        <button class="theme-btn" id="langBtn" onclick="toggleLang()" title="切换语言">EN</button>
        <button class="theme-btn" id="themeBtn" onclick="toggleTheme()">☀</button>
      </div>
    </div>

    <button class="nav-btn active" onclick="setFilter('all',this)"><span data-i18n="navAll">🌐 全部</span><span class="cnt" id="cnt-all">0</span></button>

    <div class="nav-section" data-i18n="secCategories" style="margin-top:6px;">分类</div>
    <button class="nav-btn" onclick="setCategory('polymarket',this)"><span data-i18n="navPolymarket">🎯 Polymarket</span><span class="cnt" id="cnt-polymarket">0</span></button>
    <button class="nav-btn" onclick="setCategory('venture',this)"><span data-i18n="navVenture">💼 创投</span><span class="cnt" id="cnt-venture">0</span></button>
    <button class="nav-btn" onclick="setCategory('us_stock',this)"><span data-i18n="navStocks">📈 美股</span><span class="cnt" id="cnt-us_stock">0</span></button>
    <button class="nav-btn" id="navTrump" onclick="setCategory('trump',this)"><img src="https://upload.wikimedia.org/wikipedia/commons/thumb/5/56/Donald_Trump_official_portrait.jpg/40px-Donald_Trump_official_portrait.jpg" class="trump-avatar" onerror="this.replaceWith(document.createTextNode('🇺🇸'))"><span> Trump</span><span class="cnt" id="cnt-trump">0</span></button>
    <button class="nav-btn" onclick="setCategory('geopolitics',this)"><span data-i18n="navGeopolitics">🌍 地缘政治</span><span class="cnt" id="cnt-geopolitics">0</span></button>
    <button class="nav-btn" onclick="setCategory('ai',this)"><span data-i18n="navAI">🤖 AI</span><span class="cnt" id="cnt-ai">0</span></button>
    <button class="nav-btn" onclick="setCategory('papers',this)"><span data-i18n="navPapers">📄 论文</span><span class="cnt" id="cnt-papers">0</span></button>
    <button class="nav-btn" onclick="setCategory('web3',this)"><span data-i18n="navWeb3">🔗 Web3</span><span class="cnt" id="cnt-web3">0</span></button>
    <a class="nav-btn" href="/earnings" style="text-decoration:none;"><span data-i18n="navEarnings">📅 财报日历</span></a>

    <div class="nav-section" data-i18n="secFilter" style="margin-top:6px;">筛选</div>
    <button class="nav-btn" onclick="setFilter('posts',this)"><span data-i18n="navPosts">📰 博客文章</span><span class="cnt" id="cnt-posts">0</span></button>
    <button class="nav-btn" onclick="setFilter('tweets',this)"><span data-i18n="navTweets">𝕏 推文</span><span class="cnt" id="cnt-tweets">0</span></button>
    <button class="nav-btn" id="navPodcast" onclick="showPodcasts(this)"><span data-i18n="navPodcasts">🎙 播客</span></button>

    <div class="nav-section" data-i18n="secSources" style="margin-top:6px;">来源</div>
    <div id="sourceList"></div>

    <div class="stats-box">
      <div class="stat-row"><span class="stat-label" data-i18n="statPosts">文章</span><span class="stat-val" id="statPosts">—</span></div>
      <div class="stat-row"><span class="stat-label" data-i18n="statTweets">推文</span><span class="stat-val" id="statTweets">—</span></div>
      <div class="stat-row"><span class="stat-label" data-i18n="statLatest">最新</span><span class="stat-val" id="statLast">—</span></div>
      <div class="stat-row" style="margin-top:6px;padding-top:6px;border-top:1px solid var(--border)"><span class="stat-label" data-i18n="statVisitTotal">总访问</span><span class="stat-val" id="statVisitTotal">—</span></div>
      <div class="stat-row"><span class="stat-label" data-i18n="statVisitToday">今日</span><span class="stat-val" id="statVisitToday">—</span></div>
      <div class="stat-row"><span class="stat-label" data-i18n="statVisitWeek">本周</span><span class="stat-val" id="statVisitWeek">—</span></div>
      <div class="stat-row"><span class="stat-label" data-i18n="statVisitMonth">本月</span><span class="stat-val" id="statVisitMonth">—</span></div>
      <div class="status-text" id="statusText">连接中…</div>
    </div>
  </aside>

  <!-- Main -->
  <div class="main">
    <div class="topbar">
      <button class="hamburger-btn" onclick="toggleSidebar()">☰</button>
      <div class="search-wrap">
        <span class="search-icon">🔍</span>
        <input type="text" id="searchInput" placeholder="搜索新闻…" oninput="onSearch(this.value)">
      </div>
      <button class="lang-pill" id="langPill" onclick="toggleLang()">EN</button>
      <!--ACCOUNT_PILL-->
      <div class="new-pill" id="newBadge" onclick="scrollToTop()">↑ 有新内容</div>
    </div>

    <!-- Market Ticker -->
    <div class="market-bar" id="marketBar">
      <div class="market-tiles" id="marketTiles">
        <div class="market-tile" id="mkt-QQQ"><div class="market-tile-name">QQQ</div><div class="market-tile-price">—</div><div class="market-tile-change market-flat">—</div></div>
        <div class="market-tile" id="mkt-SPY"><div class="market-tile-name">SPY</div><div class="market-tile-price">—</div><div class="market-tile-change market-flat">—</div></div>
        <div class="market-tile" id="mkt-DIA"><div class="market-tile-name">DOW</div><div class="market-tile-price">—</div><div class="market-tile-change market-flat">—</div></div>
        <div class="market-tile" id="mkt-BTC"><div class="market-tile-name">BTC</div><div class="market-tile-price">—</div><div class="market-tile-change market-flat">—</div></div>
        <div class="market-tile" id="mkt-XAU"><div class="market-tile-name">GOLD</div><div class="market-tile-price">—</div><div class="market-tile-change market-flat">—</div></div>
        <div class="market-tile" id="mkt-XAG"><div class="market-tile-name">SILVER</div><div class="market-tile-price">—</div><div class="market-tile-change market-flat">—</div></div>
        <div class="market-tile" id="mkt-WTI"><div class="market-tile-name">WTI</div><div class="market-tile-price">—</div><div class="market-tile-change market-flat">—</div></div>
        <div class="market-tile" id="mkt-DXY"><div class="market-tile-name">DXY</div><div class="market-tile-price">—</div><div class="market-tile-change market-flat">—</div></div>
      </div>
      <div class="market-bar-footer"><span id="marketUpdated"></span><button class="market-refresh-btn" onclick="loadMarkets()" title="刷新">↻</button></div>
    </div>

    <div class="feed" id="feed">
      <!-- AI 摘要 -->
      <div class="digest-panel">
        <div class="panel-header">
          <div class="panel-title" data-i18n="digestTitle">📋 资讯摘要</div>
          <div class="panel-actions">
            <button class="refresh-btn" data-i18n="refresh" onclick="loadDigest()">↻ 刷新</button>
          </div>
        </div>
        <div class="digest-text" id="digestText"><span class="panel-loading" data-i18n="loadingDigest">正在生成摘要…</span></div>
      </div>

      <!-- 今日笑话 -->
      <div class="joke-panel">
        <div class="panel-header">
          <div class="panel-title" data-i18n="jokeTitle">😂 今日笑话</div>
          <div class="panel-actions">
            <button class="refresh-btn" onclick="loadJoke(true)">↻ 刷新</button>
          </div>
        </div>
        <div id="jokeBody"><span class="panel-loading">正在生成…</span></div>
      </div>

      <!-- 速报 -->
      <div class="briefing-panel">
        <div class="panel-header">
          <div class="panel-title" data-i18n="briefingTitle">⚡ 每日要闻速报</div>
          <div class="panel-actions">
            <span class="panel-meta" id="briefingMeta"></span>
            <button class="refresh-btn" data-i18n="refresh" onclick="loadBriefing()">↻ 刷新</button>
          </div>
        </div>
        <div id="briefingBody"><span class="panel-loading" data-i18n="loadingBriefing">正在生成速报…</span></div>
      </div>

      <div id="cardFeed"><div class="loading-text" data-i18n="loading">加载中…</div></div>
    </div>
  </div>
</div>

<nav class="bottom-nav" id="bottomNav">
  <button class="bnav-btn" onclick="setFilter('all',null);bnav(this)"><span>🌐</span><label data-i18n="bnavAll">全部</label></button>
  <button class="bnav-btn" onclick="setCategoryMobile('polymarket',this)"><span>🎯</span><label data-i18n="bnavPolymarket">预测</label></button>
  <button class="bnav-btn" onclick="setCategoryMobile('venture',this)"><span>💼</span><label data-i18n="bnavVenture">创投</label></button>
  <button class="bnav-btn" onclick="setCategoryMobile('us_stock',this)"><span>📈</span><label data-i18n="bnavStocks">美股</label></button>
  <button class="bnav-btn" onclick="setCategoryMobile('geopolitics',this)"><span>🌍</span><label data-i18n="bnavGeo">地缘</label></button>
  <button class="bnav-btn" onclick="setCategoryMobile('trump',this)"><span>🇺🇸</span><label data-i18n="bnavTrump">特朗普</label></button>
  <button class="bnav-btn" onclick="setCategoryMobile('ai',this)"><span>🤖</span><label>AI</label></button>
  <button class="bnav-btn" onclick="setCategoryMobile('papers',this)"><span>📄</span><label data-i18n="bnavPapers">论文</label></button>
  <button class="bnav-btn" onclick="setCategoryMobile('web3',this)"><span>🔗</span><label>Web3</label></button>
</nav>

<script>
// ── i18n ──────────────────────────────────────────────────────────────────
const STRINGS = {
  zh: {
    navAll:'🌐 全部', navAI:'🤖 AI', navPapers:'📄 论文', navWeb3:'🔗 Web3',
    navVenture:'💼 创投', navStocks:'📈 美股', navPolymarket:'🎯 Polymarket', navGeopolitics:'🌍 地缘政治', navPosts:'📰 博客文章',
    navTweets:'𝕏 推文', navPodcasts:'🎙 播客', navTrump:'🇺🇸 特朗普',
    secCategories:'分类', secFilter:'筛选', secSources:'来源',
    statPosts:'文章', statTweets:'推文', statLatest:'最新',
    statVisitTotal:'总访问', statVisitToday:'今日', statVisitWeek:'本周', statVisitMonth:'本月',
    connecting:'连接中…', connected:'实时连接', reconnecting:'重连中…',
    searchPlaceholder:'搜索新闻…', newBadge:'↑ 有新内容',
    jokeTitle:'😂 今日笑话', jokeLoading:'正在生成…', jokeFail:'生成失败',
    digestTitle:'📋 资讯摘要', briefingTitle:'⚡ 每日要闻速报',
    trumpTitle:'🇺🇸 特朗普动向', trumpViewAll:'查看全部 →',
    refresh:'↻ 刷新', loadingDigest:'正在生成摘要…', loadingBriefing:'正在生成速报…',
    noData:'暂无数据', digestFail:'摘要生成失败', briefingFail:'速报生成失败',
    updatedAt: v => v + ' 更新',
    readMore:'阅读原文 →', viewTweet:'查看推文 →',
    noContent:'暂无内容，监控器正在抓取…', noCategory:'该分类暂无内容',
    loadFail:'加载失败', loadingPapers:'⏳ 正在加载论文…', loadingPolymarket:'⏳ 正在加载预测市场…', loading:'加载中…',
    noDigest:'暂无摘要', searchEmpty: q => '未找到 "' + q + '"',
    noPodcasts:'暂无跟踪的播客', podWebsite:'🌐 官网', podAlerts:'🔔 即时通知',
    recentEps:'最近更新', noEps:'暂无记录，等待下次抓取…',
    featured:'精选', polymarketTag:'🎯 预测市场', bnavPolymarket:'预测',
    bnavAll:'全部', bnavPapers:'论文', bnavVenture:'创投', bnavStocks:'美股', bnavMore:'更多',
    bnavTrump:'特朗普', bnavGeo:'地缘',
    navEarnings:'📅 财报日历',
  },
  en: {
    navAll:'🌐 All', navAI:'🤖 AI', navPapers:'📄 Papers', navWeb3:'🔗 Web3',
    navVenture:'💼 Venture', navStocks:'📈 US Stocks', navPolymarket:'🎯 Polymarket', navGeopolitics:'🌍 Geopolitics', navPosts:'📰 Posts',
    navTweets:'𝕏 Tweets', navPodcasts:'🎙 Podcasts', navTrump:'🇺🇸 Trump',
    secCategories:'Categories', secFilter:'Filter', secSources:'Sources',
    statPosts:'Posts', statTweets:'Tweets', statLatest:'Latest',
    statVisitTotal:'Total visits', statVisitToday:'Today', statVisitWeek:'This week', statVisitMonth:'This month',
    connecting:'Connecting…', connected:'Live', reconnecting:'Reconnecting…',
    searchPlaceholder:'Search news…', newBadge:'↑ New items',
    jokeTitle:'😂 Joke of the Day', jokeLoading:'Generating…', jokeFail:'Failed to generate',
    digestTitle:'📋 News Digest', briefingTitle:'⚡ Daily Briefing',
    trumpTitle:'🇺🇸 Trump Watch', trumpViewAll:'View all →',
    refresh:'↻ Refresh', loadingDigest:'Generating digest…', loadingBriefing:'Generating briefing…',
    noData:'No data', digestFail:'Digest failed', briefingFail:'Briefing failed',
    updatedAt: v => 'Updated ' + v,
    readMore:'Read more →', viewTweet:'View tweet →',
    noContent:'No content yet, fetching…', noCategory:'No items in this category',
    loadFail:'Load failed', loadingPapers:'⏳ Loading papers…', loadingPolymarket:'⏳ Loading prediction markets…', loading:'Loading…',
    noDigest:'No digest available', searchEmpty: q => 'No results for "' + q + '"',
    noPodcasts:'No podcasts tracked', podWebsite:'🌐 Website', podAlerts:'🔔 Alerts',
    recentEps:'Recent Episodes', noEps:'No episodes yet…',
    featured:'Featured', polymarketTag:'🎯 Prediction', bnavPolymarket:'Predict',
    bnavAll:'All', bnavPapers:'Papers', bnavVenture:'VC', bnavStocks:'Stocks', bnavMore:'More',
    bnavTrump:'Trump', bnavGeo:'Geo',
    navEarnings:'📅 Earnings Calendar',
  }
};
let lang = localStorage.getItem('lang') || 'zh';
function t(key, arg) { const v = STRINGS[lang][key]; return typeof v === 'function' ? v(arg) : (v || key); }
function toggleLang() { setLang(lang === 'zh' ? 'en' : 'zh'); }
function setLang(l) {
  lang = l;
  localStorage.setItem('lang', l);
  applyLang();
  // Server-fetched category views must be re-fetched; others re-render from allItems
  if (currentFilter === 'category:papers' || currentFilter === 'category:polymarket') {
    setCategory(currentFilter.slice(9), null);
  } else {
    renderFeed();
  }
  loadBriefing();
  loadDigest();
}
function applyLang() {
  const label = lang === 'zh' ? 'EN' : '中';
  document.getElementById('langBtn').textContent = label;
  document.getElementById('langPill').textContent = label;
  document.querySelectorAll('[data-i18n]').forEach(el => { el.textContent = t(el.dataset.i18n); });
  document.getElementById('searchInput').placeholder = t('searchPlaceholder');
  document.getElementById('newBadge').textContent = t('newBadge');
}
(function initLang() {
  const label = lang === 'zh' ? 'EN' : '中';
  document.getElementById('langBtn').textContent = label;
  document.getElementById('langPill').textContent = label;
  document.querySelectorAll('[data-i18n]').forEach(el => { el.textContent = t(el.dataset.i18n); });
  document.getElementById('searchInput').placeholder = t('searchPlaceholder');
  document.getElementById('newBadge').textContent = t('newBadge');
})();

let allItems = [], currentFilter = 'all', ws;

// ── Mobile Sidebar ────────────────────────────────────────────────────────
function toggleSidebar() {
  document.querySelector('.sidebar').classList.toggle('open');
  document.getElementById('overlay').classList.toggle('show');
}
function closeSidebar() {
  document.querySelector('.sidebar').classList.remove('open');
  document.getElementById('overlay').classList.remove('show');
}
function bnav(el) {
  document.querySelectorAll('.bnav-btn').forEach(b => b.classList.remove('active'));
  if (el) el.classList.add('active');
}



// ── Theme ──────────────────────────────────────────────────────────────────
function applyTheme(t) {
  document.documentElement.setAttribute('data-theme', t);
  document.getElementById('themeBtn').textContent = t === 'dark' ? '☀' : '🌙';
  localStorage.setItem('theme', t);
}
function toggleTheme() {
  const cur = document.documentElement.getAttribute('data-theme');
  const isDark = cur === 'dark' || (cur === 'auto' && window.matchMedia('(prefers-color-scheme: dark)').matches);
  applyTheme(isDark ? 'light' : 'dark');
}
(function initTheme() {
  const saved = localStorage.getItem('theme');
  if (saved) applyTheme(saved);
  else applyTheme('auto');
})();

// ── WebSocket ──────────────────────────────────────────────────────────────
function connectWS() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  ws = new WebSocket(`${proto}://${location.host}/ws`);
  ws.onopen = () => {
    document.getElementById('statusDot').style.background = 'var(--green)';
    document.getElementById('statusText').textContent = t('connected');
  };
  ws.onclose = () => {
    document.getElementById('statusDot').style.background = 'var(--red)';
    document.getElementById('statusText').textContent = t('reconnecting');
    setTimeout(connectWS, 3000);
  };
  ws.onmessage = (e) => {
    const msg = JSON.parse(e.data);
    const item = { type: msg.type, date: msg.data.created_at || msg.data.published || '', data: msg.data };
    allItems.unshift(item);
    updateCounts();
    if (currentFilter === 'all' || currentFilter === msg.type + 's') { prependCard(item, true); showNewBadge(); }
    updateStats();
  };
}

// ── Data ──────────────────────────────────────────────────────────────────
async function loadNews() {
  const res = await fetch('/api/news?limit=50');
  allItems = await res.json();
  renderFeed(); updateCounts(); updateStats();
}
async function updateStats() {
  const [s, v] = await Promise.all([
    fetch('/api/stats').then(r => r.json()),
    fetch('/api/visit-stats').then(r => r.json()),
  ]);
  document.getElementById('statPosts').textContent = s.post_count.toLocaleString();
  document.getElementById('statTweets').textContent = s.tweet_count.toLocaleString();
  const last = s.latest_post_at || s.latest_tweet_at;
  document.getElementById('statLast').textContent = last ? last.slice(0,16).replace('T',' ') : '—';
  document.getElementById('statVisitTotal').textContent = v.total.toLocaleString();
  document.getElementById('statVisitToday').textContent = v.today.toLocaleString();
  document.getElementById('statVisitWeek').textContent = v.this_week.toLocaleString();
  document.getElementById('statVisitMonth').textContent = v.this_month.toLocaleString();
}

// ── Render ────────────────────────────────────────────────────────────────
function renderFeed() {
  const feed = document.getElementById('cardFeed');
  const filtered = filterItems(allItems);
  if (!filtered.length) {
    feed.textContent = '';
    const empty = document.createElement('div');
    empty.className = 'empty';
    const icon = document.createElement('div');
    icon.className = 'empty-icon';
    icon.textContent = '📭';
    const msg = document.createElement('div');
    msg.textContent = t('noContent');
    empty.appendChild(icon); empty.appendChild(msg);
    feed.appendChild(empty);
    return;
  }
  feed.textContent = '';
  filtered.forEach(item => feed.appendChild(makeCard(item, false)));
  buildSourceList();
}

function prependCard(item, isNew) {
  const feed = document.getElementById('cardFeed');
  const emptyEl = feed.querySelector('.empty, .loading-text');
  if (emptyEl) emptyEl.remove();
  feed.insertBefore(makeCard(item, isNew), feed.firstChild);
}

function makeCard(item, isNew) {
  const d = item.data, isPost = item.type === 'post';
  const date = (d.created_at || d.published || '').slice(0,16).replace('T',' ');
  const div = document.createElement('div');
  div.className = 'card' + (isNew ? ' new-item' : '');

  // Meta row
  const meta = document.createElement('div');
  meta.className = 'card-meta';

  const srcTag = document.createElement('span');
  if (isPost) {
    srcTag.className = 'tag tag-post';
    srcTag.textContent = '📰 ' + d.source;
  } else {
    srcTag.className = 'tag tag-tweet';
    srcTag.textContent = '𝕏 @' + d.username;
  }
  meta.appendChild(srcTag);

  if (isPost && d.category === 'polymarket') {
    const pm = document.createElement('span');
    pm.className = 'tag tag-polymarket'; pm.textContent = t('polymarketTag');
    meta.appendChild(pm);
  } else if (isPost && d.feed_priority === 1) {
    const t1 = document.createElement('span');
    t1.className = 'tag tag-t1'; t1.textContent = t('featured');
    meta.appendChild(t1);
  }
  if (!isPost && d.category) {
    const cat = document.createElement('span');
    cat.className = 'tag tag-cat'; cat.textContent = d.category;
    meta.appendChild(cat);
  }
  // Paper-specific badges
  if (isPost && d.is_paper) {
    if (d.hf_upvotes && d.hf_upvotes > 0) {
      const uv = document.createElement('span');
      uv.className = 'tag tag-paper-uv';
      uv.textContent = '👍 ' + d.hf_upvotes;
      meta.appendChild(uv);
    }
    if (d.arxiv_id) {
      const ax = document.createElement('span');
      ax.className = 'tag tag-paper-arxiv';
      ax.textContent = 'arXiv:' + d.arxiv_id;
      meta.appendChild(ax);
    }
    // 大模型公司标签 — 检查 source/title/authors 拼接
    const haystack = ((d.source||'') + ' ' + (d.title||'') + ' ' + (d.authors||'')).toLowerCase();
    const COMPANY_LABELS = [
      ['deepseek','DeepSeek'], ['qwen','Qwen'], ['alibaba','Alibaba'],
      ['bytedance','字节跳动'], ['doubao','字节豆包'], ['moonshot','Moonshot'],
      ['kimi','Kimi'], ['zhipu','智谱'], ['thudm','智谱'], ['glm','智谱 GLM'],
      ['01-ai','01.AI'], ['01.ai','01.AI'], ['baichuan','百川'],
      ['openai','OpenAI'], ['anthropic','Anthropic'], ['deepmind','DeepMind'],
      ['meta ai','Meta AI'], ['fair','Meta FAIR'], ['mistral','Mistral'], ['xai','xAI'],
    ];
    for (const [kw, label] of COMPANY_LABELS) {
      if (haystack.includes(kw)) {
        const co = document.createElement('span');
        co.className = 'tag tag-paper-co';
        co.textContent = '🏢 ' + label;
        meta.appendChild(co);
        break;
      }
    }
  }

  const dateEl = document.createElement('span');
  dateEl.className = 'card-date'; dateEl.textContent = date;
  meta.appendChild(dateEl);
  div.appendChild(meta);

  if (isPost) {
    const titleEl = document.createElement('div');
    titleEl.className = 'card-title';
    const a = document.createElement('a');
    a.href = d.url; a.target = '_blank'; a.textContent = d.title;
    titleEl.appendChild(a);
    div.appendChild(titleEl);

    if (lang === 'zh' && d.title_zh) {
      const zh = document.createElement('div');
      zh.className = 'card-title-zh'; zh.textContent = d.title_zh;
      div.appendChild(zh);
    }
    const normZh = s => s.trim().replace(/[\s\-–—]/g, '');
    if (lang === 'zh' && d.summary_zh && normZh(d.summary_zh) !== normZh(d.title_zh||'')) {
      const sz = document.createElement('div');
      sz.className = 'card-summary-zh'; sz.textContent = d.summary_zh;
      div.appendChild(sz);
    }
    if (d.summary) {
      const se = document.createElement('div');
      se.className = 'card-summary-en'; se.textContent = d.summary;
      div.appendChild(se);
    }
    const footer = document.createElement('div');
    footer.className = 'card-footer';
    const link = document.createElement('a');
    link.className = 'card-link'; link.href = d.url; link.target = '_blank'; link.textContent = t('readMore');
    footer.appendChild(link);
    div.appendChild(footer);

    // Paper-only footer with authors + PDF link
    if (d.is_paper && (d.authors || d.pdf_url)) {
      const pf = document.createElement('div');
      pf.className = 'card-paper-footer';
      if (d.authors) {
        try {
          const arr = JSON.parse(d.authors);
          if (Array.isArray(arr) && arr.length) {
            const span = document.createElement('span');
            span.className = 'authors';
            const head = arr.slice(0, 3).join(', ');
            span.textContent = '👥 ' + head + (arr.length > 3 ? ` +${arr.length - 3}` : '');
            pf.appendChild(span);
          }
        } catch (e) { /* malformed authors json — skip */ }
      }
      if (d.pdf_url) {
        const a = document.createElement('a');
        a.className = 'pdf-link'; a.href = d.pdf_url; a.target = '_blank';
        a.textContent = 'PDF ↓';
        pf.appendChild(a);
      }
      div.appendChild(pf);
    }
  } else {
    const txt = document.createElement('div');
    txt.className = 'tweet-text'; txt.textContent = d.text;
    div.appendChild(txt);
    if (lang === 'zh' && d.text_zh) {
      const tzh = document.createElement('div');
      tzh.className = 'tweet-text-zh'; tzh.textContent = d.text_zh;
      div.appendChild(tzh);
    }
    const footer = document.createElement('div');
    footer.className = 'card-footer';
    const link = document.createElement('a');
    link.className = 'card-link'; link.href = d.url || '#'; link.target = '_blank'; link.textContent = t('viewTweet');
    footer.appendChild(link);
    const eng = document.createElement('span');
    eng.className = 'card-eng';
    eng.textContent = '❤ ' + (d.likes||0) + '  🔁 ' + (d.retweets||0);
    footer.appendChild(eng);
    div.appendChild(footer);
  }
  return div;
}

// ── Filters ───────────────────────────────────────────────────────────────
function filterItems(items) {
  if (currentFilter === 'posts')  return items.filter(i => i.type === 'post');
  if (currentFilter === 'tweets') return items.filter(i => i.type === 'tweet');
  if (currentFilter.startsWith('category:')) {
    const cat = currentFilter.slice(9);
    return items.filter(i => i.data.category === cat);
  }
  return items;
}
function showPanels(show) {
  document.querySelector('.briefing-panel').style.display = show ? '' : 'none';
  document.querySelector('.digest-panel').style.display = show ? '' : 'none';

}
function setCategoryMobile(cat, el) {
  showPanels(false);
  setCategory(cat, null);
  bnav(el);
}
function setFilter(f, btn) {
  currentFilter = f;
  document.querySelectorAll('.nav-btn').forEach(b => b.classList.remove('active'));
  if (btn) btn.classList.add('active');
  showPanels(f === 'all');
  renderFeed();
  closeSidebar();
}
function updateCounts() {
  document.getElementById('cnt-all').textContent    = allItems.length;
  document.getElementById('cnt-posts').textContent  = allItems.filter(i=>i.type==='post').length;
  document.getElementById('cnt-tweets').textContent = allItems.filter(i=>i.type==='tweet').length;
  ['trump','ai','papers','web3','venture','us_stock','polymarket','geopolitics'].forEach(cat => {
    const el = document.getElementById('cnt-' + cat);
    if (el) el.textContent = allItems.filter(i=>i.data.category===cat).length;
  });
}

async function setCategory(cat, btn) {
  currentFilter = 'category:' + cat;
  document.querySelectorAll('.nav-btn').forEach(b => b.classList.remove('active'));
  if (btn) btn.classList.add('active');
  showPanels(false);
  const feed = document.getElementById('cardFeed');
  feed.textContent = '';

  // Trump / Papers / Polymarket / Venture / Web3 / US Stock: fetch from server.
  if (cat === 'trump' || cat === 'papers' || cat === 'polymarket' || cat === 'venture' || cat === 'web3' || cat === 'us_stock' || cat === 'geopolitics') {
    const loadMsg = cat === 'papers' ? t('loadingPapers') : cat === 'polymarket' ? t('loadingPolymarket') : t('loading');
    feed.appendChild(makeEmptyEl('⏳', loadMsg));
    try {
      const res = await fetch('/api/news?category=' + cat + '&limit=50');
      const items = await res.json();
      feed.textContent = '';
      if (!items.length) { feed.appendChild(makeEmptyEl('📭', t('noCategory'))); return; }
      items.forEach(item => feed.appendChild(makeCard(item, false)));
    } catch (e) {
      feed.textContent = '';
      feed.appendChild(makeEmptyEl('⚠', t('loadFail')));
    }
    return;
  }

  const filtered = allItems.filter(i => i.data.category === cat);
  if (!filtered.length) {
    feed.appendChild(makeEmptyEl('📭', t('noCategory')));
    return;
  }
  filtered.forEach(item => feed.appendChild(makeCard(item, false)));
  closeSidebar();
}
function buildSourceList() {
  const counts = {};
  allItems.filter(i=>i.type==='post').forEach(i => { counts[i.data.source] = (counts[i.data.source]||0)+1; });
  const top = Object.entries(counts).sort((a,b)=>b[1]-a[1]).slice(0,6);
  const el = document.getElementById('sourceList');
  el.textContent = '';
  top.forEach(([src, cnt]) => {
    const btn = document.createElement('button');
    btn.className = 'nav-btn';
    btn.style.fontSize = '12px';
    btn.onclick = () => filterSource(src, btn);
    btn.textContent = src;
    const badge = document.createElement('span');
    badge.className = 'cnt'; badge.textContent = cnt;
    btn.appendChild(badge);
    el.appendChild(btn);
  });
}
function filterSource(src, btn) {
  document.querySelectorAll('.nav-btn').forEach(b => b.classList.remove('active'));
  if (btn) btn.classList.add('active');
  const feed = document.getElementById('cardFeed');
  feed.textContent = '';
  allItems.filter(i => i.data.source === src).forEach(item => feed.appendChild(makeCard(item, false)));
  closeSidebar();
}

// ── Trump Watch ───────────────────────────────────────────────────────────
async function loadTrumpWatch() {
  const body = document.getElementById('trumpBody');
  body.innerHTML = '<span class="panel-loading">' + t('loading') + '</span>';
  try {
    const items = await fetch('/api/news?category=trump&limit=7').then(r => r.json());
    body.textContent = '';
    if (!items.length) {
      body.textContent = t('noData');
      return;
    }
    const list = document.createElement('div');
    list.className = 'trump-list';
    items.forEach(item => {
      const d = item.data;
      const row = document.createElement('div');
      row.className = 'trump-item';

      const meta = document.createElement('div');
      meta.className = 'trump-item-meta';
      const src = document.createElement('span');
      src.className = 'trump-src';
      src.textContent = item.type === 'tweet' ? '𝕏 @' + d.username : d.source;
      const dateEl = document.createElement('span');
      dateEl.className = 'trump-date';
      dateEl.textContent = (d.created_at || d.published || '').slice(0, 16).replace('T', ' ');
      meta.appendChild(src); meta.appendChild(dateEl);

      const titleEl = document.createElement('div');
      titleEl.className = 'trump-title';
      const a = document.createElement('a');
      a.href = d.url || '#'; a.target = '_blank'; a.rel = 'noopener';
      a.textContent = item.type === 'tweet' ? d.text : d.title;
      titleEl.appendChild(a);

      row.appendChild(meta); row.appendChild(titleEl);
      list.appendChild(row);
    });
    body.appendChild(list);
  } catch(e) { body.textContent = t('loadFail'); }
}

// ── Briefing ──────────────────────────────────────────────────────────────
async function loadBriefing() {
  const body = document.getElementById('briefingBody');
  const meta = document.getElementById('briefingMeta');
  body.textContent = t('loadingBriefing');
  try {
    const data = await fetch('/api/briefing?lang=' + lang).then(r => r.json());
    const sections = data.sections || [];
    if (!sections.length) { body.textContent = t('noData'); return; }

    const grid = document.createElement('div');
    grid.className = 'briefing-grid';
    sections.forEach(s => {
      const sec = document.createElement('div'); sec.className = 'b-section';
      const title = document.createElement('div'); title.className = 'b-title';
      if (s.category === 'trump') {
        const img = document.createElement('img');
        img.src = 'https://upload.wikimedia.org/wikipedia/commons/thumb/5/56/Donald_Trump_official_portrait.jpg/40px-Donald_Trump_official_portrait.jpg';
        img.className = 'trump-avatar';
        img.onerror = function() { this.replaceWith(document.createTextNode('🇺🇸')); };
        title.appendChild(img);
        title.appendChild(document.createTextNode(' ' + (s.label||s.category)));
      } else {
        title.textContent = (s.icon||'') + ' ' + (s.label||s.category);
      }
      sec.appendChild(title);
      const ul = document.createElement('ul'); ul.className = 'b-list';
      (s.points||[]).forEach(p => {
        const li = document.createElement('li');
        const text = (typeof p === 'object') ? (p.text||'') : p;
        const url  = (typeof p === 'object') ? (p.url||'')  : '';
        if (url) {
          const a = document.createElement('a');
          a.href = url; a.target = '_blank'; a.rel = 'noopener';
          a.textContent = text;
          li.appendChild(a);
        } else {
          li.textContent = text;
        }
        ul.appendChild(li);
      });
      sec.appendChild(ul); grid.appendChild(sec);
    });
    body.textContent = '';
    body.appendChild(grid);
    const locale = lang === 'zh' ? 'zh-CN' : 'en-US';
    meta.textContent = t('updatedAt', new Date().toLocaleTimeString(locale,{hour:'2-digit',minute:'2-digit'}));
  } catch(e) { body.textContent = t('briefingFail'); }
}

// ── Digest ────────────────────────────────────────────────────────────────
async function loadJoke(refresh = false) {
  const el = document.getElementById('jokeBody');
  el.innerHTML = '<span class="panel-loading">' + t('jokeLoading') + '</span>';
  try {
    const url = '/api/joke' + (refresh ? '?refresh=true' : '');
    const data = await fetch(url).then(r => r.json());
    const jokes = Array.isArray(data.jokes) ? data.jokes : [];
    if (!jokes.length) { el.textContent = '今日新闻太无聊，段子写不出来。'; return; }
    el.textContent = '';
    jokes.forEach(j => {
      const div = document.createElement('div');
      div.className = 'joke-item';
      div.textContent = j;
      el.appendChild(div);
    });
  } catch(e) { el.textContent = t('jokeFail'); }
}

async function loadDigest() {
  const el = document.getElementById('digestText');
  el.textContent = t('loadingDigest');
  try {
    const data = await fetch('/api/digest-summary?lang=' + lang).then(r => r.json());
    const bullets = Array.isArray(data.summary) ? data.summary : [];
    if (!bullets.length) { el.textContent = t('noDigest'); return; }
    el.textContent = '';
    const ul = document.createElement('ul');
    ul.style.margin = '0'; ul.style.paddingLeft = '20px';
    bullets.forEach(b => {
      const li = document.createElement('li');
      li.style.margin = '6px 0'; li.style.lineHeight = '1.6';
      const text = (typeof b === 'object') ? (b.text||'') : b;
      const url  = (typeof b === 'object') ? (b.url||'')  : '';
      if (url) {
        const a = document.createElement('a');
        a.href = url; a.target = '_blank'; a.rel = 'noopener';
        a.textContent = text;
        li.appendChild(a);
      } else {
        li.textContent = text;
      }
      ul.appendChild(li);
    });
    el.appendChild(ul);
  } catch(e) { el.textContent = t('digestFail'); }
}

// ── Search ────────────────────────────────────────────────────────────────
let searchTimer;
async function onSearch(q) {
  clearTimeout(searchTimer);
  if (!q.trim()) { renderFeed(); return; }
  searchTimer = setTimeout(async () => {
    const results = await fetch(`/api/search?q=${encodeURIComponent(q)}&limit=30`).then(r=>r.json());
    const feed = document.getElementById('cardFeed');
    feed.textContent = '';
    if (!results.length) {
      const empty = document.createElement('div'); empty.className = 'empty';
      const icon = document.createElement('div'); icon.className = 'empty-icon'; icon.textContent = '🔍';
      const msg = document.createElement('div'); msg.textContent = t('searchEmpty', q);
      empty.appendChild(icon); empty.appendChild(msg); feed.appendChild(empty);
      return;
    }
    results.forEach(r => {
      const isPost = !r.text;
      feed.appendChild(makeCard({ type: isPost?'post':'tweet', date: r.created_at||r.published||'', data: r }, false));
    });
  }, 300);
}

// ── Podcasts ──────────────────────────────────────────────────────────────
function makeEmptyEl(icon, msg) {
  const el = document.createElement('div'); el.className = 'empty';
  const ic = document.createElement('div'); ic.className = 'empty-icon'; ic.textContent = icon;
  const tx = document.createElement('div'); tx.textContent = msg;
  el.appendChild(ic); el.appendChild(tx);
  return el;
}

async function showPodcasts(btn) {
  document.querySelectorAll('.nav-btn').forEach(b => b.classList.remove('active'));
  if (btn) btn.classList.add('active');
  currentFilter = 'podcasts';
  closeSidebar();

  const feed = document.getElementById('cardFeed');
  feed.textContent = '';
  const loading = document.createElement('div');
  loading.className = 'loading-text'; loading.textContent = t('loading');
  feed.appendChild(loading);

  try {
    const podcasts = await fetch('/api/podcasts').then(r => r.json());
    feed.textContent = '';
    if (!podcasts.length) { feed.appendChild(makeEmptyEl('🎙', t('noPodcasts'))); return; }

    podcasts.forEach(p => {
      const card = document.createElement('div');
      card.className = 'podcast-card';

      const header = document.createElement('div'); header.className = 'podcast-header';
      const icon = document.createElement('div'); icon.className = 'podcast-icon'; icon.textContent = '🎙';
      header.appendChild(icon);

      const info = document.createElement('div'); info.className = 'podcast-info';
      const name = document.createElement('div'); name.className = 'podcast-name'; name.textContent = p.name;
      info.appendChild(name);

      const links = document.createElement('div'); links.className = 'podcast-links';

      if (p.site) {
        const a = document.createElement('a');
        a.className = 'pod-link'; a.href = p.site; a.target = '_blank'; a.textContent = t('podWebsite');
        links.appendChild(a);
      }
      const rss = document.createElement('a');
      rss.className = 'pod-link'; rss.href = p.feed; rss.target = '_blank'; rss.textContent = '📡 RSS';
      links.appendChild(rss);

      if (p.alert) {
        const badge = document.createElement('span');
        badge.className = 'pod-link alert-badge'; badge.textContent = t('podAlerts');
        links.appendChild(badge);
      }
      info.appendChild(links);
      header.appendChild(info);
      card.appendChild(header);

      const eps = document.createElement('div'); eps.className = 'podcast-episodes';
      const label = document.createElement('div'); label.className = 'ep-label'; label.textContent = t('recentEps');
      eps.appendChild(label);

      if (!p.recent || !p.recent.length) {
        const empty = document.createElement('div');
        empty.className = 'ep-empty'; empty.textContent = t('noEps');
        eps.appendChild(empty);
      } else {
        p.recent.forEach(ep => {
          const row = document.createElement('div'); row.className = 'ep-item';
          const date = document.createElement('div'); date.className = 'ep-date';
          date.textContent = (ep.published || '').slice(0, 10);
          row.appendChild(date);
          const title = document.createElement('div'); title.className = 'ep-title';
          const a = document.createElement('a'); a.href = ep.url; a.target = '_blank'; a.textContent = ep.title;
          title.appendChild(a); row.appendChild(title);
          eps.appendChild(row);
        });
      }
      card.appendChild(eps);
      feed.appendChild(card);
    });
  } catch(e) {
    feed.textContent = '';
    feed.appendChild(makeEmptyEl('⚠️', t('loadFail')));
  }
}

// ── Helpers ───────────────────────────────────────────────────────────────
function showNewBadge() {
  const b = document.getElementById('newBadge'); b.classList.add('show');
  setTimeout(() => b.classList.remove('show'), 5000);
}
function scrollToTop() {
  document.getElementById('feed').scrollTo({top:0,behavior:'smooth'});
  document.getElementById('newBadge').classList.remove('show');
}

// ── Markets ───────────────────────────────────────────────────────────────
// Yahoo (/api/markets) supplies prev-close baseline (one call per refresh) for change%.
// Pyth Hermes SSE streams sub-second ticks for live price; change/pct is recomputed
// against Yahoo's prev-close so the displayed delta stays day-over-day.
// Note: gold/silver baselines use Yahoo futures (GC=F/SI=F) while Pyth streams XAU/XAG spot —
// the futures premium (~0.5-1.5%) is rolled into the displayed change.
const MARKET_ID_MAP = {
  // Yahoo symbol -> tile id
  'QQQ':      'QQQ',
  'SPY':      'SPY',
  'DIA':      'DIA',
  'BTC-USD':  'BTC',
  'GC=F':     'XAU',
  'SI=F':     'XAG',
  'CL=F':     'WTI',
  'DX-Y.NYB': 'DXY',
};
const PYTH_FEEDS = {
  // tile id -> Pyth feed id (no 0x prefix)
  'QQQ': '9695e2b96ea7b3859da9ed25b7a46a920a776e2fdae19a7bcfdf2b219230452d',
  'SPY': '19e09bb805456ada3979a7d1cbb4b6d63babc3a0f8e8a9509f68afa5c4c11cd5',
  'DIA': '57cff3a9a4d4c87b595a2d1bd1bac0240400a84677366d632ab838bbbe56f763',
  'BTC': 'e62df6c8b4a85fe1a67db44dc12de5db330f7ac66b72dc658afedf0f4a415b43',
  'XAU': '765d2ba906dbc32ca17cc11f5310a89e9ee1f6420508c63861f2f8ba4ee34bb2',
  'XAG': 'f2fb02c32b055c805e7238d628e5e9dadef274376114eb1f012337cabe93871e',
  'WTI': '925ca92ff005ae943c158e3563f59698ce7e75c5a8c8dd43303a0a154887b3e6',
  'DXY': '710afe0041a07156bfd71971160c78a326bf8121403e0d4e140d06bea0353b7f',
};
const PYTH_ID_TO_TILE = Object.fromEntries(Object.entries(PYTH_FEEDS).map(([k,v]) => [v, k]));
const marketBaseline = {};  // tile id -> { prevClose }
let marketUpdatedEl = null;

function renderTile(tileId, price, prevClose) {
  const tile = document.getElementById('mkt-' + tileId);
  if (!tile) return;
  const [, priceEl, changeEl] = tile.children;
  const isBTC = tileId === 'BTC';
  priceEl.textContent = isBTC
    ? price.toLocaleString('en-US', {maximumFractionDigits: 0})
    : price.toLocaleString('en-US', {minimumFractionDigits: 2, maximumFractionDigits: 2});
  if (prevClose != null && prevClose !== 0) {
    const change = price - prevClose;
    const pct = change / prevClose * 100;
    const up = change >= 0;
    const sign = up ? '+' : '';
    const chAbs = Math.abs(change).toLocaleString('en-US', {minimumFractionDigits: 2, maximumFractionDigits: 2});
    const chPct = sign + pct.toFixed(2) + '%';
    changeEl.textContent = sign + chAbs + ' (' + chPct + ')';
    changeEl.className = 'market-tile-change ' + (up ? 'market-up' : 'market-down');
  }
  if (!marketUpdatedEl) marketUpdatedEl = document.getElementById('marketUpdated');
  if (marketUpdatedEl) {
    const now = new Date();
    marketUpdatedEl.textContent = now.toLocaleTimeString(lang === 'zh' ? 'zh-CN' : 'en-US', {hour: '2-digit', minute: '2-digit', second: '2-digit'});
  }
}

async function loadMarkets() {
  try {
    const data = await fetch('/api/markets').then(r => r.json());
    data.forEach(m => {
      const id = MARKET_ID_MAP[m.symbol];
      if (!id || m.price == null) return;
      const prevClose = (m.change != null) ? (m.price - m.change) : null;
      if (prevClose != null) marketBaseline[id] = { prevClose };
      renderTile(id, m.price, prevClose);
    });
  } catch(e) { /* silently ignore */ }
}

let pythSource = null;
let pythReconnectTimer = null;
function connectPyth() {
  if (pythSource) { try { pythSource.close(); } catch(e) {} pythSource = null; }
  const ids = Object.values(PYTH_FEEDS).map(id => 'ids[]=' + id).join('&');
  const url = 'https://hermes.pyth.network/v2/updates/price/stream?' + ids;
  const es = new EventSource(url);
  pythSource = es;
  es.onmessage = (ev) => {
    try {
      const msg = JSON.parse(ev.data);
      if (!msg.parsed) return;
      msg.parsed.forEach(p => {
        const tileId = PYTH_ID_TO_TILE[p.id];
        if (!tileId) return;
        const expo = p.price.expo;
        const price = Number(p.price.price) * Math.pow(10, expo);
        if (!isFinite(price) || price <= 0) return;
        const base = marketBaseline[tileId];
        renderTile(tileId, price, base ? base.prevClose : null);
      });
    } catch(e) { /* ignore parse errors */ }
  };
  es.onerror = () => {
    try { es.close(); } catch(e) {}
    pythSource = null;
    if (pythReconnectTimer) clearTimeout(pythReconnectTimer);
    pythReconnectTimer = setTimeout(connectPyth, 5000);
  };
}

// ── Init ──────────────────────────────────────────────────────────────────
connectWS(); loadBriefing(); loadDigest(); loadJoke();
setInterval(updateStats, 60000);
setInterval(loadBriefing, 30 * 60 * 1000);
loadMarkets();
setInterval(loadMarkets, 30000);  // Yahoo refresh: day-over-day prev-close baseline
connectPyth();                     // Pyth SSE: sub-second live ticks for all 8 instruments
loadNews();
</script>
</body>
</html>"""


_ACCOUNT_PILL_STYLE = (
    "display:flex;align-items:center;gap:6px;height:36px;padding:0 14px;"
    "border-radius:20px;background:var(--surface2);color:var(--text);"
    "text-decoration:none;font-size:13px;font-weight:600;flex-shrink:0;"
    "border:1px solid var(--border);transition:background .15s;"
)


def _account_pill_html(sub) -> str:
    """Build the topbar pill that links to /account (or /login if logged out)."""
    if sub is None:
        return (
            f"<a href='/login' style='{_ACCOUNT_PILL_STYLE}' "
            f"title='登录订阅账号'>👤 <span>登录</span></a>"
        )
    import html as _h
    label = sub.name or sub.email.split("@")[0]
    badge = "★" if subscribers.is_paid(sub) else ""
    badge_html = f' <span style="color:#ca8a04">{badge}</span>' if badge else ''
    return (
        f"<a href='/account' style='{_ACCOUNT_PILL_STYLE}' "
        f"title='查看账号'>👤 <span>{_h.escape(label)}</span>"
        f"{badge_html}</a>"
    )


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    storage.record_visit()
    sub = auth.current_subscriber(request)
    html = DASHBOARD_HTML.replace("<!--ACCOUNT_PILL-->", _account_pill_html(sub))
    return HTMLResponse(content=html)


# ── Earnings calendar sub-page ─────────────────────────────────────────────

@app.get("/api/earnings-calendar")
def api_earnings_calendar(
    start: str,
    end: str,
    min_cap_m: int = None,
    industries: str = None,
    watchlist: str = None,
    include_earnings: int = 1,
    include_ipos: int = 1,
    include_macro: int = 1,
    sub=Depends(auth.require_subscriber),
):
    """Return {date: {earnings, ipos, macro}} for [start, end] (YYYY-MM-DD)."""
    if min_cap_m is None:
        min_cap_m = config.EARNINGS_DEFAULT_MIN_CAP_M
    industries_list = (
        [s.strip() for s in industries.split(",") if s.strip()]
        if industries is not None else config.EARNINGS_INDUSTRIES_DEFAULT
    )
    watchlist_list = (
        [s.strip().upper() for s in watchlist.split(",") if s.strip()]
        if watchlist is not None else config.EARNINGS_WATCHLIST
    )
    return storage.get_calendar_window(
        start, end,
        {
            "min_market_cap_m": min_cap_m,
            "industries": industries_list,
            "watchlist": watchlist_list,
            "include_earnings": bool(include_earnings),
            "include_ipos": bool(include_ipos),
            "include_macro": bool(include_macro),
        },
    )


# ── .ics export for earnings calendar ─────────────────────────────────────
#
# Strategy: reuse storage.get_calendar_window() for filtering, then render a
# single VCALENDAR with one VEVENT per earnings/IPO/macro entry. The calendar
# is timezone-aware (America/New_York) since BMO/AMC times are inherently ET.

from datetime import datetime, date as _date, time as _time, timedelta
from zoneinfo import ZoneInfo

_ET = ZoneInfo("America/New_York")
_ICS_PRODID = "-//YunFlow//Earnings Calendar//EN"


def _ics_escape(s: str) -> str:
    """Escape per RFC 5545 §3.3.11: backslash, semicolon, comma, newline."""
    if not s:
        return ""
    return (s.replace("\\", "\\\\")
             .replace(";", "\\;")
             .replace(",", "\\,")
             .replace("\r\n", "\\n")
             .replace("\n", "\\n"))


def _ics_fold(line: str) -> str:
    """Fold lines >75 octets per RFC 5545 §3.1 (CRLF + space continuation)."""
    if len(line.encode("utf-8")) <= 75:
        return line
    out, buf, size = [], [], 0
    for ch in line:
        b = len(ch.encode("utf-8"))
        if size + b > 73:  # leave room for the leading space on continuation
            out.append("".join(buf))
            buf, size = [" ", ch], 1 + b
        else:
            buf.append(ch); size += b
    out.append("".join(buf))
    return "\r\n".join(out)


def _ics_dt_utc(dt: datetime) -> str:
    """Format a datetime as UTC: 20260515T123000Z."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_ET)
    return dt.astimezone(ZoneInfo("UTC")).strftime("%Y%m%dT%H%M%SZ")


def _ics_date(d: _date) -> str:
    """Format as DATE (all-day): 20260515."""
    return d.strftime("%Y%m%d")


def _parse_ymd(s: str) -> _date:
    return datetime.strptime(s, "%Y-%m-%d").date()


def _earnings_event_times(date_str: str, hour: str | None) -> tuple[datetime, datetime] | _date:
    """Return either (start_dt, end_dt) in ET — for a timed VEVENT — or a
    `date` object — for an all-day VEVENT.

    The Finnhub `hour` field is one of: "bmo" (before market open),
    "amc" (after market close), "dmh" (during market hours), or None.

    TODO(user): implement this. See the request in the chat for trade-offs
    to consider. Default below picks sensible times; replace as needed.
    """
    d = _parse_ymd(date_str)
    if hour == "bmo":
        # Most BMO calls run ~8:00–8:30 AM ET; 60 min is a safe window.
        start = datetime.combine(d, _time(8, 0), tzinfo=_ET)
        return start, start + timedelta(minutes=60)
    if hour == "amc":
        # AMC calls usually 4:30–5:30 PM ET.
        start = datetime.combine(d, _time(16, 30), tzinfo=_ET)
        return start, start + timedelta(minutes=60)
    if hour == "dmh":
        start = datetime.combine(d, _time(12, 0), tzinfo=_ET)
        return start, start + timedelta(minutes=60)
    # Unknown hour → all-day event so it stays visible without a wrong time.
    return d


def _macro_event_times(date_str: str, time_str: str | None) -> tuple[datetime, datetime] | _date:
    """Macro event timing. `time_str` is "HH:MM" ET if present, else None."""
    d = _parse_ymd(date_str)
    if time_str:
        try:
            hh, mm = time_str.split(":")
            start = datetime.combine(d, _time(int(hh), int(mm)), tzinfo=_ET)
            return start, start + timedelta(minutes=30)
        except (ValueError, TypeError):
            pass
    return d  # all-day


def _fmt_rev_short(v) -> str:
    if v is None:
        return ""
    try:
        n = float(v)
    except (TypeError, ValueError):
        return ""
    if n >= 1e9:
        return f"${n/1e9:.1f}B"
    if n >= 1e6:
        return f"${n/1e6:.0f}M"
    return f"${n:.0f}"


def _vevent(uid: str, when, summary: str, description: str = "", url: str = "") -> list[str]:
    """Build one VEVENT block. `when` is either a `date` (all-day) or a
    (start_dt, end_dt) tuple of aware datetimes."""
    dtstamp = datetime.now(tz=ZoneInfo("UTC")).strftime("%Y%m%dT%H%M%SZ")
    lines = ["BEGIN:VEVENT", f"UID:{uid}", f"DTSTAMP:{dtstamp}"]
    if isinstance(when, tuple):
        s, e = when
        lines.append(f"DTSTART:{_ics_dt_utc(s)}")
        lines.append(f"DTEND:{_ics_dt_utc(e)}")
    else:
        s = when
        lines.append(f"DTSTART;VALUE=DATE:{_ics_date(s)}")
        lines.append(f"DTEND;VALUE=DATE:{_ics_date(s + timedelta(days=1))}")
    lines.append(f"SUMMARY:{_ics_escape(summary)}")
    if description:
        lines.append(f"DESCRIPTION:{_ics_escape(description)}")
    if url:
        lines.append(f"URL:{_ics_escape(url)}")
    lines.append("END:VEVENT")
    return [_ics_fold(ln) for ln in lines]


def _render_calendar_ics(window: dict, cal_name: str = "Earnings Calendar") -> str:
    """Transform the storage.get_calendar_window() output into an .ics body."""
    out = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        f"PRODID:{_ICS_PRODID}",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{_ics_escape(cal_name)}",
        "X-WR-TIMEZONE:America/New_York",
        f"NAME:{_ics_escape(cal_name)}",
        "REFRESH-INTERVAL;VALUE=DURATION:PT6H",
        "X-PUBLISHED-TTL:PT6H",
    ]

    for date_str in sorted(window.keys()):
        bucket = window[date_str] or {}

        for e in bucket.get("earnings") or []:
            sym = (e.get("symbol") or "").upper()
            name = e.get("name") or sym
            hour = (e.get("hour") or "").lower() or None
            uid = f"earn-{sym}-{date_str}@yunflow"
            tag = {"bmo": "BMO", "amc": "AMC", "dmh": "DMH"}.get(hour or "", "")
            summary = f"📊 {sym} 财报" + (f" ({tag})" if tag else "")
            desc_parts = [name]
            if e.get("eps_estimate") is not None:
                try:
                    desc_parts.append(f"EPS Est: ${float(e['eps_estimate']):.2f}")
                except (TypeError, ValueError):
                    pass
            rev = _fmt_rev_short(e.get("rev_estimate"))
            if rev:
                desc_parts.append(f"Rev Est: {rev}")
            if e.get("industry"):
                desc_parts.append(str(e["industry"]))
            description = " · ".join(p for p in desc_parts if p)
            url = e.get("weburl") or f"https://finance.yahoo.com/quote/{sym}"
            out.extend(_vevent(uid, _earnings_event_times(date_str, hour),
                               summary, description, url))

        for ipo in bucket.get("ipos") or []:
            name = ipo.get("name") or ipo.get("symbol") or "IPO"
            sym = (ipo.get("symbol") or "").upper()
            slug = sym or name.replace(" ", "_")[:24]
            uid = f"ipo-{slug}-{date_str}@yunflow"
            summary = f"🚀 IPO: {name}" + (f" ({sym})" if sym else "")
            desc_parts = []
            if ipo.get("exchange"):
                desc_parts.append(f"Exchange: {ipo['exchange']}")
            if ipo.get("price_range"):
                desc_parts.append(f"Price: ${ipo['price_range']}")
            if ipo.get("shares"):
                try:
                    desc_parts.append(f"Shares: {int(ipo['shares']):,}")
                except (TypeError, ValueError):
                    pass
            if ipo.get("status"):
                desc_parts.append(f"Status: {ipo['status']}")
            description = " · ".join(desc_parts)
            # IPOs are all-day events (no specific intraday time).
            out.extend(_vevent(uid, _parse_ymd(date_str), summary, description))

        for m in bucket.get("macro") or []:
            title = m.get("title") or "Macro Event"
            cat = (m.get("category") or "macro").lower()
            slug = "".join(c if c.isalnum() else "_" for c in title)[:40]
            uid = f"macro-{cat}-{slug}-{date_str}@yunflow"
            impact = (m.get("impact") or "").upper()
            summary = f"🏛 {title}" + (f" [{impact}]" if impact else "")
            desc_parts = []
            if m.get("category"):
                desc_parts.append(str(m["category"]).upper())
            if m.get("notes"):
                desc_parts.append(str(m["notes"]))
            description = "\n".join(desc_parts)
            out.extend(_vevent(uid, _macro_event_times(date_str, m.get("time")),
                               summary, description))

    out.append("END:VCALENDAR")
    return "\r\n".join(out) + "\r\n"


@app.get("/api/earnings-calendar.ics")
def api_earnings_calendar_ics(
    start: str = None,
    end: str = None,
    months: int = 3,
    min_cap_m: int = None,
    industries: str = None,
    watchlist: str = None,
    include_earnings: int = 1,
    include_ipos: int = 1,
    include_macro: int = 1,
    download: int = 0,
):
    """Serve the calendar as an RFC 5545 .ics file.

    Designed for both one-time download (?download=1) and live subscription
    (Google Calendar / Apple Calendar will poll this URL).

    If start/end are omitted, a rolling [today, today + `months` months]
    window is used — sensible default for subscribed calendars.
    """
    today = datetime.now(tz=_ET).date()
    if not start:
        start = today.strftime("%Y-%m-%d")
    if not end:
        end_date = today + timedelta(days=30 * max(1, min(months, 12)))
        end = end_date.strftime("%Y-%m-%d")

    if min_cap_m is None:
        min_cap_m = config.EARNINGS_DEFAULT_MIN_CAP_M
    industries_list = (
        [s.strip() for s in industries.split(",") if s.strip()]
        if industries is not None else config.EARNINGS_INDUSTRIES_DEFAULT
    )
    watchlist_list = (
        [s.strip().upper() for s in watchlist.split(",") if s.strip()]
        if watchlist is not None else config.EARNINGS_WATCHLIST
    )

    window = storage.get_calendar_window(
        start, end,
        {
            "min_market_cap_m": min_cap_m,
            "industries": industries_list,
            "watchlist": watchlist_list,
            "include_earnings": bool(include_earnings),
            "include_ipos": bool(include_ipos),
            "include_macro": bool(include_macro),
        },
    )
    body = _render_calendar_ics(window, cal_name="YunFlow Earnings Calendar")
    headers = {
        "Cache-Control": "public, max-age=1800",
    }
    if download:
        headers["Content-Disposition"] = 'attachment; filename="earnings-calendar.ics"'
    return Response(
        content=body,
        media_type="text/calendar; charset=utf-8",
        headers=headers,
    )


EARNINGS_HTML = r"""<!doctype html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>财报日历 · YunFlow</title>
<style>
:root {
  --bg: #f5f6f8; --surface: #ffffff; --surface2: #f0f1f4;
  --border: #e2e4e9; --text: #111318; --text2: #4b5160; --muted: #8b8f99;
  --accent: #2563eb; --accent-bg: #eff4ff;
  --earn: #2563eb; --earn-bg: #eff4ff;
  --ipo: #7c3aed; --ipo-bg: #f3efff;
  --macro-hi: #dc2626; --macro-hi-bg: #fef2f2;
  --macro-md: #ea580c; --macro-md-bg: #fff4eb;
  --macro-lo: #6b7280; --macro-lo-bg: #f3f4f6;
  --today: #fbbf24;
}
[data-theme="dark"] {
  --bg: #0f1117; --surface: #181c25; --surface2: #1f2430;
  --border: #2a2f3c; --text: #e8eaf0; --text2: #b5b9c6; --muted: #8084a0;
  --accent: #5b8def; --accent-bg: #1c2740;
  --earn-bg: #16213d; --ipo-bg: #271940; --macro-hi-bg: #3a0a0a;
  --macro-md-bg: #3a1f0a; --macro-lo-bg: #232733;
}
@media (prefers-color-scheme: dark) {
  [data-theme="auto"] {
    --bg: #0f1117; --surface: #181c25; --surface2: #1f2430;
    --border: #2a2f3c; --text: #e8eaf0; --text2: #b5b9c6; --muted: #8084a0;
    --accent: #5b8def; --accent-bg: #1c2740;
    --earn-bg: #16213d; --ipo-bg: #271940; --macro-hi-bg: #3a0a0a;
    --macro-md-bg: #3a1f0a; --macro-lo-bg: #232733;
  }
}
* { box-sizing: border-box; }
body { margin:0; font:15px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", "Helvetica Neue", Arial, "PingFang SC", "Microsoft YaHei", sans-serif; color:var(--text); background:var(--bg); }
.page { max-width: 1400px; margin: 0 auto; padding: 16px; }

.topbar { display: flex; align-items: center; gap: 12px; margin-bottom: 12px; flex-wrap: wrap; }
.back-btn { display: inline-flex; align-items: center; gap: 6px; padding: 7px 12px; background: var(--surface); border: 1px solid var(--border); border-radius: 8px; color: var(--text2); text-decoration: none; font-size: 14px; }
.back-btn:hover { color: var(--accent); border-color: var(--accent); }
h1.title { margin: 0; font-size: 22px; font-weight: 700; flex: 1; }
.theme-btn { background: var(--surface); border: 1px solid var(--border); color: var(--text2); padding: 6px 10px; border-radius: 6px; cursor: pointer; font-size: 14px; }

.cal-export { position: relative; display: inline-block; }
.cal-export-btn { display: inline-flex; align-items: center; gap: 6px; background: var(--accent-bg); border: 1px solid var(--accent); color: var(--accent); padding: 7px 12px; border-radius: 8px; cursor: pointer; font-size: 14px; font-weight: 600; }
.cal-export-btn:hover { filter: brightness(0.95); }
.cal-export-menu { position: absolute; right: 0; top: calc(100% + 6px); background: var(--surface); border: 1px solid var(--border); border-radius: 10px; box-shadow: 0 8px 24px rgba(0,0,0,0.12); min-width: 260px; padding: 6px; z-index: 60; display: none; }
.cal-export-menu.open { display: block; }
.cal-export-menu .item { display: flex; align-items: flex-start; gap: 10px; padding: 9px 11px; border-radius: 7px; cursor: pointer; color: var(--text); text-decoration: none; }
.cal-export-menu .item:hover { background: var(--surface2); }
.cal-export-menu .item.disabled { opacity: 0.55; cursor: not-allowed; }
.cal-export-menu .item.disabled:hover { background: transparent; }
.cal-export-menu .item.disabled .sub { color: var(--macro-hi); }
.export-summary { padding: 8px 11px 10px; margin-bottom: 4px; border-bottom: 1px solid var(--border); font-size: 12px; color: var(--text2); line-height: 1.5; }
.export-summary .row { display: flex; gap: 6px; align-items: baseline; }
.export-summary .label { color: var(--muted); flex-shrink: 0; }
.export-summary .value { color: var(--text); font-weight: 500; }
.export-summary .live-note { font-size: 11px; color: var(--muted); margin-top: 3px; font-style: italic; }
.cal-export-menu .item .ico { font-size: 18px; line-height: 1.3; flex-shrink: 0; }
.cal-export-menu .item .lbl { font-size: 14px; font-weight: 600; }
.cal-export-menu .item .sub { font-size: 11.5px; color: var(--muted); margin-top: 2px; line-height: 1.35; }
.cal-export-menu .divider { height: 1px; background: var(--border); margin: 4px 6px; }
.cal-toast { position: fixed; bottom: 24px; left: 50%; transform: translateX(-50%) translateY(20px); background: var(--text); color: var(--surface); padding: 10px 18px; border-radius: 8px; font-size: 14px; opacity: 0; pointer-events: none; transition: opacity .2s, transform .2s; z-index: 100; }
.cal-toast.show { opacity: 1; transform: translateX(-50%) translateY(0); }

.cal-nav { display: flex; align-items: center; gap: 8px; background: var(--surface); border: 1px solid var(--border); border-radius: 10px; padding: 10px 14px; margin-bottom: 12px; flex-wrap: wrap; }
.cal-nav button { background: none; border: 1px solid var(--border); color: var(--text); padding: 6px 12px; border-radius: 6px; cursor: pointer; font-size: 14px; }
.cal-nav button:hover { border-color: var(--accent); color: var(--accent); }
.cal-nav .month-label { font-size: 20px; font-weight: 600; min-width: 140px; text-align: center; }
.cal-nav .spacer { flex: 1; }

.filters { display: flex; gap: 14px; align-items: center; background: var(--surface); border: 1px solid var(--border); border-radius: 10px; padding: 10px 14px; margin-bottom: 12px; flex-wrap: wrap; font-size: 14px; }
.filters label { display: flex; align-items: center; gap: 6px; color: var(--text2); }
.filters .cap-val { font-weight: 600; color: var(--accent); min-width: 60px; }
.filters .toggle { display: inline-flex; gap: 6px; align-items: center; cursor: pointer; }
.legend-dot { display: inline-block; width: 10px; height: 10px; border-radius: 50%; vertical-align: middle; margin-right: 4px; }
.chip-group { display: inline-flex; gap: 6px; flex-wrap: wrap; }
.chip { display: inline-flex; align-items: center; gap: 5px; padding: 6px 12px; background: var(--surface2); border: 1px solid var(--border); border-radius: 999px; cursor: pointer; font-size: 13px; color: var(--text2); user-select: none; transition: all .12s; }
.chip:hover { border-color: var(--accent); }
.chip.active { font-weight: 600; }
.chip[data-kind="earn"].active { background: var(--earn-bg); color: var(--earn); border-color: var(--earn); }
.chip[data-kind="ipo"].active { background: var(--ipo-bg); color: var(--ipo); border-color: var(--ipo); }
.chip[data-kind="macro"].active { background: var(--macro-hi-bg); color: var(--macro-hi); border-color: var(--macro-hi); }
.chip[data-kind="watch"].active { background: #fffbeb; color: #b45309; border-color: #fbbf24; }
[data-theme="dark"] .chip[data-kind="watch"].active { background: #3a2e0a; color: #fbbf24; }
.chip-info { font-size: 11px; color: var(--muted); cursor: help; margin-left: 4px; opacity: 0.65; }
.chip-info:hover { opacity: 1; }

.cal-grid { background: var(--surface); border: 1px solid var(--border); border-radius: 10px; overflow: hidden; }
.cal-week-header { display: grid; grid-template-columns: repeat(7, 1fr); background: var(--surface2); border-bottom: 1px solid var(--border); }
.cal-week-header > div { padding: 8px 12px; font-size: 13px; font-weight: 600; color: var(--text2); text-align: center; }
.cal-week-header > div.weekend { color: var(--muted); }
.cal-month { display: grid; grid-template-columns: repeat(7, 1fr); }
.cal-day { min-height: 110px; border-right: 1px solid var(--border); border-bottom: 1px solid var(--border); padding: 6px 8px; cursor: pointer; transition: background .12s; display: flex; flex-direction: column; gap: 4px; }
.cal-day:hover { background: var(--surface2); }
.cal-day.other-month { background: var(--surface2); opacity: 0.45; }
.cal-day.today .day-num { background: var(--today); color: #1a1a1a; border-radius: 999px; width: 24px; height: 24px; display: inline-flex; align-items: center; justify-content: center; font-weight: 700; }
.cal-day .day-head { display: flex; justify-content: space-between; align-items: center; font-size: 13px; }
.cal-day .day-num { font-weight: 600; color: var(--text); }
.cal-day.weekend .day-num { color: var(--muted); }
.cal-day .day-count { font-size: 11px; color: var(--muted); }
.cal-day .day-events { display: flex; flex-direction: column; gap: 2px; min-height: 0; }
.evt { font-size: 12px; padding: 3px 7px; border-radius: 4px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; line-height: 1.4; }
.evt-earn { background: var(--earn-bg); color: var(--earn); }
.evt-ipo { background: var(--ipo-bg); color: var(--ipo); }
.evt-macro-high { background: var(--macro-hi-bg); color: var(--macro-hi); font-weight: 600; }
.evt-macro-medium { background: var(--macro-md-bg); color: var(--macro-md); }
.evt-macro-low { background: var(--macro-lo-bg); color: var(--macro-lo); }
.evt-more { font-size: 11px; color: var(--muted); padding: 1px 6px; }

.drawer-mask { position: fixed; inset: 0; background: rgba(0,0,0,0.4); z-index: 50; opacity: 0; pointer-events: none; transition: opacity .2s; }
.drawer-mask.open { opacity: 1; pointer-events: auto; }
.drawer { position: fixed; right: 0; top: 0; bottom: 0; width: 420px; max-width: 92vw; background: var(--surface); border-left: 1px solid var(--border); z-index: 51; transform: translateX(100%); transition: transform .25s; overflow-y: auto; padding: 18px 20px; }
.drawer.open { transform: translateX(0); }
.drawer-head { display: flex; justify-content: space-between; align-items: center; margin-bottom: 14px; padding-bottom: 12px; border-bottom: 1px solid var(--border); }
.drawer-head .d-date { font-size: 20px; font-weight: 700; }
.drawer-head .d-close { background: none; border: none; color: var(--text2); font-size: 24px; cursor: pointer; padding: 2px 8px; }
.drawer-section { margin-bottom: 18px; }
.drawer-section h3 { margin: 0 0 8px; font-size: 14px; text-transform: uppercase; letter-spacing: 0.5px; color: var(--text2); font-weight: 600; }
.drawer-row { padding: 11px 13px; background: var(--surface2); border-radius: 8px; margin-bottom: 6px; }
.drawer-row .row-head { display: flex; justify-content: space-between; align-items: center; gap: 8px; flex-wrap: wrap; }
.drawer-row .row-title { font-weight: 600; font-size: 15px; }
.drawer-row .row-sub { font-size: 13px; color: var(--text2); margin-top: 4px; line-height: 1.5; }
.drawer-row a { color: var(--accent); text-decoration: none; font-size: 14px; }
.drawer-row a:hover { text-decoration: underline; }
.drawer-row .row-name { color: var(--muted); font-weight: 400; }
.drawer-row .logo { width: 18px; height: 18px; border-radius: 4px; vertical-align: middle; margin-right: 6px; }
.pill { display: inline-block; padding: 3px 8px; border-radius: 4px; font-size: 11px; font-weight: 600; }
.pill-bmo { background: #fef3c7; color: #92400e; }
.pill-amc { background: #dbeafe; color: #1e40af; }
.pill-dmh { background: #e9d5ff; color: #6b21a8; }
.pill-high { background: var(--macro-hi-bg); color: var(--macro-hi); }
.pill-medium { background: var(--macro-md-bg); color: var(--macro-md); }
.pill-low { background: var(--macro-lo-bg); color: var(--macro-lo); }
[data-theme="dark"] .pill-bmo { background: #3a2e0a; color: #fbbf24; }
[data-theme="dark"] .pill-amc { background: #1c2740; color: #93c5fd; }
[data-theme="dark"] .pill-dmh { background: #2a1745; color: #d8b4fe; }

.empty { text-align: center; color: var(--muted); padding: 30px 0; font-size: 14px; }

/* News expander */
.news-toggle { background: none; border: none; color: var(--accent); font-size: 13px; cursor: pointer; margin-top: 8px; padding: 2px 0; display: inline-flex; align-items: center; gap: 4px; }
.news-toggle:hover { text-decoration: underline; }
.news-toggle .arrow { transition: transform .2s; display: inline-block; }
.news-toggle.open .arrow { transform: rotate(90deg); }
.news-list { margin-top: 8px; padding-left: 12px; border-left: 2px solid var(--border); display: none; }
.news-list.open { display: block; }
.news-item { padding: 6px 0; border-bottom: 1px dashed var(--border); }
.news-item:last-child { border-bottom: none; }
.news-item .nt { font-size: 13.5px; line-height: 1.45; color: var(--text); }
.news-item .nt a { color: var(--text); text-decoration: none; }
.news-item .nt a:hover { color: var(--accent); text-decoration: underline; }
.news-item .nm { font-size: 11.5px; color: var(--muted); margin-top: 3px; display: flex; gap: 8px; flex-wrap: wrap; }
.news-item .nm .src { color: var(--accent); }
.news-item .nm .src.finnhub { color: var(--ipo); }
.news-loading { font-size: 13px; color: var(--muted); padding: 8px 0; }
.news-empty { font-size: 13px; color: var(--muted); padding: 8px 0; font-style: italic; }

/* dot indicators — hidden on desktop */
.day-dots { display: none; }

@media (max-width: 720px) {
  /* compact grid cells — dots only, no text labels */
  .cal-day { min-height: 52px; padding: 5px 4px; justify-content: flex-start; gap: 2px; }
  .cal-day .day-head { justify-content: center; }
  .cal-day .day-count { display: none; }
  .cal-day .day-events { display: none; }
  .day-dots { display: flex; gap: 3px; flex-wrap: wrap; justify-content: center; padding: 0 2px; }
  .day-dot { width: 6px; height: 6px; border-radius: 50%; flex-shrink: 0; }
  .dot-earn   { background: var(--earn); }
  .dot-ipo    { background: var(--ipo); }
  .dot-macro-high   { background: var(--macro-hi); }
  .dot-macro-medium { background: var(--macro-md); }
  .dot-macro-low    { background: var(--macro-lo); }

  /* drawer as bottom sheet */
  .drawer {
    top: auto; bottom: 0; right: 0; left: 0;
    width: 100vw; max-width: 100vw; max-height: 82vh;
    border-left: none; border-top: 1px solid var(--border);
    border-radius: 16px 16px 0 0;
    transform: translateY(100%);
    padding: 4px 0 14px;
  }
  .drawer.open { transform: translateY(0); }
  .drawer-head { margin-bottom: 4px; padding: 0 8px 4px; border-bottom: none; }
  .drawer-head .d-date { font-size: 16px; }
  .drawer-head .d-close { padding: 0 6px; font-size: 22px; }
  .drawer-section { margin-bottom: 10px; }
  .drawer-section h3 { margin: 0 0 4px; padding: 0 8px; font-size: 11px; }
  .drawer-row { padding: 8px; margin-bottom: 0; border-radius: 0; border-bottom: 1px solid var(--border); background: transparent; }
  .drawer-row:last-child { border-bottom: none; }
  .news-list { padding-left: 8px; }

  /* compact topbar */
  h1.title { font-size: 16px; white-space: nowrap; }
  .title-sub { display: none; }
  .cal-nav .month-label { font-size: 17px; min-width: 110px; }
  .page { padding: 10px 10px; }
  .topbar { gap: 8px; margin-bottom: 8px; flex-wrap: nowrap; }
  .back-btn { padding: 6px 10px; font-size: 13px; white-space: nowrap; }
  .cal-export-btn { padding: 6px 10px; font-size: 13px; white-space: nowrap; }
}
</style>
</head>
<body data-theme="auto">

<div class="page">
  <div class="topbar">
    <a class="back-btn" href="/">← <span id="t-back">返回主页</span></a>
    <h1 class="title"><span id="t-title">📅 财报日历</span><span class="title-sub" id="t-title-sub"> · Earnings Calendar</span></h1>
    <div class="cal-export" id="calExport">
      <button class="cal-export-btn" onclick="toggleExportMenu(event)">📥 <span id="t-export">添加到日历</span> ▾</button>
      <div class="cal-export-menu" id="exportMenu" onclick="event.stopPropagation()">
        <div class="export-summary" id="exportSummary"></div>
        <a class="item" id="optGcal" href="#" target="_blank" rel="noopener">
          <span class="ico">📅</span>
          <span>
            <div class="lbl" id="t-optGcal">订阅 Google Calendar</div>
            <div class="sub" id="t-optGcalSub">在 Google 中实时同步未来更新</div>
          </span>
        </a>
        <a class="item" id="optApple" href="#">
          <span class="ico">🍎</span>
          <span>
            <div class="lbl" id="t-optApple">订阅 Apple Calendar</div>
            <div class="sub" id="t-optAppleSub">通过 webcal:// 自动同步</div>
          </span>
        </a>
        <div class="divider"></div>
        <a class="item" id="optDownload" href="#" onclick="downloadIcs(event)">
          <span class="ico">⬇️</span>
          <span>
            <div class="lbl" id="t-optDownload">下载 .ics 文件</div>
            <div class="sub" id="t-optDownloadSub">一次性导入，不会自动更新</div>
          </span>
        </a>
        <a class="item" id="optCopy" href="#" onclick="copySubscribeUrl(event)">
          <span class="ico">🔗</span>
          <span>
            <div class="lbl" id="t-optCopy">复制订阅链接</div>
            <div class="sub" id="t-optCopySub">用于 Outlook 或其他日历应用</div>
          </span>
        </a>
      </div>
    </div>
    <button class="theme-btn" id="langBtn" onclick="toggleLang()">EN</button>
    <button class="theme-btn" id="themeBtn" onclick="toggleTheme()">☀</button>
  </div>
  <div class="cal-toast" id="calToast"></div>

  <div class="cal-nav">
    <button onclick="navMonth(-1)">← <span id="t-prev">上月</span></button>
    <div class="month-label" id="monthLabel">—</div>
    <button onclick="navMonth(1)"><span id="t-next">下月</span> →</button>
    <button onclick="goToday()" id="t-today">今天</button>
    <div class="spacer"></div>
  </div>

  <div class="filters">
    <div class="chip-group">
      <span class="chip active" data-kind="earn" id="chipEarn" onclick="toggleChip('earn')">📊 <span id="t-chipE">财报</span></span>
      <span class="chip active" data-kind="ipo" id="chipIpo" onclick="toggleChip('ipo')">🚀 <span id="t-chipI">IPO</span></span>
      <span class="chip active" data-kind="macro" id="chipMacro" onclick="toggleChip('macro')">🏛 <span id="t-chipM">宏观</span></span>
      <span class="chip" data-kind="watch" id="chipWatch" onclick="toggleChip('watch')" title="只显示 watchlist 里的热门股 (40 只: AAPL/NVDA/META/BABA/PDD 等)">⭐ <span id="t-chipW">仅热点</span></span>
    </div>
    <label>
      <span id="t-cap">最低市值</span>:
      <input type="range" id="capRange" min="0" max="6" step="1" value="2" oninput="onCapChange()">
      <span class="cap-val" id="capVal">$10B</span>
    </label>
  </div>

  <div class="cal-grid">
    <div class="cal-week-header" id="weekHeader"></div>
    <div class="cal-month" id="calMonth"></div>
  </div>
</div>

<div class="drawer-mask" id="drawerMask" onclick="closeDrawer()"></div>
<div class="drawer" id="drawer">
  <div class="drawer-head">
    <div class="d-date" id="drawerDate">—</div>
    <button class="d-close" onclick="closeDrawer()">×</button>
  </div>
  <div id="drawerBody"></div>
</div>

<script>
const L = {
  zh: {
    back:'返回主页', title:'📅 财报日历', titleSub:' · Earnings Calendar', prev:'上月', next:'下月', today:'今天',
    cap:'最低市值',
    chipEarn:'财报', chipIpo:'IPO', chipMacro:'宏观', chipWatch:'仅热点',
    watchTip:'只显示 watchlist 里的 40 只热门股 (七巨头 / 大科技 / 中概 / Crypto / 金融消费蓝筹)',
    weekdays:['周一','周二','周三','周四','周五','周六','周日'],
    months:['1月','2月','3月','4月','5月','6月','7月','8月','9月','10月','11月','12月'],
    earnings:'财报', ipo:'IPO', macroSec:'美国宏观事件',
    bmo:'盘前', amc:'盘后', dmh:'盘中',
    eps:'EPS 预期', rev:'营收预期', impact:'影响', priceRange:'定价', shares:'股数', exchange:'交易所',
    none:'当天无事件', loading:'加载中…', more:'更多',
    high:'高影响', medium:'中影响', low:'低影响',
    expected:'预期', priced:'已定价', withdrawn:'已撤回', filed:'已申报',
    capLabels:['不限','$1B+','$10B+','$50B+','$200B+','$500B+','$1T+'],
    viewNews:'查看相关资讯', hideNews:'收起资讯', noNews:'未找到相关资讯', loadingNews:'加载中…',
    exportBtn:'添加到日历',
    optGcal:'订阅 Google Calendar', optGcalSub:'在 Google 中实时同步未来更新',
    optGcalLocalSub:'需公网 URL — Google 无法访问 localhost',
    optApple:'订阅 Apple Calendar', optAppleSub:'通过 webcal:// 自动同步',
    optAppleImport:'添加到 Apple Calendar (导入)', optAppleLocalSub:'localhost 下回退为单次导入',
    optDownload:'下载 .ics 文件', optDownloadSub:'一次性导入，不会自动更新',
    optCopy:'复制订阅链接', optCopySub:'用于 Outlook 或其他日历应用',
    toastCopied:'订阅链接已复制', toastCopyFail:'复制失败，请手动复制',
    toastLocalSubscribe:'订阅功能需要公网可访问的 URL',
    sumTypes:'类型', sumCap:'市值', sumRange:'范围', sumWatch:'仅热点 watchlist',
    sumDownloadHint:'下载/导入将仅包含当前可见日历', sumSubscribeHint:'订阅自动同步未来 3 个月',
    sumCapAny:'不限', sumNoTypes:'未选择任何类型',
  },
  en: {
    back:'Back to home', title:'📅 Earnings Calendar', titleSub:'', prev:'Prev', next:'Next', today:'Today',
    cap:'Min Mkt Cap',
    chipEarn:'Earnings', chipIpo:'IPO', chipMacro:'Macro', chipWatch:'Watchlist',
    watchTip:'Show only the 40 watchlist tickers (Mag 7 / Big Tech / China ADRs / Crypto / Blue chips)',
    weekdays:['Mon','Tue','Wed','Thu','Fri','Sat','Sun'],
    months:['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'],
    earnings:'Earnings', ipo:'IPO', macroSec:'US Macro Events',
    bmo:'BMO', amc:'AMC', dmh:'DMH',
    eps:'EPS Est', rev:'Rev Est', impact:'Impact', priceRange:'Price', shares:'Shares', exchange:'Exchange',
    none:'No events today', loading:'Loading…', more:'more',
    high:'High', medium:'Med', low:'Low',
    expected:'Expected', priced:'Priced', withdrawn:'Withdrawn', filed:'Filed',
    capLabels:['Any','$1B+','$10B+','$50B+','$200B+','$500B+','$1T+'],
    viewNews:'View related news', hideNews:'Hide news', noNews:'No related news found', loadingNews:'Loading…',
    exportBtn:'Add to Calendar',
    optGcal:'Subscribe in Google Calendar', optGcalSub:'Auto-sync future updates in Google',
    optGcalLocalSub:'Needs public URL — Google can\'t reach localhost',
    optApple:'Subscribe in Apple Calendar', optAppleSub:'Auto-sync via webcal://',
    optAppleImport:'Add to Apple Calendar (import)', optAppleLocalSub:'Falls back to one-time import on localhost',
    optDownload:'Download .ics file', optDownloadSub:'One-time import, no auto-update',
    optCopy:'Copy subscribe URL', optCopySub:'Use with Outlook or any calendar app',
    toastCopied:'Subscribe URL copied', toastCopyFail:'Copy failed — please copy manually',
    toastLocalSubscribe:'Subscribe needs a publicly reachable URL',
    sumTypes:'Types', sumCap:'Min cap', sumRange:'Range', sumWatch:'Watchlist only',
    sumDownloadHint:'Download/import includes only the visible month', sumSubscribeHint:'Subscribe auto-syncs next 3 months',
    sumCapAny:'Any', sumNoTypes:'No types selected',
  }
};
const CAP_VALUES = [0, 1000, 10000, 50000, 200000, 500000, 1000000];

let lang = localStorage.getItem('lang') || 'zh';
let theme = localStorage.getItem('theme') || 'auto';
let cursor = new Date(); cursor.setDate(1);
let data = {};
let onlyWatch = false;

function $(id) { return document.getElementById(id); }
function el(tag, opts) {
  const e = document.createElement(tag);
  if (!opts) return e;
  if (opts.cls) e.className = opts.cls;
  if (opts.text != null) e.textContent = opts.text;
  if (opts.title) e.title = opts.title;
  if (opts.href) e.href = opts.href;
  if (opts.target) e.target = opts.target;
  if (opts.rel) e.rel = opts.rel;
  if (opts.style) e.setAttribute('style', opts.style);
  if (opts.onclick) e.onclick = opts.onclick;
  return e;
}

function applyTheme() {
  document.body.setAttribute('data-theme', theme);
  $('themeBtn').textContent = theme==='dark' ? '☾' : (theme==='light' ? '☀' : '◐');
}
function toggleTheme() {
  theme = theme==='auto' ? 'light' : (theme==='light' ? 'dark' : 'auto');
  localStorage.setItem('theme', theme); applyTheme();
}
function toggleLang() {
  lang = lang==='zh' ? 'en' : 'zh';
  localStorage.setItem('lang', lang); applyLang(); render();
}
function applyLang() {
  const l = L[lang];
  document.documentElement.lang = lang;
  $('t-back').textContent = l.back;
  $('t-title').textContent = l.title;
  $('t-title-sub').textContent = l.titleSub || '';
  $('t-prev').textContent = l.prev;
  $('t-next').textContent = l.next;
  $('t-today').textContent = l.today;
  $('t-cap').textContent = l.cap;
  $('t-chipE').textContent = l.chipEarn;
  $('t-chipI').textContent = l.chipIpo;
  $('t-chipM').textContent = l.chipMacro;
  $('t-chipW').textContent = l.chipWatch;
  $('chipWatch').title = l.watchTip;
  $('langBtn').textContent = lang==='zh' ? 'EN' : '中';
  $('t-export').textContent = l.exportBtn;
  $('t-optGcal').textContent = l.optGcal;
  $('t-optGcalSub').textContent = l.optGcalSub;
  $('t-optApple').textContent = l.optApple;
  $('t-optAppleSub').textContent = l.optAppleSub;
  $('t-optDownload').textContent = l.optDownload;
  $('t-optDownloadSub').textContent = l.optDownloadSub;
  $('t-optCopy').textContent = l.optCopy;
  $('t-optCopySub').textContent = l.optCopySub;
  refreshExportLinks();
  const wh = $('weekHeader');
  wh.textContent = '';
  l.weekdays.forEach((w, i) => {
    const d = el('div', { text: w });
    if (i >= 5) d.classList.add('weekend');
    wh.appendChild(d);
  });
  updateCapLabel();
}
function updateCapLabel() {
  const idx = parseInt($('capRange').value);
  $('capVal').textContent = L[lang].capLabels[idx];
}
function onCapChange() { updateCapLabel(); reload(); refreshExportLinks(); }
function toggleChip(kind) {
  const id = { earn: 'chipEarn', ipo: 'chipIpo', macro: 'chipMacro', watch: 'chipWatch' }[kind];
  if (!id) return;
  $(id).classList.toggle('active');
  if (kind === 'watch') onlyWatch = $(id).classList.contains('active');
  reload();
  refreshExportLinks();
}

// ── Calendar export (.ics) ────────────────────────────────────────────────
function visibleGridRange() {
  // Same logic as reload(): Monday of the week containing the 1st, +42 days.
  const gridStart = new Date(cursor); gridStart.setDate(1);
  const firstWeekday = (gridStart.getDay() + 6) % 7;
  gridStart.setDate(gridStart.getDate() - firstWeekday);
  const gridEnd = new Date(gridStart); gridEnd.setDate(gridStart.getDate() + 41);
  return { start: ymd(gridStart), end: ymd(gridEnd) };
}
function buildIcsParams(opts) {
  opts = opts || {};
  const capIdx = parseInt($('capRange').value);
  let minCap = CAP_VALUES[capIdx];
  if (onlyWatch) minCap = 99999999;
  const params = new URLSearchParams({
    min_cap_m: String(minCap),
    include_earnings: $('chipEarn').classList.contains('active') ? '1' : '0',
    include_ipos:     $('chipIpo').classList.contains('active')  ? '1' : '0',
    include_macro:    $('chipMacro').classList.contains('active') ? '1' : '0',
  });
  if (onlyWatch) params.set('industries', '');
  // Snapshot mode (download / one-time import): lock the export to exactly
  // what's visible on screen. Subscribe mode (opts.snapshot=false) omits
  // start/end so the calendar app sees a rolling future window — necessary
  // for subscriptions to pick up newly-scheduled earnings.
  if (opts.snapshot) {
    const r = visibleGridRange();
    params.set('start', r.start);
    params.set('end',   r.end);
  }
  return params;
}
function subscribeUrl(scheme) {
  const params = buildIcsParams({ snapshot: false });
  const base = location.origin + '/api/earnings-calendar.ics?' + params.toString();
  if (scheme === 'webcal') return base.replace(/^https?:/, 'webcal:');
  return base;
}
function isLocalDev() {
  const h = location.hostname;
  return h === 'localhost' || h === '127.0.0.1' || h === '0.0.0.0' || h === '::1' || h.endsWith('.local');
}
function fmtRangeShort(startYmd, endYmd) {
  const l = L[lang];
  const s = new Date(startYmd + 'T00:00:00');
  const e = new Date(endYmd + 'T00:00:00');
  const fmt = (d) => (lang === 'zh'
    ? (d.getMonth() + 1) + '月' + d.getDate() + '日'
    : l.months[d.getMonth()] + ' ' + d.getDate());
  return fmt(s) + ' – ' + fmt(e);
}
function refreshExportSummary() {
  const l = L[lang];
  const sum = $('exportSummary'); if (!sum) return;
  const types = [];
  if ($('chipEarn').classList.contains('active'))  types.push('📊 ' + l.chipEarn);
  if ($('chipIpo').classList.contains('active'))   types.push('🚀 ' + l.chipIpo);
  if ($('chipMacro').classList.contains('active')) types.push('🏛 ' + l.chipMacro);
  const typesText = types.length ? types.join(' · ') : l.sumNoTypes;

  const capIdx = parseInt($('capRange').value);
  const capText = onlyWatch ? ('⭐ ' + l.sumWatch) : (CAP_VALUES[capIdx] ? l.capLabels[capIdx] : l.sumCapAny);

  const r = visibleGridRange();
  const rangeText = fmtRangeShort(r.start, r.end);

  sum.textContent = '';
  const mkRow = (label, value) => {
    const row = el('div', { cls: 'row' });
    row.appendChild(el('span', { cls: 'label', text: label + ':' }));
    row.appendChild(el('span', { cls: 'value', text: value }));
    return row;
  };
  sum.appendChild(mkRow(l.sumTypes, typesText));
  sum.appendChild(mkRow(l.sumCap,   capText));
  sum.appendChild(mkRow(l.sumRange, rangeText));
  sum.appendChild(el('div', { cls: 'live-note', text: '⬇️ ' + l.sumDownloadHint }));
  sum.appendChild(el('div', { cls: 'live-note', text: '🔄 ' + l.sumSubscribeHint }));
}
function refreshExportLinks() {
  const l = L[lang];
  const httpsUrl = subscribeUrl('https');
  const webcalUrl = subscribeUrl('webcal');
  const localDev = isLocalDev();
  refreshExportSummary();

  // ── Google Calendar ────────────────────────────────────────────
  // Subscribing via cid= requires Google's servers to fetch the URL.
  // localhost is unreachable from outside, so disable with explanation.
  const gcal = $('optGcal');
  if (localDev) {
    gcal.href = '#';
    gcal.classList.add('disabled');
    gcal.removeAttribute('target');
    gcal.onclick = (e) => { e.preventDefault(); showToast(l.toastLocalSubscribe); };
    $('t-optGcalSub').textContent = l.optGcalLocalSub;
  } else {
    gcal.href = 'https://calendar.google.com/calendar/r?cid=' + encodeURIComponent(httpsUrl);
    gcal.setAttribute('target', '_blank');
    gcal.classList.remove('disabled');
    gcal.onclick = null;
    $('t-optGcalSub').textContent = l.optGcalSub;
  }

  // ── Apple Calendar ─────────────────────────────────────────────
  // webcal:// to localhost or non-standard ports is unreliable on macOS
  // Calendar.app (Sonoma+ enforces HTTPS upgrade; webcal+custom-port often
  // rejected). Fall back to direct .ics file — Calendar.app's file handler
  // opens it and offers Import.
  const apple = $('optApple');
  if (localDev) {
    const params = buildIcsParams({ snapshot: true });
    params.set('download', '1');
    apple.href = '/api/earnings-calendar.ics?' + params.toString();
    apple.setAttribute('download', 'earnings-calendar.ics');
    apple.onclick = () => { $('exportMenu').classList.remove('open'); };
    $('t-optApple').textContent = l.optAppleImport;
    $('t-optAppleSub').textContent = l.optAppleLocalSub;
  } else {
    apple.href = webcalUrl;
    apple.removeAttribute('download');
    apple.onclick = null;
    $('t-optApple').textContent = l.optApple;
    $('t-optAppleSub').textContent = l.optAppleSub;
  }
}
function toggleExportMenu(ev) {
  ev.stopPropagation();
  const open = $('exportMenu').classList.toggle('open');
  if (open) refreshExportLinks();
}
function downloadIcs(ev) {
  ev.preventDefault();
  const params = buildIcsParams({ snapshot: true });
  params.set('download', '1');
  window.location.href = '/api/earnings-calendar.ics?' + params.toString();
  $('exportMenu').classList.remove('open');
}
async function copySubscribeUrl(ev) {
  ev.preventDefault();
  const url = subscribeUrl('https');
  const l = L[lang];
  try {
    await navigator.clipboard.writeText(url);
    showToast(l.toastCopied);
  } catch (e) {
    // Fallback: legacy execCommand for non-secure contexts
    try {
      const ta = document.createElement('textarea');
      ta.value = url; document.body.appendChild(ta);
      ta.select(); document.execCommand('copy');
      document.body.removeChild(ta);
      showToast(l.toastCopied);
    } catch (e2) {
      showToast(l.toastCopyFail);
    }
  }
  $('exportMenu').classList.remove('open');
}
function showToast(msg) {
  const t = $('calToast');
  t.textContent = msg;
  t.classList.add('show');
  clearTimeout(showToast._tm);
  showToast._tm = setTimeout(() => t.classList.remove('show'), 2200);
}
document.addEventListener('click', (e) => {
  const menu = $('exportMenu');
  if (menu && menu.classList.contains('open') && !$('calExport').contains(e.target)) {
    menu.classList.remove('open');
  }
});

function ymd(d) {
  const y = d.getFullYear();
  const m = (d.getMonth()+1).toString().padStart(2,'0');
  const day = d.getDate().toString().padStart(2,'0');
  return y + '-' + m + '-' + day;
}
function navMonth(delta) {
  cursor.setMonth(cursor.getMonth() + delta);
  reload();
  refreshExportLinks();
}
function goToday() {
  cursor = new Date(); cursor.setDate(1);
  reload();
  refreshExportLinks();
}

async function reload() {
  const l = L[lang];
  $('monthLabel').textContent = cursor.getFullYear() + ' ' + l.months[cursor.getMonth()];
  const gridStart = new Date(cursor); gridStart.setDate(1);
  const firstWeekday = (gridStart.getDay() + 6) % 7;
  gridStart.setDate(gridStart.getDate() - firstWeekday);
  const gridEnd = new Date(gridStart); gridEnd.setDate(gridStart.getDate() + 41);

  const capIdx = parseInt($('capRange').value);
  let minCap = CAP_VALUES[capIdx];
  if (onlyWatch) minCap = 99999999;
  const params = new URLSearchParams({
    start: ymd(gridStart),
    end:   ymd(gridEnd),
    min_cap_m: String(minCap),
    include_earnings: $('chipEarn').classList.contains('active') ? '1' : '0',
    include_ipos:     $('chipIpo').classList.contains('active')  ? '1' : '0',
    include_macro:    $('chipMacro').classList.contains('active') ? '1' : '0',
  });
  if (onlyWatch) params.set('industries', '');
  try {
    const res = await fetch('/api/earnings-calendar?' + params.toString());
    data = await res.json();
  } catch (e) {
    data = {};
  }
  render();
}

function makeEvt(text, cls, tooltip) {
  return el('div', { cls: 'evt ' + cls, text: text, title: tooltip || text });
}

function render() {
  const l = L[lang];
  const month = cursor.getMonth();
  const gridStart = new Date(cursor); gridStart.setDate(1);
  const firstWeekday = (gridStart.getDay() + 6) % 7;
  gridStart.setDate(gridStart.getDate() - firstWeekday);
  const today = ymd(new Date());

  const cal = $('calMonth');
  cal.textContent = '';

  for (let i = 0; i < 42; i++) {
    const d = new Date(gridStart); d.setDate(gridStart.getDate() + i);
    const key = ymd(d);
    const inMonth = d.getMonth() === month;
    const isWeekend = d.getDay() === 0 || d.getDay() === 6;

    const cell = el('div');
    let cls = 'cal-day';
    if (!inMonth) cls += ' other-month';
    if (isWeekend) cls += ' weekend';
    if (key === today) cls += ' today';
    cell.className = cls;
    cell.onclick = () => openDrawer(key);

    const head = el('div', { cls: 'day-head' });
    head.appendChild(el('span', { cls: 'day-num', text: String(d.getDate()) }));
    cell.appendChild(head);

    const ev = data[key] || { earnings: [], ipos: [], macro: [] };
    const total = ev.earnings.length + ev.ipos.length + ev.macro.length;
    if (total > 0) {
      head.appendChild(el('span', { cls: 'day-count', text: String(total) }));
    }

    const list = el('div', { cls: 'day-events' });

    ev.macro.slice(0, 2).forEach(m => {
      const trimmed = m.title.length > 22 ? m.title.slice(0, 22) + '…' : m.title;
      list.appendChild(makeEvt('🏛 ' + trimmed, 'evt-macro-' + (m.impact || 'low'), m.title));
    });

    ev.earnings.slice(0, 3).forEach(e => {
      const hr = e.hour === 'bmo' ? (lang === 'zh' ? '·盘前' : ' BMO')
              : e.hour === 'amc' ? (lang === 'zh' ? '·盘后' : ' AMC') : '';
      list.appendChild(makeEvt(e.symbol + hr, 'evt-earn', (e.name || e.symbol) + ' (' + (e.hour || '') + ')'));
    });

    ev.ipos.slice(0, 2).forEach(i => {
      const label = (i.symbol || i.name).slice(0, 12);
      list.appendChild(makeEvt('🚀 ' + label, 'evt-ipo', i.name));
    });

    const shown = Math.min(2, ev.macro.length) + Math.min(3, ev.earnings.length) + Math.min(2, ev.ipos.length);
    if (total > shown) {
      list.appendChild(el('div', { cls: 'evt-more', text: '+ ' + (total - shown) + ' ' + l.more }));
    }
    cell.appendChild(list);

    // mobile: colored dots (CSS hides on desktop)
    if (total > 0) {
      const dots = el('div', { cls: 'day-dots' });
      ev.macro.forEach(m => {
        const d2 = el('span', { cls: 'day-dot dot-macro-' + (m.impact || 'low') });
        d2.title = m.title;
        dots.appendChild(d2);
      });
      ev.earnings.slice(0, 5).forEach(() => dots.appendChild(el('span', { cls: 'day-dot dot-earn' })));
      ev.ipos.slice(0, 3).forEach(() => dots.appendChild(el('span', { cls: 'day-dot dot-ipo' })));
      cell.appendChild(dots);
    }

    cal.appendChild(cell);
  }
}

function fmtRev(v) {
  if (v == null || isNaN(v)) return '—';
  const n = Number(v);
  if (n >= 1e9) return '$' + (n/1e9).toFixed(1) + 'B';
  if (n >= 1e6) return '$' + (n/1e6).toFixed(0) + 'M';
  return '$' + n.toFixed(0);
}
function fmtCap(m) {
  if (!m) return '';
  if (m >= 1e6) return '$' + (m/1e6).toFixed(1) + 'T';
  if (m >= 1000) return '$' + (m/1000).toFixed(0) + 'B';
  return '$' + m.toFixed(0) + 'M';
}

function fmtNewsDate(s) {
  if (!s) return '';
  try {
    const d = new Date(s);
    if (isNaN(d.getTime())) return s.slice(0, 10);
    const now = new Date();
    const diffH = (now - d) / 3600000;
    if (diffH < 1) return Math.max(1, Math.round(diffH * 60)) + 'm';
    if (diffH < 24) return Math.round(diffH) + 'h';
    if (diffH < 24 * 30) return Math.round(diffH / 24) + 'd';
    return s.slice(0, 10);
  } catch (e) { return s.slice(0, 10); }
}

const NEWS_CACHE = {};

function buildNewsItem(n) {
  const item = el('div', { cls: 'news-item' });
  const t = el('div', { cls: 'nt' });
  const link = el('a', { href: n.url || '#', target: '_blank', rel: 'noopener',
                          text: (lang === 'zh' && n.title_zh) ? n.title_zh : (n.title || '') });
  t.appendChild(link);
  item.appendChild(t);

  const meta = el('div', { cls: 'nm' });
  if (n.source) {
    meta.appendChild(el('span', { cls: 'src' + (n.category === 'finnhub' ? ' finnhub' : ''), text: n.source }));
  }
  if (n.published) {
    meta.appendChild(el('span', { text: fmtNewsDate(n.published) }));
  }
  item.appendChild(meta);
  return item;
}

function attachNewsExpander(row, params) {
  const l = L[lang];
  const cacheKey = JSON.stringify(params);

  const toggle = el('button', { cls: 'news-toggle' });
  const arrow = el('span', { cls: 'arrow', text: '▶' });
  const labelSpan = el('span', { text: ' ' + l.viewNews });
  toggle.appendChild(arrow);
  toggle.appendChild(labelSpan);

  const list = el('div', { cls: 'news-list' });
  let loaded = false;

  toggle.onclick = async (ev) => {
    ev.stopPropagation();
    const isOpen = toggle.classList.toggle('open');
    list.classList.toggle('open', isOpen);
    labelSpan.textContent = ' ' + (isOpen ? l.hideNews : l.viewNews);
    if (isOpen && !loaded) {
      loaded = true;
      list.textContent = '';
      list.appendChild(el('div', { cls: 'news-loading', text: l.loadingNews }));
      try {
        let data;
        if (NEWS_CACHE[cacheKey]) {
          data = NEWS_CACHE[cacheKey];
        } else {
          const qs = new URLSearchParams(params);
          const res = await fetch('/api/event-news?' + qs.toString());
          data = await res.json();
          NEWS_CACHE[cacheKey] = data;
        }
        list.textContent = '';
        const items = (data && data.items) || [];
        if (!items.length) {
          list.appendChild(el('div', { cls: 'news-empty', text: l.noNews }));
        } else {
          items.forEach(n => list.appendChild(buildNewsItem(n)));
        }
      } catch (err) {
        list.textContent = '';
        list.appendChild(el('div', { cls: 'news-empty', text: l.noNews }));
      }
    }
  };

  row.appendChild(toggle);
  row.appendChild(list);
}

function buildEarningsRow(e) {
  const l = L[lang];
  const row = el('div', { cls: 'drawer-row' });
  const head = el('div', { cls: 'row-head' });
  const title = el('div', { cls: 'row-title' });
  if (e.logo) {
    const img = el('img', { cls: 'logo' });
    img.src = e.logo;
    img.onerror = () => { img.style.display = 'none'; };
    title.appendChild(img);
  }
  const link = el('a', {
    href: e.weburl || ('https://finance.yahoo.com/quote/' + e.symbol),
    target: '_blank', rel: 'noopener', text: e.symbol,
  });
  title.appendChild(link);
  if (e.name) {
    title.appendChild(el('span', { cls: 'row-name', text: ' ' + e.name }));
  }
  head.appendChild(title);
  if (e.hour) {
    head.appendChild(el('span', { cls: 'pill pill-' + e.hour, text: l[e.hour] || e.hour.toUpperCase() }));
  }
  row.appendChild(head);

  const subParts = [];
  if (e.eps_estimate != null) subParts.push(l.eps + ': $' + Number(e.eps_estimate).toFixed(2));
  if (e.rev_estimate != null) subParts.push(l.rev + ': ' + fmtRev(e.rev_estimate));
  if (e.market_cap_m) subParts.push(fmtCap(e.market_cap_m));
  let subText = subParts.join(' · ');
  if (e.industry) subText += (subText ? '\n' : '') + e.industry;
  if (subText) {
    const sub = el('div', { cls: 'row-sub' });
    sub.style.whiteSpace = 'pre-line';
    sub.textContent = subText;
    row.appendChild(sub);
  }
  attachNewsExpander(row, { type: 'earnings', symbol: e.symbol, name: e.name || '' });
  return row;
}

function buildIpoRow(i) {
  const l = L[lang];
  const row = el('div', { cls: 'drawer-row' });
  const head = el('div', { cls: 'row-head' });
  const title = el('div', { cls: 'row-title', text: i.name });
  if (i.symbol) {
    title.appendChild(el('span', { cls: 'row-name', text: ' (' + i.symbol + ')' }));
  }
  head.appendChild(title);
  if (i.status) {
    head.appendChild(el('span', { cls: 'pill pill-medium', text: l[i.status] || i.status }));
  }
  row.appendChild(head);

  const subParts = [];
  if (i.exchange) subParts.push(l.exchange + ': ' + i.exchange);
  if (i.price_range) subParts.push(l.priceRange + ': $' + i.price_range);
  if (i.shares) subParts.push(l.shares + ': ' + Number(i.shares).toLocaleString());
  if (subParts.length) {
    row.appendChild(el('div', { cls: 'row-sub', text: subParts.join(' · ') }));
  }
  attachNewsExpander(row, { type: 'ipo', symbol: i.symbol || '', name: i.name || '' });
  return row;
}

function buildMacroRow(m) {
  const l = L[lang];
  const row = el('div', { cls: 'drawer-row' });
  const head = el('div', { cls: 'row-head' });
  head.appendChild(el('div', { cls: 'row-title', text: m.title }));
  if (m.impact) {
    head.appendChild(el('span', { cls: 'pill pill-' + m.impact, text: l[m.impact] || m.impact }));
  }
  row.appendChild(head);

  const subParts = [];
  if (m.time) subParts.push('⏰ ' + m.time);
  if (m.category) subParts.push(m.category.toUpperCase());
  let subText = subParts.join(' · ');
  if (m.notes) subText += (subText ? '\n' : '') + m.notes;
  if (subText) {
    const sub = el('div', { cls: 'row-sub' });
    sub.style.whiteSpace = 'pre-line';
    sub.textContent = subText;
    row.appendChild(sub);
  }
  attachNewsExpander(row, { type: 'macro', category: m.category || '', title: m.title || '' });
  return row;
}

function buildSection(headerText, items, builder) {
  if (!items.length) return null;
  const sec = el('div', { cls: 'drawer-section' });
  sec.appendChild(el('h3', { text: headerText + ' (' + items.length + ')' }));
  items.forEach(it => sec.appendChild(builder(it)));
  return sec;
}

function openDrawer(date) {
  const l = L[lang];
  const ev = data[date] || { earnings: [], ipos: [], macro: [] };
  $('drawerDate').textContent = date;
  const body = $('drawerBody');
  body.textContent = '';

  if (ev.earnings.length + ev.ipos.length + ev.macro.length === 0) {
    body.appendChild(el('div', { cls: 'empty', text: l.none }));
  } else {
    const macroSec = buildSection('🏛 ' + l.macroSec, ev.macro, buildMacroRow);
    if (macroSec) body.appendChild(macroSec);
    const earnSec = buildSection('📊 ' + l.earnings, ev.earnings, buildEarningsRow);
    if (earnSec) body.appendChild(earnSec);
    const ipoSec = buildSection('🚀 ' + l.ipo, ev.ipos, buildIpoRow);
    if (ipoSec) body.appendChild(ipoSec);
  }

  $('drawerMask').classList.add('open');
  $('drawer').classList.add('open');
}
function closeDrawer() {
  $('drawerMask').classList.remove('open');
  $('drawer').classList.remove('open');
}

document.addEventListener('keydown', e => { if (e.key === 'Escape') closeDrawer(); });
applyTheme();
applyLang();
reload();
</script>
</body>
</html>"""


@app.get("/earnings", response_class=HTMLResponse)
def earnings_page(sub=Depends(auth.require_subscriber)):
    storage.record_visit()
    return HTMLResponse(content=EARNINGS_HTML)


# Cache the macro keywords map (loaded once, reused for every event-news lookup)
_MACRO_KEYWORDS_CACHE: dict[str, list[str]] | None = None


def _macro_keywords_map() -> dict[str, list[str]]:
    global _MACRO_KEYWORDS_CACHE
    if _MACRO_KEYWORDS_CACHE is not None:
        return _MACRO_KEYWORDS_CACHE
    import os
    path = os.path.join(os.path.dirname(__file__), "data", "us_macro_events.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            doc = json.load(f)
        _MACRO_KEYWORDS_CACHE = (doc.get("keywords_by_category") or {}) if isinstance(doc, dict) else {}
    except Exception:
        _MACRO_KEYWORDS_CACHE = {}
    return _MACRO_KEYWORDS_CACHE


def _normalize_post(p: dict) -> dict:
    """Trim fields for the event-news response (keep payload light)."""
    return {
        "source":    p.get("source") or "",
        "title":     p.get("title") or "",
        "title_zh":  p.get("title_zh") or "",
        "summary":   (p.get("summary") or "")[:280],
        "url":       p.get("url") or "",
        "published": p.get("published") or p.get("fetched_at") or "",
        "category":  p.get("category") or "",
    }


@app.get("/api/event-news")
def api_event_news(
    type: str,                # "earnings" | "ipo" | "macro"
    symbol: str = None,       # for earnings/ipo
    name: str = None,         # company name (used as fallback search term)
    category: str = None,     # for macro: "fomc"|"cpi"|...
    title: str = None,        # for macro: full title (also used as search term)
    limit: int = 8,
):
    """Return relevant news items for a calendar event.

    Strategy:
    - Earnings/IPO: search local blog_posts by symbol/name; if < 3 hits AND we have
      a Finnhub key, also fetch /company-news as supplementary results.
    - Macro: search local blog_posts using keywords mapped from event category
      (with the title itself as an extra hint).
    """
    type = (type or "").lower()
    items: list[dict] = []

    if type in ("earnings", "ipo"):
        keywords = []
        if symbol:
            keywords.append(symbol)
        if name:
            # Strip common corporate suffixes so 'NVIDIA Corporation' also matches 'NVIDIA'
            cleaned = name
            for suf in (" Corporation", " Corp.", " Corp", ", Inc.", " Inc.", " Inc",
                        " Ltd.", " Ltd", " plc", " plc.", " AG", " SA", " NV"):
                if cleaned.endswith(suf):
                    cleaned = cleaned[: -len(suf)]
            keywords.append(cleaned.strip())
        local = storage.search_posts_any_keyword(keywords, limit=limit, days=90)
        items = [_normalize_post(p) for p in local]

        if type == "earnings" and symbol and len(items) < 3:
            try:
                from earnings_monitor import fetch_company_news
                extra = fetch_company_news(symbol, days=14, limit=limit - len(items))
                # Mark these so the UI can label them differently
                for e in extra:
                    e["category"] = "finnhub"
                items.extend(extra)
            except Exception as e:
                logger.warning("Finnhub company-news fallback failed: %s", e)

    elif type == "macro":
        keywords = list(_macro_keywords_map().get((category or "").lower(), []))
        local = storage.search_posts_any_keyword(keywords, limit=limit, days=30)
        items = [_normalize_post(p) for p in local]

    return {"items": items[:limit]}


# ── Subscription / Auth routes ─────────────────────────────────────────────
#
# Magic Link flow:
#   GET  /login                  → form to enter your email
#   POST /auth/request-link      → emails a one-time login URL
#   GET  /auth/verify?token=...  → consumes the token, sets session cookie
#   POST /auth/logout            → clears session cookie
#   GET  /account                → logged-in user's status + logout button
#
# The dependency helpers (require_subscriber / require_paid) live in
# auth.py; they raise HTTPException(302) for HTML routes so the browser
# follows the Location header back to /login or /account?paywall=1.


def _redirect(target: str) -> RedirectResponse:
    """Internal helper — 303 'See Other' so POST→GET is enforced."""
    return RedirectResponse(target, status_code=303)


def _set_session_cookie(response: Response, session_id: str) -> None:
    """Attach the session cookie with appropriate Secure/HttpOnly flags."""
    is_https = config.BASE_URL.lower().startswith("https://")
    response.set_cookie(
        key=config.SESSION_COOKIE_NAME,
        value=session_id,
        max_age=config.SESSION_TTL_DAYS * 24 * 3600,
        httponly=True,
        secure=is_https,
        samesite="lax",
        path="/",
    )


def _clear_session_cookie(response: Response) -> None:
    response.delete_cookie(
        key=config.SESSION_COOKIE_NAME,
        path="/",
    )


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next: str = "/", sent: int = 0,
               err: str = "", tab: str = "link", email: str = ""):
    """Render the login page with Tab toggle between Magic Link and verification code.

    Query params:
      tab=link|code   which tab is active (default 'link')
      sent=1          show the post-submit panel ("check your email" or
                      "enter the 6-digit code" depending on tab)
      email=...       pre-fill the email input on the code tab after request-code
      err=...         shows a red error banner
      next=...        post-login redirect target
    """
    # If already logged in, send them to the dashboard
    if auth.current_subscriber(request):
        return _redirect("/")

    import html as _h
    next_safe = _h.escape(next or "/")
    email_safe = _h.escape(email or "")
    active_tab = "code" if tab == "code" else "link"

    err_panel = (
        f"<div style='background:#fee2e2;border:1px solid #fca5a5;color:#991b1b;"
        f"padding:12px 16px;border-radius:8px;margin-bottom:16px;font-size:14px;'>"
        f"⚠ {_h.escape(err)}</div>"
        if err else ""
    )

    link_sent_panel = (
        """<div style='background:#dcfce7;border:1px solid #86efac;color:#166534;
              padding:12px 16px;border-radius:8px;margin-bottom:16px;font-size:14px;
              line-height:1.6;'>
            ✓ 如果该邮箱在订阅名单中,登录链接已发送。请查收邮件
            (15 分钟内有效)。
          </div>"""
        if sent and active_tab == "link" else ""
    )
    code_sent_panel = (
        f"""<div style='background:#dcfce7;border:1px solid #86efac;color:#166534;
              padding:12px 16px;border-radius:8px;margin-bottom:16px;font-size:14px;
              line-height:1.6;'>
            ✓ 如果该邮箱在订阅名单中,6 位数字验证码已发送
            ({config.LOGIN_CODE_TTL_MINUTES} 分钟内有效)。请在下方输入。
          </div>"""
        if sent and active_tab == "code" else ""
    )

    # Tabs — switching is done via plain links so JS isn't required.
    def _tab_style(active: bool) -> str:
        if active:
            return ("flex:1;padding:10px 0;text-align:center;font-size:14px;"
                    "font-weight:600;color:#0f3460;background:#fff;"
                    "border-bottom:2px solid #0f3460;text-decoration:none;")
        return ("flex:1;padding:10px 0;text-align:center;font-size:14px;"
                "font-weight:500;color:#888;background:#f9fafb;"
                "border-bottom:2px solid transparent;text-decoration:none;")
    tab_link_href = f"/login?tab=link&next={_h.escape(next or '/')}"
    tab_code_href = f"/login?tab=code&next={_h.escape(next or '/')}"
    link_tab_style = _tab_style(active_tab == "link")
    code_tab_style = _tab_style(active_tab == "code")
    tabs_html = (
        f"<div style='display:flex;margin:0 -32px 24px;border-bottom:1px solid #eee;'>"
        f"<a href='{tab_link_href}' style='{link_tab_style}'>邮件链接</a>"
        f"<a href='{tab_code_href}' style='{code_tab_style}'>验证码</a>"
        f"</div>"
    )

    # ── Tab 1: Magic Link ──
    link_form = f"""
    <form method='post' action='/auth/request-link' style='margin:0;'>
      <input type='hidden' name='next' value='{next_safe}'>
      <input type='email' name='email' required autocomplete='email'
             placeholder='your@email.com'
             {"autofocus" if active_tab == "link" else ""}
             style='display:block;width:100%;box-sizing:border-box;
                    padding:12px 14px;font-size:14px;border:1px solid #d1d5db;
                    border-radius:8px;margin-bottom:14px;'>
      <button type='submit'
              style='display:block;width:100%;padding:12px;font-size:14px;
                     color:#fff;background:#0f3460;border:none;border-radius:8px;
                     font-weight:600;cursor:pointer;'>
        发送登录链接
      </button>
    </form>
    <p style='margin:16px 0 0;font-size:12px;color:#888;line-height:1.5;'>
      链接 {config.MAGIC_LINK_TTL_MINUTES} 分钟内有效,点击即可登录。
    </p>"""

    # ── Tab 2: Verification code (two-step) ──
    if sent and active_tab == "code" and email_safe:
        # Step 2: code entry form, email is already locked in.
        code_form = f"""
    <form method='post' action='/auth/verify-code' style='margin:0;'>
      <input type='hidden' name='next' value='{next_safe}'>
      <input type='hidden' name='email' value='{email_safe}'>
      <div style='font-size:13px;color:#666;margin-bottom:10px;'>
        验证码已发送至 <b style='color:#0f3460;'>{email_safe}</b>
      </div>
      <input type='text' name='code' required autofocus
             inputmode='numeric' pattern='[0-9]{{6}}' maxlength='6'
             autocomplete='one-time-code'
             placeholder='6 位数字'
             style='display:block;width:100%;box-sizing:border-box;
                    padding:12px 14px;font-size:18px;letter-spacing:6px;
                    text-align:center;border:1px solid #d1d5db;
                    border-radius:8px;margin-bottom:14px;
                    font-family:ui-monospace,SFMono-Regular,Menlo,monospace;'>
      <button type='submit'
              style='display:block;width:100%;padding:12px;font-size:14px;
                     color:#fff;background:#0f3460;border:none;border-radius:8px;
                     font-weight:600;cursor:pointer;'>
        登录
      </button>
    </form>
    <form method='post' action='/auth/request-code' style='margin:12px 0 0;'>
      <input type='hidden' name='next' value='{next_safe}'>
      <input type='hidden' name='email' value='{email_safe}'>
      <button type='submit'
              style='display:block;width:100%;padding:8px;font-size:12px;
                     color:#666;background:transparent;border:none;
                     cursor:pointer;text-decoration:underline;'>
        重新发送验证码
      </button>
    </form>"""
    else:
        # Step 1: email entry → triggers code send.
        code_form = f"""
    <form method='post' action='/auth/request-code' style='margin:0;'>
      <input type='hidden' name='next' value='{next_safe}'>
      <input type='email' name='email' required autocomplete='email'
             placeholder='your@email.com'
             {"autofocus" if active_tab == "code" else ""}
             value='{email_safe}'
             style='display:block;width:100%;box-sizing:border-box;
                    padding:12px 14px;font-size:14px;border:1px solid #d1d5db;
                    border-radius:8px;margin-bottom:14px;'>
      <button type='submit'
              style='display:block;width:100%;padding:12px;font-size:14px;
                     color:#fff;background:#0f3460;border:none;border-radius:8px;
                     font-weight:600;cursor:pointer;'>
        发送验证码
      </button>
    </form>
    <p style='margin:16px 0 0;font-size:12px;color:#888;line-height:1.5;'>
      6 位数字验证码, {config.LOGIN_CODE_TTL_MINUTES} 分钟内有效。
    </p>"""

    active_form = code_form if active_tab == "code" else link_form
    active_sent_panel = code_sent_panel if active_tab == "code" else link_sent_panel

    return f"""<!DOCTYPE html>
<html lang='zh'><head>
  <meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>
  <title>登录 · 看牛韵新闻</title>
</head>
<body style='font-family:-apple-system,BlinkMacSystemFont,sans-serif;
             background:#f4f6fb;margin:0;padding:48px 16px;color:#222;'>
  <div style='max-width:440px;margin:auto;background:#fff;
              border-radius:12px;box-shadow:0 4px 24px rgba(0,0,0,.06);
              padding:32px;'>
    <h1 style='margin:0 0 8px;font-size:22px;color:#0f3460;'>看牛韵新闻</h1>
    <p style='margin:0 0 24px;font-size:14px;color:#666;line-height:1.6;'>
      会员专享:AI 板块轮动 dashboard、关键词智能匹配新闻、未来研究工具。
      输入邮箱即可登录,无需密码。
    </p>
    {tabs_html}
    {active_sent_panel}
    {err_panel}
    {active_form}
    <p style='margin:24px 0 0;font-size:12px;color:#999;line-height:1.5;
              border-top:1px solid #eee;padding-top:16px;'>
      仅限受邀订阅者。如未收到邮件,请确认邮箱拼写或联系管理员。
    </p>
  </div>
</body></html>"""


@app.post("/auth/request-link")
def request_magic_link(
    request: Request,
    email: str = Form(...),
    next: str = Form("/"),
):
    """Issue a one-time login URL to the given email — if it's on the allowlist.

    Always returns the same response regardless of whether the email exists,
    to prevent attackers from probing the subscriber list (email enumeration).
    """
    email = (email or "").strip().lower()
    sub = subscribers.get_by_email(email) if email else None
    if sub and sub.status == "active":
        try:
            token = subscribers.create_magic_link(email)
            auth.send_magic_link_email(email, token, next_path=next or "/")
        except Exception as e:
            logger.error("Failed to send magic link to %s: %s", email, e)
            # Still return the same success-looking response below
    else:
        # Log only — don't expose to caller
        logger.info("Magic link requested for unknown/inactive email: %s", email)
    return _redirect(f"/login?sent=1&next={next or '/'}")


@app.get("/auth/verify")
def verify_magic_link(request: Request, token: str, next: str = "/"):
    """Consume a one-time token, create a session, set cookie, redirect."""
    email = subscribers.consume_magic_link(token)
    if not email:
        return _redirect("/login?err=" + "链接无效或已过期，请重新申请")
    sub = subscribers.get_by_email(email)
    if not sub or sub.status != "active":
        return _redirect("/login?err=" + "账号不存在或已暂停")
    session_id = subscribers.create_session(sub.id)
    target = next if (next and next.startswith("/")) else "/"
    response = _redirect(target)
    _set_session_cookie(response, session_id)
    return response


@app.post("/auth/request-code")
def request_login_code(
    request: Request,
    email: str = Form(...),
    next: str = Form("/"),
):
    """Issue a 6-digit verification code to the given email.

    Anti-enumeration: always redirect to the verify step regardless of
    whether the email exists, is inactive, or is in cooldown. The
    `email` arg is echoed back in the URL so the verify form is pre-filled.
    """
    from urllib.parse import quote
    email = (email or "").strip().lower()
    sub = subscribers.get_by_email(email) if email else None
    if sub and sub.status == "active":
        try:
            code = subscribers.create_login_code(email)
            auth.send_login_code_email(email, code)
        except subscribers.LoginCodeCooldownError:
            # Treat as success from the caller's perspective; the existing
            # code is still valid, user should check their inbox.
            logger.info("Login code cooldown for %s — skipping resend", email)
        except Exception as e:
            logger.error("Failed to send login code to %s: %s", email, e)
    else:
        logger.info("Login code requested for unknown/inactive email: %s", email)
    next_q = f"&next={quote(next)}" if next and next != "/" else ""
    return _redirect(f"/login?tab=code&email={quote(email)}&sent=1{next_q}")


@app.post("/auth/verify-code")
def verify_login_code(
    request: Request,
    email: str = Form(...),
    code: str = Form(...),
    next: str = Form("/"),
):
    """Validate a submitted code, issue a session, set cookie, redirect."""
    from urllib.parse import quote
    email = (email or "").strip().lower()
    verified_email = subscribers.consume_login_code(email, code)
    if not verified_email:
        next_q = f"&next={quote(next)}" if next and next != "/" else ""
        return _redirect(
            f"/login?tab=code&email={quote(email)}&sent=1{next_q}"
            f"&err=验证码错误或已过期，请重试"
        )
    sub = subscribers.get_by_email(verified_email)
    if not sub or sub.status != "active":
        return _redirect("/login?err=" + "账号不存在或已暂停")
    session_id = subscribers.create_session(sub.id)
    target = next if (next and next.startswith("/")) else "/"
    response = _redirect(target)
    _set_session_cookie(response, session_id)
    return response


@app.post("/auth/logout")
def logout(request: Request):
    sid = request.cookies.get(config.SESSION_COOKIE_NAME)
    subscribers.expire_session(sid)
    response = _redirect("/login")
    _clear_session_cookie(response)
    return response


@app.get("/account", response_class=HTMLResponse)
def account_page(request: Request, paywall: int = 0,
                 sub=Depends(auth.require_subscriber)):
    """Logged-in user's status. `paywall=1` shows an upgrade banner."""
    import html as _h
    paid = subscribers.is_paid(sub)
    tier_badge = (
        "<span style='background:#dcfce7;color:#166534;padding:2px 10px;"
        "border-radius:999px;font-size:12px;font-weight:600;'>付费会员</span>"
        if paid else
        "<span style='background:#fef3c7;color:#92400e;padding:2px 10px;"
        "border-radius:999px;font-size:12px;font-weight:600;'>免费用户</span>"
    )
    paywall_panel = (
        """<div style='background:#fef3c7;border:1px solid #fcd34d;color:#92400e;
              padding:14px 18px;border-radius:8px;margin-bottom:20px;font-size:14px;
              line-height:1.6;'>
            🔒 该页面仅对付费会员开放。如需开通,请联系管理员。
          </div>"""
        if paywall and not paid else ""
    )
    paid_until = sub.paid_until or "—(无到期)"
    name_line = (
        f"<div style='font-size:13px;color:#888;margin-top:4px;'>{_h.escape(sub.name)}</div>"
        if sub.name else ""
    )

    return f"""<!DOCTYPE html>
<html lang='zh'><head>
  <meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>
  <title>账号 · 看牛韵新闻</title>
</head>
<body style='font-family:-apple-system,BlinkMacSystemFont,sans-serif;
             background:#f4f6fb;margin:0;padding:48px 16px;color:#222;'>
  <div style='max-width:560px;margin:auto;background:#fff;
              border-radius:12px;box-shadow:0 4px 24px rgba(0,0,0,.06);
              padding:32px;'>
    <h1 style='margin:0 0 24px;font-size:22px;color:#0f3460;'>我的账号</h1>
    {paywall_panel}
    <div style='border:1px solid #e5e7eb;border-radius:8px;padding:20px;
                margin-bottom:24px;'>
      <div style='font-size:11px;color:#888;text-transform:uppercase;
                  letter-spacing:.5px;margin-bottom:6px;'>邮箱</div>
      <div style='font-size:16px;font-weight:600;color:#222;'>{_h.escape(sub.email)}</div>
      {name_line}
    </div>
    <div style='display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:24px;'>
      <div style='border:1px solid #e5e7eb;border-radius:8px;padding:16px;'>
        <div style='font-size:11px;color:#888;text-transform:uppercase;
                    letter-spacing:.5px;margin-bottom:8px;'>等级</div>
        {tier_badge}
      </div>
      <div style='border:1px solid #e5e7eb;border-radius:8px;padding:16px;'>
        <div style='font-size:11px;color:#888;text-transform:uppercase;
                    letter-spacing:.5px;margin-bottom:8px;'>到期</div>
        <div style='font-size:13px;color:#374151;'>{_h.escape(paid_until)}</div>
      </div>
    </div>
    <div style='display:flex;gap:12px;'>
      <a href='/' style='flex:1;text-align:center;padding:10px;
         font-size:14px;color:#0f3460;background:#eef2f8;border-radius:8px;
         text-decoration:none;font-weight:500;'>返回首页</a>
      <form method='post' action='/auth/logout' style='flex:1;margin:0;'>
        <button type='submit'
                style='width:100%;padding:10px;font-size:14px;color:#991b1b;
                       background:#fee2e2;border:none;border-radius:8px;
                       font-weight:500;cursor:pointer;'>
          登出
        </button>
      </form>
    </div>
  </div>
</body></html>"""


