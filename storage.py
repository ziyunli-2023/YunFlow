"""SQLite storage with deduplication."""

import hashlib
import sqlite3
from datetime import datetime
from contextlib import contextmanager
import config


@contextmanager
def get_conn():
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _migrate(conn):
    """Add columns introduced after initial schema."""
    for ddl in (
        "ALTER TABLE blog_posts ADD COLUMN category TEXT",
        "ALTER TABLE tweets ADD COLUMN text_zh TEXT",
        # Paper-tracking fields (Phase 1: technical reports + trending papers)
        "ALTER TABLE blog_posts ADD COLUMN is_paper INTEGER DEFAULT 0",
        "ALTER TABLE blog_posts ADD COLUMN arxiv_id TEXT",
        "ALTER TABLE blog_posts ADD COLUMN hf_paper_id TEXT",
        "ALTER TABLE blog_posts ADD COLUMN hf_upvotes INTEGER DEFAULT 0",
        "ALTER TABLE blog_posts ADD COLUMN hn_score INTEGER DEFAULT 0",
        "ALTER TABLE blog_posts ADD COLUMN authors TEXT",
        "ALTER TABLE blog_posts ADD COLUMN pdf_url TEXT",
        "ALTER TABLE blog_posts ADD COLUMN paper_score REAL DEFAULT 0",
        "ALTER TABLE page_visits ADD COLUMN week TEXT",
        "ALTER TABLE subscribers ADD COLUMN stripe_customer_id TEXT",
        "ALTER TABLE subscribers ADD COLUMN stripe_subscription_id TEXT",
        "ALTER TABLE subscribers ADD COLUMN stripe_subscription_status TEXT",
        "ALTER TABLE subscribers ADD COLUMN stripe_current_period_end TEXT",
        "ALTER TABLE subscribers ADD COLUMN stripe_cancel_at_period_end INTEGER DEFAULT 0",
        "ALTER TABLE subscribers ADD COLUMN stripe_cancel_at TEXT",
    ):
        try:
            conn.execute(ddl)
            conn.commit()
        except Exception:
            pass  # column already exists


def init_db():
    with get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS tweets (
                id            TEXT PRIMARY KEY,
                username      TEXT NOT NULL,
                name          TEXT,
                text          TEXT NOT NULL,
                created_at    TEXT NOT NULL,
                url           TEXT,
                likes         INTEGER DEFAULT 0,
                retweets      INTEGER DEFAULT 0,
                reply_count   INTEGER DEFAULT 0,
                lang          TEXT,
                priority_rank INTEGER DEFAULT 2,
                category      TEXT,
                fetched_at    TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS blog_posts (
                id            TEXT PRIMARY KEY,
                source        TEXT NOT NULL,
                title         TEXT NOT NULL,
                url           TEXT NOT NULL,
                summary       TEXT,
                published     TEXT,
                feed_priority INTEGER DEFAULT 2,
                content_hash  TEXT,
                title_zh      TEXT,
                summary_zh    TEXT,
                category      TEXT,
                fetched_at    TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS x_cursors (
                user_id    TEXT PRIMARY KEY,
                username   TEXT NOT NULL,
                since_id   TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS feed_health (
                source               TEXT PRIMARY KEY,
                last_success         TEXT,
                last_error           TEXT,
                consecutive_failures INTEGER DEFAULT 0
            );

            CREATE INDEX IF NOT EXISTS idx_tweets_created   ON tweets(created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_tweets_user      ON tweets(username);
            CREATE INDEX IF NOT EXISTS idx_tweets_priority  ON tweets(priority_rank);
            CREATE INDEX IF NOT EXISTS idx_tweets_category  ON tweets(category);
            CREATE INDEX IF NOT EXISTS idx_posts_published  ON blog_posts(published DESC);
            CREATE INDEX IF NOT EXISTS idx_posts_source     ON blog_posts(source);
            CREATE INDEX IF NOT EXISTS idx_posts_priority   ON blog_posts(feed_priority);
            CREATE INDEX IF NOT EXISTS idx_posts_hash       ON blog_posts(content_hash);
        """)
        _migrate(conn)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_posts_category ON blog_posts(category)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_posts_paper    ON blog_posts(is_paper, paper_score DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_posts_arxiv    ON blog_posts(arxiv_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_posts_hf_paper ON blog_posts(hf_paper_id)")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS digest_log (
                date    TEXT NOT NULL,
                hour    INTEGER NOT NULL,
                sent_at TEXT NOT NULL,
                PRIMARY KEY (date, hour)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS page_visits (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                visited_at TEXT NOT NULL,
                date       TEXT NOT NULL,
                week       TEXT,
                month      TEXT NOT NULL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_visits_date  ON page_visits(date)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_visits_month ON page_visits(month)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_visits_week  ON page_visits(week)")

        # ── Earnings calendar tables ───────────────────────────────────────
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS earnings_events (
                id            TEXT PRIMARY KEY,
                symbol        TEXT NOT NULL,
                date          TEXT NOT NULL,
                hour          TEXT,
                eps_estimate  REAL,
                eps_actual    REAL,
                rev_estimate  REAL,
                rev_actual    REAL,
                quarter       INTEGER,
                year          INTEGER,
                fetched_at    TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_earnings_date   ON earnings_events(date);
            CREATE INDEX IF NOT EXISTS idx_earnings_symbol ON earnings_events(symbol);

            CREATE TABLE IF NOT EXISTS ipo_events (
                id            TEXT PRIMARY KEY,
                symbol        TEXT,
                name          TEXT NOT NULL,
                date          TEXT NOT NULL,
                exchange      TEXT,
                price_range   TEXT,
                shares        INTEGER,
                total_value   REAL,
                status        TEXT,
                fetched_at    TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_ipo_date ON ipo_events(date);

            CREATE TABLE IF NOT EXISTS stock_profiles (
                symbol        TEXT PRIMARY KEY,
                name          TEXT,
                market_cap_m  REAL,
                industry      TEXT,
                exchange      TEXT,
                logo          TEXT,
                weburl        TEXT,
                fetched_at    TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS macro_events (
                id            TEXT PRIMARY KEY,
                date          TEXT NOT NULL,
                time          TEXT,
                title         TEXT NOT NULL,
                category      TEXT,
                impact        TEXT,
                notes         TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_macro_date ON macro_events(date);
        """)

        # ── Subscription system ────────────────────────────────────────────
        # subscribers: who can receive digests / access gated pages
        # magic_links: short-lived one-time login tokens (email-based, no password)
        # sessions:    long-lived cookie-keyed sessions issued after Magic Link verify
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS subscribers (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                email        TEXT UNIQUE NOT NULL,
                name         TEXT,
                status       TEXT NOT NULL DEFAULT 'active',
                tier         TEXT NOT NULL DEFAULT 'free',
                paid_until   TEXT,
                stripe_customer_id TEXT,
                stripe_subscription_id TEXT,
                stripe_subscription_status TEXT,
                stripe_current_period_end TEXT,
                stripe_cancel_at_period_end INTEGER DEFAULT 0,
                stripe_cancel_at TEXT,
                preferences  TEXT,
                created_at   TEXT NOT NULL,
                updated_at   TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_subscribers_status ON subscribers(status, tier);
            CREATE INDEX IF NOT EXISTS idx_subscribers_stripe_customer ON subscribers(stripe_customer_id);
            CREATE INDEX IF NOT EXISTS idx_subscribers_stripe_subscription ON subscribers(stripe_subscription_id);

            CREATE TABLE IF NOT EXISTS magic_links (
                token        TEXT PRIMARY KEY,
                email        TEXT NOT NULL,
                created_at   TEXT NOT NULL,
                expires_at   TEXT NOT NULL,
                used_at      TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_magic_links_email ON magic_links(email);

            CREATE TABLE IF NOT EXISTS sessions (
                id             TEXT PRIMARY KEY,
                subscriber_id  INTEGER NOT NULL,
                created_at     TEXT NOT NULL,
                expires_at     TEXT NOT NULL,
                last_seen      TEXT NOT NULL,
                FOREIGN KEY (subscriber_id) REFERENCES subscribers(id)
            );
            CREATE INDEX IF NOT EXISTS idx_sessions_sub ON sessions(subscriber_id);

            -- Email-based 6-digit verification codes (alternative to magic_links).
            -- code_hash is HMAC-SHA256(code, LOGIN_CODE_HMAC_KEY) hex — never store
            -- the raw code. attempts counts wrong submissions; once it reaches
            -- LOGIN_CODE_MAX_ATTEMPTS the row is invalidated.
            CREATE TABLE IF NOT EXISTS login_codes (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                email        TEXT NOT NULL,
                code_hash    TEXT NOT NULL,
                created_at   TEXT NOT NULL,
                expires_at   TEXT NOT NULL,
                used_at      TEXT,
                attempts     INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_login_codes_email ON login_codes(email, created_at);

            CREATE TABLE IF NOT EXISTS stripe_events (
                event_id     TEXT PRIMARY KEY,
                event_type   TEXT NOT NULL,
                processed_at TEXT NOT NULL
            );
        """)


def _content_hash(title: str) -> str:
    """Normalized title hash for cross-source deduplication."""
    import re
    normalized = re.sub(r"[^a-z0-9 ]", "", title.lower()).strip()
    return hashlib.sha256(normalized.encode()).hexdigest()


# ── Cursor persistence ─────────────────────────────────────────────────────

def load_cursors() -> dict[str, str]:
    """Load {user_id: since_id} from DB."""
    with get_conn() as conn:
        rows = conn.execute("SELECT user_id, since_id FROM x_cursors").fetchall()
        return {r["user_id"]: r["since_id"] for r in rows}


def save_cursor(user_id: str, username: str, since_id: str):
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO x_cursors (user_id, username, since_id, updated_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(user_id) DO UPDATE SET
                   since_id=excluded.since_id,
                   updated_at=excluded.updated_at""",
            (user_id, username, since_id, datetime.utcnow().isoformat()),
        )


# ── Feed health ────────────────────────────────────────────────────────────

def record_feed_success(source: str):
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO feed_health (source, last_success, consecutive_failures)
               VALUES (?, ?, 0)
               ON CONFLICT(source) DO UPDATE SET
                   last_success=excluded.last_success,
                   consecutive_failures=0""",
            (source, datetime.utcnow().isoformat()),
        )


def record_feed_error(source: str, error: str):
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO feed_health (source, last_error, consecutive_failures)
               VALUES (?, ?, 1)
               ON CONFLICT(source) DO UPDATE SET
                   last_error=excluded.last_error,
                   consecutive_failures=consecutive_failures+1""",
            (source, str(error)[:500]),
        )


def get_feed_health() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM feed_health ORDER BY consecutive_failures DESC").fetchall()
        return [dict(r) for r in rows]


# ── Tweets ─────────────────────────────────────────────────────────────────

def save_tweet(tweet: dict) -> bool:
    """Insert new tweet or update engagement counts. Returns True if new."""
    with get_conn() as conn:
        existing = conn.execute("SELECT id FROM tweets WHERE id=?", (tweet["id"],)).fetchone()
        if existing:
            # Update engagement counts only
            conn.execute(
                "UPDATE tweets SET likes=?, retweets=?, reply_count=? WHERE id=?",
                (tweet.get("likes", 0), tweet.get("retweets", 0), tweet.get("reply_count", 0), tweet["id"]),
            )
            return False
        conn.execute(
            """INSERT INTO tweets
               (id, username, name, text, created_at, url,
                likes, retweets, reply_count, lang, priority_rank, category, fetched_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                tweet["id"],
                tweet["username"],
                tweet.get("name", ""),
                tweet["text"],
                tweet["created_at"],
                tweet.get("url", ""),
                tweet.get("likes", 0),
                tweet.get("retweets", 0),
                tweet.get("reply_count", 0),
                tweet.get("lang"),
                tweet.get("priority_rank", 2),
                tweet.get("category"),
                datetime.utcnow().isoformat(),
            ),
        )
        return True


# ── Blog posts ─────────────────────────────────────────────────────────────

def update_tweet_translation(tweet_id: str, text_zh: str):
    with get_conn() as conn:
        conn.execute(
            "UPDATE tweets SET text_zh=? WHERE id=?",
            (text_zh, tweet_id),
        )


def update_post_translation(post_id: str, title_zh: str, summary_zh: str):
    with get_conn() as conn:
        conn.execute(
            "UPDATE blog_posts SET title_zh=?, summary_zh=? WHERE id=?",
            (title_zh, summary_zh, post_id),
        )


def get_untranslated_posts(limit: int = 10) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM blog_posts WHERE title_zh IS NULL ORDER BY fetched_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


def save_post(post: dict) -> bool:
    """Insert new post. Returns True if new (not a duplicate by id or content_hash).

    For papers, if a duplicate exists by content_hash (e.g. same paper picked up
    by both arXiv RSS and HF Daily Papers), enrich the existing row with the
    paper-specific fields rather than dropping the data.
    """
    ch = _content_hash(post["title"])
    with get_conn() as conn:
        dup = conn.execute(
            "SELECT id FROM blog_posts WHERE content_hash=?", (ch,)
        ).fetchone()
        if dup:
            if post.get("is_paper"):
                _enrich_paper_fields(conn, dup["id"], post)
            return False
        try:
            conn.execute(
                """INSERT INTO blog_posts
                   (id, source, title, url, summary, published, feed_priority, content_hash, category, fetched_at,
                    is_paper, arxiv_id, hf_paper_id, hf_upvotes, hn_score, authors, pdf_url, paper_score)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    post["id"],
                    post["source"],
                    post["title"],
                    post["url"],
                    post.get("summary", ""),
                    post.get("published", ""),
                    post.get("feed_priority", 2),
                    ch,
                    post.get("category", "ai"),
                    datetime.utcnow().isoformat(),
                    1 if post.get("is_paper") else 0,
                    post.get("arxiv_id"),
                    post.get("hf_paper_id"),
                    int(post.get("hf_upvotes") or 0),
                    int(post.get("hn_score") or 0),
                    post.get("authors"),
                    post.get("pdf_url"),
                    float(post.get("paper_score") or 0.0),
                ),
            )
            return True
        except sqlite3.IntegrityError:
            return False


def _enrich_paper_fields(conn, row_id: str, post: dict) -> None:
    """Backfill paper-specific fields on an existing row when a duplicate paper
    arrives from a different source (e.g. arXiv RSS landed first, then HF Daily)."""
    conn.execute(
        """UPDATE blog_posts SET
               is_paper    = COALESCE(NULLIF(is_paper,0), 1),
               arxiv_id    = COALESCE(arxiv_id, ?),
               hf_paper_id = COALESCE(hf_paper_id, ?),
               hf_upvotes  = MAX(COALESCE(hf_upvotes,0), ?),
               authors     = COALESCE(authors, ?),
               pdf_url     = COALESCE(pdf_url, ?),
               paper_score = MAX(COALESCE(paper_score,0), ?)
           WHERE id = ?""",
        (
            post.get("arxiv_id"),
            post.get("hf_paper_id"),
            int(post.get("hf_upvotes") or 0),
            post.get("authors"),
            post.get("pdf_url"),
            float(post.get("paper_score") or 0.0),
            row_id,
        ),
    )


def update_paper_metrics(post_id: str, hf_upvotes: int = None,
                         hn_score: int = None, paper_score: float = None) -> None:
    """Refresh trending signals on an existing paper row."""
    sets, vals = [], []
    if hf_upvotes is not None:
        sets.append("hf_upvotes=?"); vals.append(int(hf_upvotes))
    if hn_score is not None:
        sets.append("hn_score=?"); vals.append(int(hn_score))
    if paper_score is not None:
        sets.append("paper_score=?"); vals.append(float(paper_score))
    if not sets:
        return
    vals.append(post_id)
    with get_conn() as conn:
        conn.execute(f"UPDATE blog_posts SET {', '.join(sets)} WHERE id=?", vals)


def get_papers_for_refresh(hours: int = 72) -> list[dict]:
    """Return recently-fetched papers whose upvotes should be re-polled.

    Includes title/summary/authors so the score recompute can re-evaluate
    tier (大模型公司 vs 实验室) bonuses.
    """
    cutoff = datetime.utcfromtimestamp(datetime.utcnow().timestamp() - hours * 3600).isoformat()
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT id, hf_paper_id, arxiv_id, hf_upvotes, hn_score,
                      published, fetched_at, source, title, summary, authors
               FROM blog_posts
               WHERE is_paper=1 AND fetched_at >= ?""",
            (cutoff,),
        ).fetchall()
        return [dict(r) for r in rows]


def get_trending_papers(hours: int = 72, limit: int = 10, min_score: float = 0.0) -> list[dict]:
    """Top papers by paper_score within recent window."""
    cutoff = datetime.utcfromtimestamp(datetime.utcnow().timestamp() - hours * 3600).isoformat()
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT * FROM blog_posts
               WHERE is_paper=1
                 AND (published >= ? OR fetched_at >= ?)
                 AND paper_score >= ?
               ORDER BY paper_score DESC, hf_upvotes DESC
               LIMIT ?""",
            (cutoff, cutoff, min_score, limit),
        ).fetchall()
        return [dict(r) for r in rows]


def get_papers_by_lab(lab_label: str, limit: int = 20) -> list[dict]:
    """Papers whose source matches a lab label (e.g. 'DeepSeek 技术报告')."""
    like = f"%{lab_label}%"
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT * FROM blog_posts
               WHERE is_paper=1 AND source LIKE ?
               ORDER BY published DESC
               LIMIT ?""",
            (like, limit),
        ).fetchall()
        return [dict(r) for r in rows]


def find_paper_by_arxiv_id(arxiv_id: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM blog_posts WHERE arxiv_id=? LIMIT 1", (arxiv_id,)
        ).fetchone()
        return dict(row) if row else None


# ── Queries ────────────────────────────────────────────────────────────────

def get_latest_tweets(limit: int = 20, username: str = None, category: str = None) -> list[dict]:
    with get_conn() as conn:
        if username:
            rows = conn.execute(
                "SELECT * FROM tweets WHERE username=? ORDER BY created_at DESC LIMIT ?",
                (username, limit),
            ).fetchall()
        elif category:
            rows = conn.execute(
                "SELECT * FROM tweets WHERE category=? ORDER BY created_at DESC LIMIT ?",
                (category, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM tweets ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]


def get_latest_posts_by_category(category: str, limit: int = 30) -> list[dict]:
    with get_conn() as conn:
        fetch_limit = limit * 3 if category == "polymarket" else limit
        rows = conn.execute(
            "SELECT * FROM blog_posts WHERE category=? ORDER BY fetched_at DESC LIMIT ?",
            (category, fetch_limit),
        ).fetchall()
        posts = [dict(r) for r in rows]
        if category == "polymarket" and posts:
            from polymarket_monitor import _global_score, _topic_cluster, _BLOCKED_TOPICS
            posts = [p for p in posts if not any(b in p["title"].lower() for b in _BLOCKED_TOPICS)]
            scored = [(p, _global_score(p["title"], 0)) for p in posts]
            scored = [(p, s) for p, s in scored if s > 0]
            scored.sort(key=lambda x: x[1], reverse=True)
            cluster_counts: dict[str, int] = {}
            deduped = []
            for p, s in scored:
                c = _topic_cluster(p["title"])
                if cluster_counts.get(c, 0) >= 2:
                    continue
                cluster_counts[c] = cluster_counts.get(c, 0) + 1
                deduped.append(p)
            posts = deduped[:limit]
        return posts


def get_latest_posts(limit: int = 20, source: str = None) -> list[dict]:
    with get_conn() as conn:
        if source:
            rows = conn.execute(
                "SELECT * FROM blog_posts WHERE source=? ORDER BY published DESC LIMIT ?",
                (source, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM blog_posts ORDER BY published DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]


def get_top_tweets(hours: int = 48, limit: int = 10) -> list[dict]:
    cutoff = (datetime.utcnow().timestamp() - hours * 3600)
    from datetime import timezone
    cutoff_iso = datetime.utcfromtimestamp(cutoff).isoformat()
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT * FROM tweets
               WHERE created_at >= ?
               ORDER BY (likes + retweets * 2) DESC
               LIMIT ?""",
            (cutoff_iso, limit),
        ).fetchall()
        return [dict(r) for r in rows]


def get_top_posts(hours: int = 48, limit: int = 10) -> list[dict]:
    cutoff = (datetime.utcnow().timestamp() - hours * 3600)
    cutoff_iso = datetime.utcfromtimestamp(cutoff).isoformat()
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT * FROM blog_posts
               WHERE published >= ? OR published IS NULL
               ORDER BY feed_priority ASC, published DESC
               LIMIT ?""",
            (cutoff_iso, limit),
        ).fetchall()
        return [dict(r) for r in rows]


def search_posts_any_keyword(keywords: list[str], limit: int = 10, days: int = 60) -> list[dict]:
    """Return blog_posts whose title OR summary matches ANY of the keywords.

    Two-stage matcher:
      1. SQL LIKE pre-filters candidates by date range.
      2. Python regex post-filters with WORD-BOUNDARY for short ASCII tokens
         (≤4 chars, e.g. PPI/CPI/GDP/Fed/NVDA), so 'PPI' no longer matches
         the 'ppi' substring inside 'shipping' / 'mapping'. Longer tokens
         and Chinese words use plain substring matching.
    """
    import re
    # Clean + dedupe (case-insensitive)
    cleaned: list[str] = []
    seen_low: set[str] = set()
    for k in (keywords or []):
        k = (k or "").strip()
        if not k or len(k) < 2:
            continue
        kl = k.lower()
        if kl in seen_low:
            continue
        seen_low.add(kl)
        cleaned.append(k)
    if not cleaned:
        return []

    # Build the strict matcher
    pat_parts: list[str] = []
    for k in cleaned:
        is_ascii = all(ord(c) < 128 for c in k)
        if is_ascii and len(k) <= 4:
            pat_parts.append(r"\b" + re.escape(k) + r"\b")
        else:
            pat_parts.append(re.escape(k))
    matcher = re.compile("|".join(pat_parts), re.IGNORECASE)

    # SQL pre-filter (permissive — exact filtering happens in Python)
    where_parts = []
    params: list = []
    for k in cleaned:
        like = f"%{k}%"
        where_parts.append("(title LIKE ? OR summary LIKE ?)")
        params.extend([like, like])
    from datetime import timedelta
    cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()
    params.append(cutoff)
    sql = f"""SELECT * FROM blog_posts
              WHERE ({' OR '.join(where_parts)})
                AND COALESCE(published, fetched_at) >= ?
              ORDER BY COALESCE(published, fetched_at) DESC
              LIMIT 300"""
    with get_conn() as conn:
        rows = conn.execute(sql, params).fetchall()

    out: list[dict] = []
    seen: set[str] = set()
    for r in rows:
        if r["id"] in seen:
            continue
        text = (r["title"] or "") + "\n" + (r["summary"] or "")
        if not matcher.search(text):
            continue
        seen.add(r["id"])
        out.append(dict(r))
        if len(out) >= limit:
            break
    return out


def search_news(query: str, limit: int = 20, source_type: str = "all") -> list[dict]:
    like = f"%{query}%"
    results = []
    with get_conn() as conn:
        if source_type in ("all", "tweets"):
            rows = conn.execute(
                "SELECT *, 'tweet' as item_type FROM tweets WHERE text LIKE ? ORDER BY created_at DESC LIMIT ?",
                (like, limit),
            ).fetchall()
            results.extend([dict(r) for r in rows])
        if source_type in ("all", "posts"):
            rows = conn.execute(
                "SELECT *, 'post' as item_type FROM blog_posts WHERE title LIKE ? OR summary LIKE ? ORDER BY published DESC LIMIT ?",
                (like, like, limit),
            ).fetchall()
            results.extend([dict(r) for r in rows])
    return results[:limit]


# ── Digest send log ────────────────────────────────────────────────────────

def was_digest_sent(date_str: str, hour: int) -> bool:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM digest_log WHERE date=? AND hour=?", (date_str, hour)
        ).fetchone()
        return row is not None


def record_digest_sent(date_str: str, hour: int):
    with get_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO digest_log (date, hour, sent_at) VALUES (?, ?, ?)",
            (date_str, hour, datetime.utcnow().isoformat()),
        )


def get_recent_posts_by_category(hours: int = 24, limit_per_category: int = 10) -> dict[str, list[dict]]:
    """Return recent posts grouped by category for the daily briefing."""
    cutoff = datetime.utcfromtimestamp(datetime.utcnow().timestamp() - hours * 3600).isoformat()
    categories = ["polymarket", "venture", "us_stock", "trump", "geopolitics", "ai", "papers", "web3"]
    result = {}
    with get_conn() as conn:
        for cat in categories:
            rows = conn.execute(
                """SELECT * FROM blog_posts
                   WHERE category=? AND (published >= ? OR fetched_at >= ?)
                   ORDER BY published DESC LIMIT ?""",
                (cat, cutoff, cutoff, limit_per_category * 3),
            ).fetchall()
            posts = [dict(r) for r in rows]
            # For trump, also mix in tweets (nitter / X) normalised as post-like dicts
            if cat == "trump":
                tweet_rows = conn.execute(
                    """SELECT * FROM tweets WHERE category='trump' AND created_at >= ?
                       ORDER BY created_at DESC LIMIT ?""",
                    (cutoff, limit_per_category),
                ).fetchall()
                for t in tweet_rows:
                    td = dict(t)
                    posts.append({
                        "id":       td["id"],
                        "title":    td["text"][:280],
                        "source":   "@" + td["username"],
                        "url":      td.get("url", ""),
                        "published": td["created_at"],
                        "category": "trump",
                        "summary":  td["text"],
                        "is_tweet": True,
                    })
                posts.sort(key=lambda x: x.get("published", ""), reverse=True)
            if cat == "polymarket" and posts:
                from polymarket_monitor import _global_score, _topic_cluster, _BLOCKED_TOPICS
                posts = [p for p in posts if not any(b in p["title"].lower() for b in _BLOCKED_TOPICS)]
                scored = [(p, _global_score(p["title"], 0)) for p in posts]
                scored = [(p, s) for p, s in scored if s > 0]
                scored.sort(key=lambda x: x[1], reverse=True)
                # Topic dedup: max 2 per cluster
                cluster_counts: dict[str, int] = {}
                deduped = []
                for p, s in scored:
                    c = _topic_cluster(p["title"])
                    if cluster_counts.get(c, 0) >= 2:
                        continue
                    cluster_counts[c] = cluster_counts.get(c, 0) + 1
                    deduped.append(p)
                posts = deduped
            result[cat] = posts[:limit_per_category * 3]
    return result


def record_visit():
    now = datetime.utcnow()
    iso = now.isocalendar()
    week_str = f"{iso[0]}-W{iso[1]:02d}"
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO page_visits (visited_at, date, week, month) VALUES (?, ?, ?, ?)",
            (now.isoformat(), now.strftime("%Y-%m-%d"), week_str, now.strftime("%Y-%m")),
        )


def get_visit_stats() -> dict:
    now = datetime.utcnow()
    today = now.strftime("%Y-%m-%d")
    month = now.strftime("%Y-%m")
    iso = now.isocalendar()
    week_str = f"{iso[0]}-W{iso[1]:02d}"
    with get_conn() as conn:
        total = conn.execute("SELECT COUNT(*) FROM page_visits").fetchone()[0]
        today_count = conn.execute(
            "SELECT COUNT(*) FROM page_visits WHERE date=?", (today,)
        ).fetchone()[0]
        week_count = conn.execute(
            "SELECT COUNT(*) FROM page_visits WHERE week=?", (week_str,)
        ).fetchone()[0]
        month_count = conn.execute(
            "SELECT COUNT(*) FROM page_visits WHERE month=?", (month,)
        ).fetchone()[0]
    return {"total": total, "today": today_count, "this_week": week_count, "this_month": month_count}


def get_stats() -> dict:
    with get_conn() as conn:
        tweet_count = conn.execute("SELECT COUNT(*) FROM tweets").fetchone()[0]
        post_count  = conn.execute("SELECT COUNT(*) FROM blog_posts").fetchone()[0]
        latest_tweet = conn.execute(
            "SELECT created_at FROM tweets ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        latest_post = conn.execute(
            "SELECT published FROM blog_posts ORDER BY published DESC LIMIT 1"
        ).fetchone()
        return {
            "tweet_count": tweet_count,
            "post_count": post_count,
            "latest_tweet_at": latest_tweet[0] if latest_tweet else None,
            "latest_post_at": latest_post[0] if latest_post else None,
        }


# ── Earnings calendar ──────────────────────────────────────────────────────

def upsert_earnings(rows: list[dict]) -> int:
    if not rows:
        return 0
    now = datetime.utcnow().isoformat()
    with get_conn() as conn:
        for r in rows:
            conn.execute(
                """INSERT INTO earnings_events
                   (id, symbol, date, hour, eps_estimate, eps_actual,
                    rev_estimate, rev_actual, quarter, year, fetched_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET
                       hour=excluded.hour,
                       eps_estimate=excluded.eps_estimate,
                       eps_actual=excluded.eps_actual,
                       rev_estimate=excluded.rev_estimate,
                       rev_actual=excluded.rev_actual,
                       fetched_at=excluded.fetched_at""",
                (
                    r["id"], r["symbol"], r["date"], r.get("hour"),
                    r.get("eps_estimate"), r.get("eps_actual"),
                    r.get("rev_estimate"), r.get("rev_actual"),
                    r.get("quarter"), r.get("year"), now,
                ),
            )
    return len(rows)


def upsert_ipos(rows: list[dict]) -> int:
    if not rows:
        return 0
    now = datetime.utcnow().isoformat()
    with get_conn() as conn:
        for r in rows:
            conn.execute(
                """INSERT INTO ipo_events
                   (id, symbol, name, date, exchange, price_range, shares,
                    total_value, status, fetched_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET
                       price_range=excluded.price_range,
                       shares=excluded.shares,
                       total_value=excluded.total_value,
                       status=excluded.status,
                       fetched_at=excluded.fetched_at""",
                (
                    r["id"], r.get("symbol"), r["name"], r["date"],
                    r.get("exchange"), r.get("price_range"), r.get("shares"),
                    r.get("total_value"), r.get("status"), now,
                ),
            )
    return len(rows)


def upsert_profile(symbol: str, profile: dict):
    now = datetime.utcnow().isoformat()
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO stock_profiles
               (symbol, name, market_cap_m, industry, exchange, logo, weburl, fetched_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(symbol) DO UPDATE SET
                   name=excluded.name,
                   market_cap_m=excluded.market_cap_m,
                   industry=excluded.industry,
                   exchange=excluded.exchange,
                   logo=excluded.logo,
                   weburl=excluded.weburl,
                   fetched_at=excluded.fetched_at""",
            (
                symbol,
                profile.get("name"),
                profile.get("marketCapitalization"),
                profile.get("finnhubIndustry"),
                profile.get("exchange"),
                profile.get("logo"),
                profile.get("weburl"),
                now,
            ),
        )


def get_symbols_needing_profile(symbols: list[str], max_age_days: int = 30) -> list[str]:
    """Return subset of symbols that have no cached profile or whose cache is stale."""
    if not symbols:
        return []
    from datetime import timedelta
    cutoff = (datetime.utcnow() - timedelta(days=max_age_days)).isoformat()
    with get_conn() as conn:
        placeholders = ",".join("?" * len(symbols))
        rows = conn.execute(
            f"""SELECT symbol FROM stock_profiles
                WHERE symbol IN ({placeholders}) AND fetched_at >= ?""",
            (*symbols, cutoff),
        ).fetchall()
        fresh = {r["symbol"] for r in rows}
    return [s for s in symbols if s not in fresh]


def upsert_macro_events(rows: list[dict]) -> int:
    """Replace-style upsert: identifies a row by (date, title)."""
    if not rows:
        return 0
    import re
    n = 0
    with get_conn() as conn:
        for r in rows:
            slug = re.sub(r"[^a-z0-9]+", "-", r["title"].lower()).strip("-")
            mid = f"macro::{slug}::{r['date']}"
            conn.execute(
                """INSERT INTO macro_events (id, date, time, title, category, impact, notes)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET
                       time=excluded.time,
                       category=excluded.category,
                       impact=excluded.impact,
                       notes=excluded.notes""",
                (
                    mid, r["date"], r.get("time"), r["title"],
                    r.get("category"), r.get("impact"), r.get("notes"),
                ),
            )
            n += 1
    return n


def get_calendar_window(start: str, end: str, filters: dict | None = None) -> dict:
    """Return {date: {earnings:[...], ipos:[...], macro:[...]}} for [start, end] inclusive.

    filters keys (all optional):
      - min_market_cap_m: int (default 10000)
      - industries: list[str] | None (substring match against profile.industry)
      - watchlist:  list[str] | None (always include these symbols)
      - include_ipos:  bool (default True)
      - include_macro: bool (default True)
    """
    filters = filters or {}
    min_cap = filters.get("min_market_cap_m", 10000)
    industries = [i.lower() for i in (filters.get("industries") or [])]
    watchlist = set((filters.get("watchlist") or []))
    include_earnings = filters.get("include_earnings", True)
    include_ipos = filters.get("include_ipos", True)
    include_macro = filters.get("include_macro", True)

    out: dict[str, dict] = {}

    def _bucket(d: str) -> dict:
        return out.setdefault(d, {"earnings": [], "ipos": [], "macro": []})

    with get_conn() as conn:
        if not include_earnings:
            rows = []
        else:
            # Earnings — JOIN profiles, filter client-side for flexibility
            rows = conn.execute(
            """SELECT e.symbol, e.date, e.hour, e.eps_estimate, e.eps_actual,
                      e.rev_estimate, e.rev_actual, e.quarter, e.year,
                      p.name, p.market_cap_m, p.industry, p.logo, p.weburl
               FROM earnings_events e
               LEFT JOIN stock_profiles p USING(symbol)
               WHERE e.date BETWEEN ? AND ?
               ORDER BY e.date, COALESCE(p.market_cap_m, 0) DESC""",
            (start, end),
        ).fetchall()
        for r in rows:
            symbol = r["symbol"]
            cap = r["market_cap_m"] or 0
            industry = (r["industry"] or "").lower()
            in_watchlist = symbol in watchlist
            cap_ok = cap >= min_cap
            ind_ok = industries and any(i in industry for i in industries)
            if not (in_watchlist or cap_ok or ind_ok):
                continue
            _bucket(r["date"])["earnings"].append({
                "symbol": symbol,
                "name": r["name"] or symbol,
                "hour": r["hour"],
                "eps_estimate": r["eps_estimate"],
                "eps_actual": r["eps_actual"],
                "rev_estimate": r["rev_estimate"],
                "rev_actual": r["rev_actual"],
                "quarter": r["quarter"],
                "year": r["year"],
                "market_cap_m": cap,
                "industry": r["industry"],
                "logo": r["logo"],
                "weburl": r["weburl"],
                "is_watchlist": in_watchlist,
            })

        if include_ipos:
            rows = conn.execute(
                """SELECT symbol, name, date, exchange, price_range, shares,
                          total_value, status
                   FROM ipo_events
                   WHERE date BETWEEN ? AND ?
                   ORDER BY date, COALESCE(total_value, 0) DESC""",
                (start, end),
            ).fetchall()
            for r in rows:
                _bucket(r["date"])["ipos"].append(dict(r))

        if include_macro:
            rows = conn.execute(
                """SELECT date, time, title, category, impact, notes
                   FROM macro_events
                   WHERE date BETWEEN ? AND ?
                   ORDER BY date, time""",
                (start, end),
            ).fetchall()
            for r in rows:
                _bucket(r["date"])["macro"].append(dict(r))

    return out
