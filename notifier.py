"""Email notifier — sends digest at 06:00, 12:00, 18:00 and 22:00 daily."""

import html as _html
import logging
import smtplib
import threading
import time
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import config
import ai_processor
import storage
import subscribers

logger = logging.getLogger(__name__)

# Send digest at these hours (local time) → window covers since previous send
SEND_HOURS = {6, 12, 18, 22}
SEND_WINDOWS = {6: 8, 12: 6, 18: 6, 22: 4}   # hour → look-back hours
SEND_LABELS  = {6: "早报", 12: "午报", 18: "晚报", 22: "夜报"}

# Earnings calendar block — included in 07:00 (today) and 20:00 (tomorrow's preview)
_HOUR_LABEL_ZH = {"bmo": "盘前", "amc": "盘后", "dmh": "盘中"}
_IMPACT_COLOR  = {"high": "#dc2626", "medium": "#ea580c", "low": "#6b7280"}
_IMPACT_LABEL  = {"high": "高影响", "medium": "中影响", "low": "低影响"}


def _fmt_cap(m_usd: float | None) -> str:
    if not m_usd:
        return ""
    if m_usd >= 1_000_000:
        return f"${m_usd/1_000_000:.1f}T"
    if m_usd >= 1000:
        return f"${m_usd/1000:.0f}B"
    return f"${m_usd:.0f}M"


def _build_calendar_html(target_date, label_zh: str) -> str:
    """Render an HTML block for the day's earnings/IPO/macro events.

    target_date: a datetime.date.
    Returns "" if the day has no events under the default filter.
    """
    iso = target_date.isoformat()
    # Email uses a stricter cap threshold and drops the broad industry whitelist:
    # only mega-cap or watchlist names ship to email so a heavy earnings day
    # stays scannable. Full list is still on the /earnings sub-page.
    bucket = storage.get_calendar_window(iso, iso, {
        "min_market_cap_m": getattr(config, "EARNINGS_EMAIL_MIN_CAP_M", 50000),
        "industries":       [],
        "watchlist":        config.EARNINGS_WATCHLIST,
    }).get(iso, {"earnings": [], "ipos": [], "macro": []})

    earnings = bucket.get("earnings") or []
    ipos     = bucket.get("ipos") or []
    macro    = bucket.get("macro") or []
    if not (earnings or ipos or macro):
        return ""

    parts = ["""
  <div style='background:#eff6ff;border:1px solid #93c5fd;border-radius:8px;
              padding:16px 20px;margin-bottom:24px;'>
"""]
    weekday = ["一", "二", "三", "四", "五", "六", "日"][target_date.weekday()]
    header = f"📅 {label_zh}财报与重要数据 · {target_date.strftime('%Y-%m-%d')} 周{weekday}"
    parts.append(f"""    <div style='font-size:13px;font-weight:700;color:#1e40af;margin-bottom:12px;'>{header}</div>
""")

    # Macro first — usually the most market-moving
    if macro:
        parts.append("""    <div style='margin-bottom:12px;'>
      <div style='font-size:12px;font-weight:600;color:#dc2626;margin-bottom:6px;'>🏛 美国宏观事件</div>
      <ul style='margin:0;padding-left:20px;'>
""")
        for m in macro:
            impact = (m.get("impact") or "low").lower()
            color  = _IMPACT_COLOR.get(impact, "#6b7280")
            ilabel = _IMPACT_LABEL.get(impact, impact)
            time_s = _html.escape(m.get("time") or "")
            title  = _html.escape(m.get("title") or "")
            tail = ""
            if m.get("notes"):
                tail = f"<span style='color:#6b7280;'> &nbsp;·&nbsp; {_html.escape(m['notes'])}</span>"
            time_html = f"<span style='color:#374151;'>{time_s}</span> &nbsp;·&nbsp; " if time_s else ""
            parts.append(
                f"        <li style='margin:4px 0;font-size:13px;line-height:1.55;'>"
                f"{time_html}<strong>{title}</strong> "
                f"<span style='color:{color};font-size:11px;font-weight:600;'>[{ilabel}]</span>"
                f"{tail}</li>\n"
            )
        parts.append("      </ul>\n    </div>\n")

    # Earnings — sort by market cap desc (already done by storage)
    if earnings:
        # Cap to 12 to keep email tidy even on heavy days
        shown = earnings[:12]
        more  = len(earnings) - len(shown)
        parts.append(f"""    <div style='margin-bottom:12px;'>
      <div style='font-size:12px;font-weight:600;color:#1e40af;margin-bottom:6px;'>📊 财报 ({len(earnings)})</div>
      <ul style='margin:0;padding-left:20px;'>
""")
        for e in shown:
            sym  = _html.escape(e.get("symbol") or "")
            name = _html.escape(e.get("name") or "")
            hour = (e.get("hour") or "").lower()
            hour_label = _HOUR_LABEL_ZH.get(hour, "")
            hour_html = ""
            if hour_label:
                hour_html = (f" <span style='background:#dbeafe;color:#1e40af;"
                             f"padding:1px 6px;border-radius:3px;font-size:10px;font-weight:600;'>"
                             f"{hour_label}</span>")
            details = []
            eps = e.get("eps_estimate")
            if eps is not None:
                try: details.append(f"EPS 预期 ${float(eps):.2f}")
                except Exception: pass
            cap_str = _fmt_cap(e.get("market_cap_m"))
            if cap_str:
                details.append(cap_str)
            details_html = ""
            if details:
                details_html = f"<span style='color:#6b7280;font-size:12px;'> &nbsp;·&nbsp; {' · '.join(details)}</span>"
            url = e.get("weburl") or f"https://finance.yahoo.com/quote/{sym}"
            parts.append(
                f"        <li style='margin:4px 0;font-size:13px;line-height:1.55;'>"
                f"<a href='{_html.escape(url)}' style='color:#1e40af;text-decoration:none;font-weight:600;' "
                f"target='_blank'>{sym}</a>{hour_html} "
                f"<span style='color:#374151;'>{name}</span>"
                f"{details_html}</li>\n"
            )
        if more > 0:
            parts.append(
                f"        <li style='margin:4px 0;font-size:12px;color:#6b7280;font-style:italic;'>"
                f"… 另有 {more} 家(查看 /earnings 子页面)</li>\n"
            )
        parts.append("      </ul>\n    </div>\n")

    # IPOs
    if ipos:
        parts.append(f"""    <div>
      <div style='font-size:12px;font-weight:600;color:#7c3aed;margin-bottom:6px;'>🚀 IPO ({len(ipos)})</div>
      <ul style='margin:0;padding-left:20px;'>
""")
        for i in ipos[:8]:
            name = _html.escape(i.get("name") or "")
            sym  = _html.escape(i.get("symbol") or "")
            sym_html = f" <span style='color:#6b7280;'>({sym})</span>" if sym else ""
            details = []
            if i.get("exchange"):    details.append(_html.escape(i["exchange"]))
            if i.get("price_range"): details.append(f"${_html.escape(i['price_range'])}")
            details_html = (f"<span style='color:#6b7280;font-size:12px;'> &nbsp;·&nbsp; {' · '.join(details)}</span>"
                            if details else "")
            parts.append(
                f"        <li style='margin:4px 0;font-size:13px;line-height:1.55;'>"
                f"<strong style='color:#7c3aed;'>{name}</strong>{sym_html}{details_html}</li>\n"
            )
        parts.append("      </ul>\n    </div>\n")

    parts.append("  </div>\n")
    return "".join(parts)


def _ensure_translations(posts: list[dict], tweets: list[dict]):
    """
    Make sure every post has title_zh/summary_zh and every tweet has text_zh.
    Calls DeepSeek for any missing pieces and persists results to SQLite so the
    web UI sees the same translations next time.
    """
    if not config.DEEPSEEK_API_KEY:
        return

    # Posts: collect missing title + summary in a flat list, translate, scatter back
    post_targets = []  # list of (post_dict, field_name, original_text)
    for b in posts:
        p = b["item"]
        if not p.get("title_zh") and p.get("title"):
            post_targets.append((p, "title_zh", p["title"]))
        if p.get("category") == "polymarket":
            continue  # summary contains Yes/No odds — keep in English
        if not p.get("summary_zh") and p.get("summary"):
            post_targets.append((p, "summary_zh", p["summary"][:500]))

    if post_targets:
        # Chunk into batches of 10 to stay within max_tokens
        CHUNK = 10
        translated = []
        texts = [t[2] for t in post_targets]
        for i in range(0, len(texts), CHUNK):
            translated.extend(ai_processor.translate_texts(texts[i:i + CHUNK]))
        for (p, field, _orig), zh in zip(post_targets, translated):
            if zh and zh != _orig:
                p[field] = zh
        # Persist per-post (one UPDATE each — small batches, fine)
        seen = set()
        for p, _field, _ in post_targets:
            if p.get("id") and p["id"] not in seen and (p.get("title_zh") or p.get("summary_zh")):
                seen.add(p["id"])
                try:
                    storage.update_post_translation(
                        p["id"], p.get("title_zh", ""), p.get("summary_zh", "")
                    )
                except Exception as e:
                    logger.warning("persist post translation failed: %s", e)

    # Tweets
    tweet_targets = [b["item"] for b in tweets if not b["item"].get("text_zh") and b["item"].get("text")]
    if tweet_targets:
        CHUNK = 10
        texts = [t["text"] for t in tweet_targets]
        translated = []
        for i in range(0, len(texts), CHUNK):
            translated.extend(ai_processor.translate_texts(texts[i:i + CHUNK]))
        for t, zh in zip(tweet_targets, translated):
            if zh and zh != t["text"]:
                t["text_zh"] = zh
                try:
                    storage.update_tweet_translation(t["id"], zh)
                except Exception as e:
                    logger.warning("persist tweet translation failed: %s", e)


class EmailNotifier:
    def __init__(self):
        self._queue: list[dict] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        if not config.EMAIL_SENDER or not config.EMAIL_APP_PASSWORD:
            logger.warning("Email not configured — notifier disabled")
            return
        self._thread = threading.Thread(target=self._run, daemon=True, name="email-notifier")
        self._thread.start()
        logger.info("Email notifier started — daily digest at %s → %s",
                    ", ".join(f"{h:02d}:00" for h in sorted(SEND_HOURS)),
                    ", ".join(config.EMAIL_RECIPIENTS))

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def enqueue(self, item: dict, item_type: str = "post"):
        """Accumulate new items for the next digest."""
        with self._lock:
            self._queue.append({"item": item, "type": item_type})

    def _build_batch(self, slot_time: datetime, window_hours: int) -> list[dict]:
        """Fetch posts for the window ending at slot_time. Tweets are no
        longer included in digests."""
        from datetime import timedelta
        cutoff = (slot_time - timedelta(hours=window_hours)).isoformat()
        # Pull a wide candidate set so the window-cutoff filter — not the SQL
        # LIMIT — decides what's in the digest. Heavy windows can produce 60+
        # posts; the per-category quota in _send() trims it back down.
        posts  = [p for p in storage.get_latest_posts(limit=300)
                  if (p.get("published") or p.get("fetched_at", "")) >= cutoff]
        return [{"item": p, "type": "post"} for p in posts]

    def _try_send_slot(self, date_str: str, slot_hour: int, slot_time: datetime, label: str):
        """Build and send digest for one slot; returns True on success."""
        if not config.EMAIL_ENABLED:
            logger.info("Email paused (EMAIL_ENABLED=false) — skipping digest slot %02d:00", slot_hour)
            return False
        window_hours = SEND_WINDOWS[slot_hour]
        batch = self._build_batch(slot_time, window_hours)

        # Drain any in-memory queue items not yet in DB. Tweets are dropped
        # since digests no longer include a Twitter section.
        with self._lock:
            queued = list(self._queue)
            self._queue.clear()
        dropped_tweets = sum(1 for q in queued if q.get("type") == "tweet")
        if dropped_tweets:
            logger.info("dropped %d queued tweet(s) — digests no longer include Twitter",
                        dropped_tweets)
        existing_ids = {b["item"].get("id") for b in batch}
        for q in queued:
            if q.get("type") == "tweet":
                continue
            if q["item"].get("id") not in existing_ids:
                batch.append(q)

        # Earnings calendar: 06:00 → today, 22:00 → tomorrow's preview
        calendar_html = ""
        if slot_hour == 6:
            calendar_html = _build_calendar_html(slot_time.date(), "今日")
        elif slot_hour == 22:
            calendar_html = _build_calendar_html(slot_time.date() + timedelta(days=1), "明日")

        if batch or calendar_html:
            try:
                self._send(batch, label=label, calendar_html=calendar_html,
                           window_hours=window_hours, slot_time=slot_time)
                storage.record_digest_sent(date_str, slot_hour)
                return True
            except Exception as e:
                logger.error("Failed to send email: %s", e)
                with self._lock:
                    self._queue = queued + self._queue
                return False
        else:
            logger.info("Digest %s %02d:00 — no items in window, skipping", date_str, slot_hour)
            storage.record_digest_sent(date_str, slot_hour)
            return True

    def _run(self):
        from datetime import timedelta
        # Grace period: catch up today's slot if woke up within 2 hours
        CATCHUP_GRACE_HOURS = 2

        while not self._stop.is_set():
            now = datetime.now()
            today = now.strftime("%Y-%m-%d")

            # ── Today's scheduled and grace-period sends ───────────────────
            for slot_hour in sorted(SEND_HOURS):
                slot_time = now.replace(hour=slot_hour, minute=0, second=0, microsecond=0)
                if slot_time > now:
                    continue
                if storage.was_digest_sent(today, slot_hour):
                    continue

                minutes_late = (now - slot_time).total_seconds() / 60
                if minutes_late > CATCHUP_GRACE_HOURS * 60:
                    storage.record_digest_sent(today, slot_hour)
                    logger.info("Digest slot %02d:00 missed by %.0f min — skipping", slot_hour, minutes_late)
                    continue

                label = SEND_LABELS[slot_hour]
                if minutes_late > 5:
                    label += " (catch-up)"
                self._try_send_slot(today, slot_hour, slot_time, label)

            # ── Cross-day catch-up: send the most recent missed past slot ──
            # Collect all unsent slots from previous days (up to 7 days back),
            # ordered most-recent-first. Send only the latest one; silently
            # mark the rest as done to avoid a flood of old digests.
            missed_past = []
            for days_back in range(1, 8):
                check_date = now - timedelta(days=days_back)
                date_str = check_date.strftime("%Y-%m-%d")
                for slot_hour in sorted(SEND_HOURS, reverse=True):
                    slot_time = check_date.replace(hour=slot_hour, minute=0, second=0, microsecond=0)
                    if not storage.was_digest_sent(date_str, slot_hour):
                        missed_past.append((date_str, slot_hour, slot_time))

            if missed_past:
                # Send only the most recent missed slot
                date_str, slot_hour, slot_time = missed_past[0]
                label = f"{SEND_LABELS[slot_hour]} (catch-up from {date_str})"
                self._try_send_slot(date_str, slot_hour, slot_time, label)
                # Silently discard all older missed slots
                for old_date, old_hour, _ in missed_past[1:]:
                    storage.record_digest_sent(old_date, old_hour)
                    logger.info("Cross-day slot %s %02d:00 — marked skipped (superseded by catch-up)",
                                old_date, old_hour)

            # Sleep 60s between checks
            self._stop.wait(60)

    def send_alert(self, post: dict):
        """Send an immediate single-post alert email (bypasses digest queue)."""
        if not config.EMAIL_ENABLED or not config.EMAIL_SENDER or not config.EMAIL_APP_PASSWORD:
            return
        try:
            self._send_alert_email(post)
        except Exception as e:
            logger.error("Failed to send alert email: %s", e)

    def _send_alert_email(self, post: dict):
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
        subject = f"🔔 {post['source']}: {post['title']}"
        date = post.get("published", "")[:16].replace("T", " ")
        summary_html = (
            f"<p style='margin:8px 0 0;font-size:13px;color:#555;line-height:1.5;'>{post['summary'][:300]}…</p>"
            if post.get("summary") else ""
        )
        html_body = f"""
<html><body style='font-family:-apple-system,BlinkMacSystemFont,sans-serif;
  max-width:680px;margin:auto;padding:24px;background:#fff;color:#222;'>
  <div style='background:#7c3aed;color:#fff;padding:20px 24px;border-radius:10px 10px 0 0;'>
    <h1 style='margin:0;font-size:18px;'>🔔 新文章通知</h1>
    <p style='margin:4px 0 0;font-size:12px;opacity:.8;'>{now_str}</p>
  </div>
  <div style='padding:20px;border:1px solid #e5e7eb;border-top:none;border-radius:0 0 10px 10px;margin-bottom:24px;'>
    <div style='font-size:11px;color:#888;margin-bottom:8px;'>{post['source']} · {date}</div>
    <a href='{post['url']}' style='font-size:17px;font-weight:700;color:#7c3aed;text-decoration:none;
       line-height:1.4;display:block;'>{post['title']}</a>
    {summary_html}
    <a href='{post['url']}' style='display:inline-block;margin-top:14px;font-size:13px;
       color:#fff;background:#7c3aed;padding:8px 16px;border-radius:6px;text-decoration:none;'>
      阅读全文 →</a>
  </div>
</body></html>"""
        text_body = f"{post['source']} — {post['title']}\n{post['url']}\n"

        recipients = subscribers.list_active_subscribers()
        if not recipients:
            logger.warning("Alert — no active subscribers in DB; skipping")
            return
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(config.EMAIL_SENDER, config.EMAIL_APP_PASSWORD)
            for sub in recipients:
                msg = MIMEMultipart("alternative")
                msg["Subject"] = subject
                msg["From"]    = config.EMAIL_SENDER
                msg["To"]      = sub.email
                msg.attach(MIMEText(text_body, "plain"))
                msg.attach(MIMEText(html_body, "html"))
                server.sendmail(config.EMAIL_SENDER, sub.email, msg.as_string())
        logger.info("Alert sent: [%s] %s", post["source"], post["title"][:80])

    def _send(self, batch: list[dict], label: str = "Digest", calendar_html: str = "",
              window_hours: int = 24, slot_time: datetime | None = None):
        _podcast_sources = {f["name"] for f in config.RSS_FEEDS if f.get("podcast")}

        # Cutoff used to align every auxiliary section (briefing / trending
        # papers / polymarket) to the same look-back window as `batch`.
        cutoff_dt = (slot_time or datetime.now()) - timedelta(hours=window_hours)
        cutoff_iso = cutoff_dt.isoformat()

        all_posts = [b for b in batch if b["type"] == "post"]
        # Papers are a bonus section sourced separately (top-N by score). Hide
        # them from the regular Posts section so they aren't shown twice.
        non_paper      = [b for b in all_posts if not b["item"].get("is_paper")]
        podcasts       = [b for b in non_paper if b["item"]["source"] in _podcast_sources]
        # Polymarket — filter to slot window so the section matches the rest
        # of the digest instead of surfacing markets from yesterday.
        polymarket_all = storage.get_latest_posts_by_category("polymarket", limit=40)
        polymarket_top = [p for p in polymarket_all
                          if (p.get("fetched_at") or p.get("published") or "") >= cutoff_iso][:8]
        polymarket_ids = {p["id"] for p in polymarket_top}
        posts          = [b for b in non_paper
                          if b["item"]["source"] not in _podcast_sources
                          and b["item"].get("id") not in polymarket_ids
                          and b["item"].get("category") != "polymarket"]

        # Trending papers — aligned to slot window.
        trending_paper_rows = storage.get_trending_papers(
            hours=window_hours, limit=5, min_score=10.0,
        )
        trending_papers = [{"item": p, "type": "post"} for p in trending_paper_rows]

        # Category briefing — aligned to slot window so morning/midday/evening
        # emails each summarise *their own* time slice instead of a fixed 24h.
        briefing = {"sections": []}
        try:
            posts_by_cat = storage.get_recent_posts_by_category(
                hours=window_hours, limit_per_category=10,
            )
            briefing = ai_processor.generate_daily_briefing(posts_by_cat)
        except Exception as e:
            logger.warning("briefing generation failed (skipping section): %s", e)

        now_str   = datetime.now().strftime("%Y-%m-%d %H:%M")

        # ── Ensure bilingual: fill any missing _zh translations on demand ──
        _ensure_translations(all_posts + trending_papers, [])

        subject = f"看牛韵新闻，抓财富风云 · {label} · {now_str}"

        # ── AI digest summary — balanced by category priority ─────────────
        _digest_quota = {"us_stock": 8, "trump": 7, "geopolitics": 7, "venture": 6,
                         "polymarket": 5, "ai": 5, "papers": 3, "web3": 3}
        _posts_by_cat: dict[str, list] = {}
        for b in batch:
            cat = b["item"].get("category") or "ai"
            _posts_by_cat.setdefault(cat, []).append(b)
        def _q(b):
            d = b["item"]
            return (int(d.get("hn_score") or 0)
                    + int(d.get("hf_upvotes") or 0) * 2
                    + float(d.get("paper_score") or 0))
        digest_batch = []
        for cat, quota in _digest_quota.items():
            items_for_cat = sorted(_posts_by_cat.get(cat, []), key=_q, reverse=True)
            digest_batch.extend(items_for_cat[:quota])
        digest_summary = ai_processor.generate_digest_summary(digest_batch, top_n=6)

        # ── Joke of the day ───────────────────────────────────────────────
        jokes: list[str] = []
        try:
            jokes = ai_processor.generate_joke(digest_batch)
        except Exception as e:
            logger.warning("joke generation failed (skipping): %s", e)

        # ── HTML body ──────────────────────────────────────────────────────
        html_parts = ["""
<html><body style='font-family:-apple-system,BlinkMacSystemFont,sans-serif;
  max-width:680px;margin:auto;padding:24px;background:#fff;color:#222;'>
"""]
        html_parts.append(f"""
  <div style='background:#0f3460;color:#fff;padding:20px 24px;border-radius:10px 10px 0 0;'>
    <h1 style='margin:0;font-size:20px;'>看牛韵新闻，抓财富风云 · {label}</h1>
    <p style='margin:4px 0 0;font-size:13px;opacity:.8;'>{now_str} &nbsp;·&nbsp; {len(batch)} 条资讯</p>
  </div>
  <div style='background:#f4f6fb;padding:16px 24px;border-radius:0 0 10px 10px;margin-bottom:24px;'>
    <span style='font-size:13px;color:#555;'>🎙 {len(podcasts)} 播客 &nbsp;&nbsp; 📰 {len(posts)} blog posts &nbsp;&nbsp; 📄 {len(trending_papers)} 热门论文 &nbsp;&nbsp; 🎯 {len(polymarket_top)} 预测市场</span>
  </div>
""")
        # Digest summary block — bullet points, one per line
        if digest_summary:
            def _digest_li(b) -> str:
                text = b.get("text", b) if isinstance(b, dict) else b
                url  = b.get("url", "") if isinstance(b, dict) else ""
                inner = (f"<a href='{url}' style='color:#b45309;text-decoration:none;' "
                         f"target='_blank'>{text}</a>") if url else text
                return f"<li style='margin:6px 0;font-size:14px;color:#333;line-height:1.6;'>{inner}</li>"
            bullets_html = "".join(_digest_li(b) for b in digest_summary)
            html_parts.append(f"""
  <div style='background:#fffbeb;border:1px solid #fcd34d;border-radius:8px;
              padding:16px 20px;margin-bottom:24px;'>
    <div style='font-size:12px;font-weight:600;color:#92400e;margin-bottom:10px;'>📋 今日资讯摘要</div>
    <ul style='margin:0;padding-left:20px;'>{bullets_html}</ul>
  </div>
""")

        # ── 😂 Joke of the day ────────────────────────────────────────────
        if jokes:
            email_jokes = jokes[:2]
            jokes_html = "".join(
                f"<div style='padding:10px 0;font-size:14px;color:#374151;line-height:1.8;"
                f"white-space:pre-wrap;border-bottom:1px solid #fcd34d;'>{_html.escape(j)}</div>"
                if i < len(email_jokes) - 1 else
                f"<div style='padding:10px 0;font-size:14px;color:#374151;line-height:1.8;"
                f"white-space:pre-wrap;'>{_html.escape(j)}</div>"
                for i, j in enumerate(email_jokes)
            )
            html_parts.append(f"""
  <div style='background:#fffbeb;border:1px solid #fcd34d;border-left:4px solid #f59e0b;
              border-radius:8px;padding:16px 20px;margin-bottom:24px;'>
    <div style='font-size:12px;font-weight:600;color:#92400e;margin-bottom:8px;'>😂 今日笑话</div>
    {jokes_html}
  </div>
""")

        # ── 📅 Earnings calendar — only present in 07:00/20:00 emails ──────
        if calendar_html:
            html_parts.append(calendar_html)

        # ── ⚡ Category Briefing (analogous to website /api/briefing) ──────
        sections = (briefing or {}).get("sections") or []
        if sections:
            html_parts.append("""
  <div style='background:#eff6ff;border:1px solid #93c5fd;border-radius:8px;
              padding:16px 20px;margin-bottom:24px;'>
    <div style='font-size:13px;font-weight:700;color:#1e40af;margin-bottom:12px;'>⚡ 每日要闻速报</div>
""")
            for sec in sections:
                points = sec.get("points") or []
                if not points:
                    continue
                icon  = sec.get("icon")  or "📌"
                title = sec.get("label") or sec.get("category") or ""
                def _pt_html(p) -> str:
                    text = p.get("text", p) if isinstance(p, dict) else p
                    url  = p.get("url", "") if isinstance(p, dict) else ""
                    inner = (f"<a href='{url}' style='color:#1e40af;text-decoration:none;' "
                             f"target='_blank'>{text}</a>") if url else text
                    return f"<li style='margin:4px 0;font-size:13px;color:#1f2937;line-height:1.55;'>{inner}</li>"
                points_html = "".join(_pt_html(p) for p in points)
                html_parts.append(f"""
    <div style='margin-bottom:14px;'>
      <div style='font-size:13px;font-weight:600;color:#1e3a8a;margin-bottom:4px;'>{icon} {title}</div>
      <ul style='margin:0;padding-left:22px;'>{points_html}</ul>
    </div>""")
            html_parts.append("  </div>\n")

        # ── 🔥 Trending Papers section ─────────────────────────────────────
        if trending_papers:
            html_parts.append(
                f"<h2 style='font-size:16px;color:#be185d;border-bottom:2px solid #be185d;"
                f"padding-bottom:8px;margin-top:8px;'>🔥 今日热门论文 ({len(trending_papers)})</h2>"
            )
            for b in trending_papers:
                p = b["item"]
                title_zh   = p.get("title_zh") or ""
                summary_zh = p.get("summary_zh") or ""
                upvotes    = int(p.get("hf_upvotes") or 0)
                arxiv_id   = p.get("arxiv_id") or ""
                pdf_url    = p.get("pdf_url") or ""
                authors_str = ""
                try:
                    import json as _json
                    arr = _json.loads(p.get("authors") or "[]")
                    if isinstance(arr, list) and arr:
                        head = ", ".join(arr[:3])
                        authors_str = f"👥 {head}" + (f" +{len(arr) - 3}" if len(arr) > 3 else "")
                except Exception:
                    pass

                # Detect 大模型公司 label from source/title/authors
                haystack = " ".join((p.get("source") or "", p.get("title") or "",
                                     p.get("authors") or "")).lower()
                company_label = ""
                for kw, label_zh in (
                    ("deepseek", "DeepSeek"), ("qwen", "Qwen"), ("alibaba", "Alibaba"),
                    ("bytedance", "字节跳动"), ("doubao", "字节豆包"),
                    ("moonshot", "Moonshot"), ("kimi", "Kimi"),
                    ("zhipu", "智谱"), ("thudm", "智谱"), ("glm", "智谱 GLM"),
                    ("01-ai", "01.AI"), ("01.ai", "01.AI"), ("baichuan", "百川"),
                    ("openai", "OpenAI"), ("anthropic", "Anthropic"),
                    ("deepmind", "DeepMind"), ("meta ai", "Meta AI"),
                    ("fair", "Meta FAIR"), ("mistral", "Mistral"), ("xai", "xAI"),
                ):
                    if kw in haystack:
                        company_label = label_zh
                        break

                badges = []
                if company_label:
                    badges.append(
                        f"<span style='background:#fce7f3;color:#be185d;font-size:11px;"
                        f"font-weight:600;padding:2px 8px;border-radius:4px;'>🏢 {company_label}</span>"
                    )
                if upvotes > 0:
                    badges.append(
                        f"<span style='background:#fef3c7;color:#b45309;font-size:11px;"
                        f"font-weight:600;padding:2px 8px;border-radius:4px;'>👍 {upvotes}</span>"
                    )
                if arxiv_id:
                    badges.append(
                        f"<span style='background:#ede9fe;color:#6d28d9;font-size:11px;"
                        f"font-weight:600;padding:2px 8px;border-radius:4px;font-family:monospace;'>"
                        f"arXiv:{arxiv_id}</span>"
                    )
                badges_html = " ".join(badges)

                title_zh_html  = f"<div style='font-size:13px;color:#666;margin-top:4px;'>{title_zh}</div>" if title_zh else ""
                summary_html   = (
                    f"<p style='margin:8px 0 0;font-size:12px;color:#aaa;line-height:1.5;'>{summary_zh[:220]}…</p>"
                    if summary_zh else (
                        f"<p style='margin:8px 0 0;font-size:12px;color:#888;line-height:1.5;'>{(p.get('summary') or '')[:220]}…</p>"
                        if p.get("summary") else ""
                    )
                )
                authors_html = (
                    f"<div style='font-size:11px;color:#888;margin-top:8px;'>{authors_str}</div>"
                    if authors_str else ""
                )
                pdf_link_html = (
                    f"&nbsp;&nbsp;<a href='{pdf_url}' style='color:#be185d;text-decoration:none;font-size:12px;'>PDF ↓</a>"
                    if pdf_url else ""
                )

                html_parts.append(f"""
  <div style='margin-bottom:16px;padding:14px 16px;border-left:4px solid #be185d;
              background:#fdf2f8;border-radius:0 8px 8px 0;'>
    <div style='display:flex;flex-wrap:wrap;gap:6px;margin-bottom:8px;'>{badges_html}</div>
    <a href='{p.get('url') or ''}' style='font-size:14px;font-weight:700;color:#be185d;
       text-decoration:none;line-height:1.4;display:block;'>{p.get('title') or ''}</a>
    {title_zh_html}
    {summary_html}
    {authors_html}
    <div style='margin-top:10px;'>
      <a href='{p.get('url') or ''}' style='display:inline-block;font-size:12px;color:#fff;
         background:#be185d;padding:5px 12px;border-radius:4px;text-decoration:none;'>查看论文 →</a>
      {pdf_link_html}
    </div>
  </div>""")

        # ── Polymarket section ────────────────────────────────────────────
        if polymarket_top:
            html_parts.append(
                f"<h2 style='font-size:16px;color:#15803d;border-bottom:2px solid #15803d;"
                f"padding-bottom:8px;margin-top:28px;'>🎯 预测市场热榜 ({len(polymarket_top)})</h2>"
            )
            for p in polymarket_top:
                summary = p.get("summary") or ""
                # Extract probability line (first segment before " | ")
                prob_line = summary.split(" | ")[0] if " | " in summary else ""
                vol_line  = ""
                for seg in summary.split(" | "):
                    if "Vol" in seg:
                        vol_line = seg
                        break
                deadline_line = ""
                for seg in summary.split(" | "):
                    if "Ends" in seg:
                        deadline_line = seg
                        break
                prob_html = (
                    f"<div style='font-size:13px;font-weight:600;color:#15803d;margin-top:6px;'>{prob_line}</div>"
                    if prob_line else ""
                )
                meta_parts = [s for s in [vol_line, deadline_line] if s]
                meta_html = (
                    f"<div style='font-size:12px;color:#888;margin-top:4px;'>{' &nbsp;·&nbsp; '.join(meta_parts)}</div>"
                    if meta_parts else ""
                )
                title_en = p.get('title') or ''
                title_zh = p.get('title_zh') or ''
                title_zh_html = (
                    f"<div style='font-size:13px;color:#166534;margin-top:3px;'>{title_zh}</div>"
                    if title_zh else ""
                )
                html_parts.append(f"""
  <div style='margin-bottom:14px;padding:14px 16px;border-left:4px solid #15803d;
              background:#f0fdf4;border-radius:0 8px 8px 0;'>
    <a href='{p.get('url') or ''}' style='font-size:14px;font-weight:700;color:#14532d;
       text-decoration:none;line-height:1.4;display:block;'>{title_en}</a>
    {title_zh_html}
    {prob_html}
    {meta_html}
    <a href='{p.get('url') or ''}' style='display:inline-block;margin-top:10px;font-size:12px;
       color:#fff;background:#15803d;padding:5px 12px;border-radius:4px;text-decoration:none;'>
      查看市场 →</a>
  </div>""")

        # ── Podcast section ────────────────────────────────────────────────
        if podcasts:
            html_parts.append(f"<h2 style='font-size:16px;color:#7c3aed;border-bottom:2px solid #7c3aed;padding-bottom:8px;margin-bottom:14px;'>🎙 新播客 ({len(podcasts)})</h2>")
            for b in podcasts:
                p = b["item"]
                date = p.get("published", "")[:10]
                summary = p.get("summary_zh") or p.get("summary", "")
                summary_snippet = summary[:250] + "…" if len(summary) > 250 else summary
                html_parts.append(f"""
  <div style='margin-bottom:16px;padding:16px;border-left:4px solid #7c3aed;
              background:#f5f3ff;border-radius:0 8px 8px 0;'>
    <div style='font-size:11px;color:#888;margin-bottom:6px;'>{p['source']} · {date}</div>
    <a href='{p['url']}' style='font-size:15px;font-weight:700;color:#7c3aed;text-decoration:none;
       line-height:1.4;display:block;margin-bottom:8px;'>{p['title']}</a>
    <p style='margin:0;font-size:13px;color:#555;line-height:1.6;'>{summary_snippet}</p>
    <a href='{p['url']}' style='display:inline-block;margin-top:10px;font-size:12px;
       color:#fff;background:#7c3aed;padding:5px 12px;border-radius:4px;text-decoration:none;'>
      收听 →</a>
  </div>""")

        if posts:
            html_parts.append(
                f"<h2 style='font-size:16px;color:#0f3460;border-bottom:2px solid #0f3460;"
                f"padding-bottom:8px;margin-top:28px;'>📰 Blog Posts ({len(posts)})</h2>"
            )

            # Group posts by category. Papers were already pulled out earlier
            # (handled by the trending-papers section), so the categories here
            # are AI / Web3 / 创投 / 美股. Anything with an unknown/missing
            # category falls under 其他.
            from collections import defaultdict
            by_cat: dict[str, list[dict]] = defaultdict(list)
            for b in posts:
                cat = (b["item"].get("category") or "ai").strip() or "ai"
                by_cat[cat].append(b)

            CATEGORY_ORDER = [
                ("polymarket",  "🎯", "预测市场"),
                ("venture",     "💰", "创投圈"),
                ("us_stock",    "📈", "美股"),
                ("trump",       "🇺🇸", "特朗普动向"),
                ("geopolitics", "🌍", "地缘政治"),
                ("ai",          "🤖", "AI 前沿"),
                ("papers",      "📄", "AI 论文"),
                ("web3",        "⛓️", "Web3"),
            ]
            seen_cats = {c for c, _, _ in CATEGORY_ORDER}
            extras = [(c, "📌", c) for c in by_cat.keys() if c not in seen_cats]

            for cat_key, icon, cat_label in CATEGORY_ORDER + extras:
                items = by_cat.get(cat_key, [])
                if not items:
                    continue
                html_parts.append(
                    f"<h3 style='font-size:14px;color:#0f3460;margin:20px 0 10px;"
                    f"padding:6px 12px;background:#eef2f8;border-radius:6px;"
                    f"display:inline-block;'>{icon} {cat_label} "
                    f"<span style='color:#888;font-weight:500;'>({len(items)})</span></h3>"
                )
                for b in items:
                    p = b["item"]
                    date = p.get("published", "")[:16].replace("T", " ") if p.get("published") else ""
                    title_zh   = p.get("title_zh", "")
                    summary_zh = p.get("summary_zh", "")
                    zh_title_html   = f"<div style='font-size:13px;color:#888;margin-top:3px;'>{title_zh}</div>" if title_zh else ""
                    zh_summary_html = f"<p style='margin:6px 0 0;font-size:12px;color:#aaa;line-height:1.5;'>{summary_zh[:200]}…</p>" if summary_zh else ""
                    html_parts.append(f"""
  <div style='margin-bottom:18px;padding:16px;border-left:4px solid #0f3460;
              background:#f8f9fa;border-radius:0 8px 8px 0;'>
    <div style='font-size:11px;color:#888;margin-bottom:6px;'>{p['source']} · {date}</div>
    <a href='{p['url']}' style='font-size:15px;font-weight:600;color:#0f3460;text-decoration:none;
       line-height:1.4;display:block;'>{p['title']}</a>
    {zh_title_html}
    {'<p style="margin:8px 0 0;font-size:13px;color:#555;line-height:1.5;">' + p["summary"][:200] + "…</p>" if p.get("summary") else ""}
    {zh_summary_html}
    <a href='{p['url']}' style='display:inline-block;margin-top:10px;font-size:12px;
       color:#fff;background:#0f3460;padding:5px 12px;border-radius:4px;text-decoration:none;'>
      Read →</a>
  </div>""")

        html_parts.append("""
  <div style='margin-top:32px;padding-top:16px;border-top:1px solid #eee;
              font-size:11px;color:#aaa;text-align:center;'>
    YunFlow · Sent automatically
  </div>
</body></html>""")
        html_body = "".join(html_parts)

        # ── Plain text fallback ────────────────────────────────────────────
        text_lines = [f"AI News {label} — {now_str}\n{len(batch)} new item(s)\n"]
        for b in batch:
            if b["type"] != "post":
                continue
            p = b["item"]
            line = f"[{p['source']}] {p['title']}"
            if p.get("title_zh"):
                line += f"\n  {p['title_zh']}"
            line += f"\n{p['url']}\n"
            text_lines.append(line)

        recipients = subscribers.list_active_subscribers()
        if not recipients:
            logger.warning("Digest %s — no active subscribers in DB; skipping send", label)
            return
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(config.EMAIL_SENDER, config.EMAIL_APP_PASSWORD)
            for sub in recipients:
                msg = MIMEMultipart("alternative")
                msg["Subject"] = subject
                msg["From"]    = config.EMAIL_SENDER
                msg["To"]      = sub.email
                msg.attach(MIMEText("\n".join(text_lines), "plain"))
                msg.attach(MIMEText(html_body, "html"))
                server.sendmail(config.EMAIL_SENDER, sub.email, msg.as_string())

        logger.info("Digest sent (%s): %d items → %s", label, len(batch),
                    ", ".join(s.email for s in recipients))
