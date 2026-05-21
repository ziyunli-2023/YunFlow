"""Subscription system — subscribers, magic links, sessions.

This module owns all business operations on the three subscription tables
(`subscribers`, `magic_links`, `sessions`). Schema lives in storage.py;
this file just provides higher-level operations on top.

Key concepts
------------
- `Subscriber`         : a person who can receive emails / view gated pages.
- `status`             : 'active' | 'invited' | 'paused' | 'churned'.
                        Only 'active' subscribers receive emails.
- `tier`               : 'free' | 'paid'. Gates content visibility.
- `paid_until`         : ISO timestamp. NULL = no expiry (internal/comp users).
- Magic Link           : 15-min one-time login token, emailed as a clickable URL.
- Session              : 30-day cookie-keyed login, issued after magic-link verify.
"""

import hashlib
import hmac
import json
import logging
import secrets
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

import config
from storage import get_conn

logger = logging.getLogger(__name__)


# ── Data class ─────────────────────────────────────────────────────────────

@dataclass
class Subscriber:
    id: int
    email: str
    name: Optional[str]
    status: str
    tier: str
    paid_until: Optional[str]
    stripe_customer_id: Optional[str] = None
    stripe_subscription_id: Optional[str] = None
    stripe_subscription_status: Optional[str] = None
    stripe_current_period_end: Optional[str] = None
    stripe_cancel_at_period_end: bool = False
    stripe_cancel_at: Optional[str] = None
    preferences: dict = field(default_factory=dict)
    created_at: str = ""
    updated_at: str = ""

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Subscriber":
        prefs: dict = {}
        raw = row["preferences"]
        if raw:
            try:
                prefs = json.loads(raw)
            except json.JSONDecodeError:
                logger.warning("subscriber %s has invalid preferences JSON", row["id"])
        return cls(
            id=row["id"],
            email=row["email"],
            name=row["name"],
            status=row["status"],
            tier=row["tier"],
            paid_until=row["paid_until"],
            stripe_customer_id=row["stripe_customer_id"],
            stripe_subscription_id=row["stripe_subscription_id"],
            stripe_subscription_status=row["stripe_subscription_status"],
            stripe_current_period_end=row["stripe_current_period_end"],
            stripe_cancel_at_period_end=bool(row["stripe_cancel_at_period_end"]),
            stripe_cancel_at=row["stripe_cancel_at"],
            preferences=prefs,
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


# ── Tier check (BUSINESS RULE) ─────────────────────────────────────────────

def is_paid(sub: Optional[Subscriber]) -> bool:
    """Return True if `sub` currently has paid-tier access.

    The three conditions that should all hold:
      1. The account must be active (status == 'active').
         Paused / churned / invited accounts never count as paid.
      2. The tier field must be 'paid'.
      3. If paid_until is set, it must be in the future.
         NULL paid_until means 'no expiry' — used for internal/comp accounts
         and pre-payment users. Treat NULL as "valid forever".

    A `None` subscriber (unauthenticated) is never paid.

    NOTE: this function is the single source of truth for tier gating.
    Both the email rendering path (notifier.py) and the web auth path
    (auth.py:require_paid) call it. Keep the logic here, don't inline.
    """
    if sub is None:
        return False
    if sub.status != "active" or sub.tier != "paid":
        return False
    if sub.paid_until is None:
        return True
    return sub.paid_until > _now()


# ── Subscriber reads ───────────────────────────────────────────────────────

def list_active_subscribers() -> list[Subscriber]:
    """All subscribers eligible to receive digest emails (status='active').

    Used by notifier.py as the source of truth for recipients.
    """
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM subscribers WHERE status = 'active' ORDER BY id"
        ).fetchall()
        return [Subscriber.from_row(r) for r in rows]


def get_by_email(email: str) -> Optional[Subscriber]:
    email = (email or "").lower().strip()
    if not email:
        return None
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM subscribers WHERE email = ?", (email,)
        ).fetchone()
    return Subscriber.from_row(row) if row else None


def get_by_id(sub_id: int) -> Optional[Subscriber]:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM subscribers WHERE id = ?", (sub_id,)
        ).fetchone()
    return Subscriber.from_row(row) if row else None


def get_by_stripe_customer_id(customer_id: str) -> Optional[Subscriber]:
    customer_id = (customer_id or "").strip()
    if not customer_id:
        return None
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM subscribers WHERE stripe_customer_id = ?",
            (customer_id,)
        ).fetchone()
    return Subscriber.from_row(row) if row else None


def get_by_stripe_subscription_id(subscription_id: str) -> Optional[Subscriber]:
    subscription_id = (subscription_id or "").strip()
    if not subscription_id:
        return None
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM subscribers WHERE stripe_subscription_id = ?",
            (subscription_id,)
        ).fetchone()
    return Subscriber.from_row(row) if row else None


# ── Subscriber writes ──────────────────────────────────────────────────────

def add_subscriber(email: str, name: str = "", tier: str = "free",
                   status: str = "active",
                   paid_until: Optional[str] = None) -> Subscriber:
    """Insert a new subscriber. Raises sqlite3.IntegrityError on duplicate email."""
    email = email.lower().strip()
    if not email:
        raise ValueError("email is required")
    now = _now()
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO subscribers
               (email, name, status, tier, paid_until, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (email, (name or None), status, tier, paid_until, now, now)
        )
        sub_id = cur.lastrowid
    logger.info("Subscriber added: %s (tier=%s, status=%s)", email, tier, status)
    return get_by_id(sub_id)  # type: ignore[return-value]


def set_stripe_customer(sub_id: int, customer_id: str) -> None:
    """Attach a Stripe customer ID to a subscriber if it is not already set."""
    if not customer_id:
        return
    with get_conn() as conn:
        conn.execute(
            """UPDATE subscribers
               SET stripe_customer_id =
                       CASE
                         WHEN stripe_customer_id IS NULL
                              OR stripe_customer_id = ''
                              OR stripe_customer_id NOT LIKE 'cus_%'
                         THEN ?
                         ELSE stripe_customer_id
                       END,
                   updated_at = ?
               WHERE id = ?""",
            (customer_id, _now(), sub_id)
        )


def sync_stripe_subscription(
    sub_id: int,
    *,
    customer_id: str = "",
    subscription_id: str = "",
    status: str = "",
    current_period_end: Optional[str] = None,
    cancel_at_period_end: bool = False,
    cancel_at: Optional[str] = None,
) -> Optional[Subscriber]:
    """Update local VIP access from a trusted Stripe subscription event."""
    now = _now()
    status = (status or "").strip()
    has_access = (
        bool(current_period_end)
        and status in {"active", "trialing", "past_due", "unpaid"}
        and current_period_end > now
    )
    tier = "paid" if has_access else "free"
    with get_conn() as conn:
        conn.execute(
            """UPDATE subscribers
               SET tier = ?,
                   status = 'active',
                   paid_until = ?,
                   stripe_customer_id =
                       CASE
                         WHEN ? != '' AND (
                           stripe_customer_id IS NULL
                           OR stripe_customer_id = ''
                           OR stripe_customer_id NOT LIKE 'cus_%'
                         )
                         THEN ?
                         ELSE stripe_customer_id
                       END,
                   stripe_subscription_id = COALESCE(NULLIF(?, ''), stripe_subscription_id),
                   stripe_subscription_status = COALESCE(NULLIF(?, ''), stripe_subscription_status),
                   stripe_current_period_end = ?,
                   stripe_cancel_at_period_end = ?,
                   stripe_cancel_at = ?,
                   updated_at = ?
               WHERE id = ?""",
            (
                tier,
                current_period_end,
                customer_id,
                customer_id,
                subscription_id,
                status,
                current_period_end,
                1 if cancel_at_period_end else 0,
                cancel_at,
                now,
                sub_id,
            )
        )
    return get_by_id(sub_id)


def stripe_event_processed(event_id: str) -> bool:
    event_id = (event_id or "").strip()
    if not event_id:
        return False
    with get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM stripe_events WHERE event_id = ?",
            (event_id,)
        ).fetchone()
    return bool(row)


def record_stripe_event(event_id: str, event_type: str) -> None:
    event_id = (event_id or "").strip()
    if not event_id:
        return
    with get_conn() as conn:
        conn.execute(
            """INSERT OR IGNORE INTO stripe_events
               (event_id, event_type, processed_at)
               VALUES (?, ?, ?)""",
            (event_id, event_type or "", _now())
        )


# ── Magic links ────────────────────────────────────────────────────────────

def create_magic_link(email: str) -> str:
    """Generate a one-time login token for `email`. Caller emails the URL."""
    email = email.lower().strip()
    token = secrets.token_urlsafe(32)
    now = datetime.now()
    expires = now + timedelta(minutes=config.MAGIC_LINK_TTL_MINUTES)
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO magic_links (token, email, created_at, expires_at)
               VALUES (?, ?, ?, ?)""",
            (token, email,
             now.isoformat(timespec="seconds"),
             expires.isoformat(timespec="seconds"))
        )
    return token


def consume_magic_link(token: str) -> Optional[str]:
    """Validate & burn a token. Returns the email if valid, else None.

    Uses `WHERE used_at IS NULL` on the UPDATE so concurrent uses can't both
    succeed — at most one caller will get rowcount=1.
    """
    if not token:
        return None
    with get_conn() as conn:
        row = conn.execute(
            "SELECT email, expires_at, used_at FROM magic_links WHERE token = ?",
            (token,)
        ).fetchone()
        if not row:
            return None
        if row["used_at"] is not None:
            return None
        if row["expires_at"] < _now():
            return None
        cur = conn.execute(
            "UPDATE magic_links SET used_at = ? WHERE token = ? AND used_at IS NULL",
            (_now(), token)
        )
        if cur.rowcount != 1:
            return None  # someone else just consumed it
        return row["email"]


# ── Login codes (email-based 6-digit verification code) ───────────────────
#
# Alternative to magic links: email a 6-digit code, user types it back. We
# store HMAC-SHA256(code, LOGIN_CODE_HMAC_KEY) so a DB leak doesn't reveal
# live codes. Codes are short-lived (config.LOGIN_CODE_TTL_MINUTES) and
# bounded to LOGIN_CODE_MAX_ATTEMPTS wrong submissions per code.

class LoginCodeCooldownError(Exception):
    """Raised by create_login_code when a code was sent too recently."""


def _hash_code(code: str) -> str:
    key = (config.LOGIN_CODE_HMAC_KEY or "").encode("utf-8")
    return hmac.new(key, code.encode("utf-8"), hashlib.sha256).hexdigest()


def create_login_code(email: str) -> str:
    """Generate a 6-digit code for `email`. Caller emails it.

    Raises LoginCodeCooldownError if a code was issued for the same email
    within the last config.LOGIN_CODE_COOLDOWN_SECONDS — prevents email
    spamming. The most recent un-used code for this email is also marked
    used, so only one live code exists at a time.
    """
    email = email.lower().strip()
    if not email:
        raise ValueError("email is required")
    now = datetime.now()
    cooldown_cutoff = (now - timedelta(seconds=config.LOGIN_CODE_COOLDOWN_SECONDS)
                       ).isoformat(timespec="seconds")
    with get_conn() as conn:
        recent = conn.execute(
            """SELECT created_at FROM login_codes
               WHERE email = ? AND created_at > ?
               ORDER BY created_at DESC LIMIT 1""",
            (email, cooldown_cutoff)
        ).fetchone()
        if recent:
            raise LoginCodeCooldownError(
                f"a code was sent within the last "
                f"{config.LOGIN_CODE_COOLDOWN_SECONDS}s"
            )
        # Invalidate any prior live codes for this email so only the newest
        # one works. Otherwise an attacker could keep multiple in flight.
        conn.execute(
            "UPDATE login_codes SET used_at = ? "
            "WHERE email = ? AND used_at IS NULL",
            (now.isoformat(timespec="seconds"), email)
        )
        code = f"{secrets.randbelow(1_000_000):06d}"
        expires = now + timedelta(minutes=config.LOGIN_CODE_TTL_MINUTES)
        conn.execute(
            """INSERT INTO login_codes (email, code_hash, created_at, expires_at)
               VALUES (?, ?, ?, ?)""",
            (email, _hash_code(code),
             now.isoformat(timespec="seconds"),
             expires.isoformat(timespec="seconds"))
        )
    return code


def consume_login_code(email: str, code: str) -> Optional[str]:
    """Validate a 6-digit code for `email`. Returns email on success, else None.

    On mismatch, increments the attempt counter on the latest live code; once
    it reaches LOGIN_CODE_MAX_ATTEMPTS the row is invalidated so the user must
    request a fresh code.
    """
    email = (email or "").lower().strip()
    code = (code or "").strip()
    if not email or not code or len(code) != 6 or not code.isdigit():
        return None
    with get_conn() as conn:
        row = conn.execute(
            """SELECT id, code_hash, expires_at, used_at, attempts
               FROM login_codes
               WHERE email = ? AND used_at IS NULL
               ORDER BY created_at DESC LIMIT 1""",
            (email,)
        ).fetchone()
        if not row:
            return None
        if row["expires_at"] < _now():
            return None
        submitted_hash = _hash_code(code)
        if not hmac.compare_digest(submitted_hash, row["code_hash"]):
            new_attempts = row["attempts"] + 1
            if new_attempts >= config.LOGIN_CODE_MAX_ATTEMPTS:
                # Burn the code — too many wrong tries
                conn.execute(
                    "UPDATE login_codes SET attempts = ?, used_at = ? WHERE id = ?",
                    (new_attempts, _now(), row["id"])
                )
            else:
                conn.execute(
                    "UPDATE login_codes SET attempts = ? WHERE id = ?",
                    (new_attempts, row["id"])
                )
            return None
        cur = conn.execute(
            "UPDATE login_codes SET used_at = ? WHERE id = ? AND used_at IS NULL",
            (_now(), row["id"])
        )
        if cur.rowcount != 1:
            return None
        return email


# ── Sessions ───────────────────────────────────────────────────────────────

def create_session(sub_id: int) -> str:
    """Issue a new session cookie value for the subscriber."""
    session_id = secrets.token_urlsafe(32)
    now = datetime.now()
    expires = now + timedelta(days=config.SESSION_TTL_DAYS)
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO sessions
               (id, subscriber_id, created_at, expires_at, last_seen)
               VALUES (?, ?, ?, ?, ?)""",
            (session_id, sub_id,
             now.isoformat(timespec="seconds"),
             expires.isoformat(timespec="seconds"),
             now.isoformat(timespec="seconds"))
        )
    return session_id


def get_by_session(session_id: Optional[str]) -> Optional[Subscriber]:
    """Resolve a session cookie to a Subscriber. None if missing/expired."""
    if not session_id:
        return None
    with get_conn() as conn:
        row = conn.execute(
            "SELECT subscriber_id, expires_at FROM sessions WHERE id = ?",
            (session_id,)
        ).fetchone()
        if not row:
            return None
        if row["expires_at"] < _now():
            return None
        # Touch last_seen — best-effort, don't crash auth on update failures
        try:
            conn.execute(
                "UPDATE sessions SET last_seen = ? WHERE id = ?",
                (_now(), session_id)
            )
        except Exception:
            pass
        sub_id = row["subscriber_id"]
    return get_by_id(sub_id)


def expire_session(session_id: Optional[str]) -> None:
    """Delete a session row (logout). No-op for unknown/empty IDs."""
    if not session_id:
        return
    with get_conn() as conn:
        conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))


# ── One-time seed from EMAIL_RECIPIENTS env ────────────────────────────────

def seed_initial_subscribers() -> int:
    """Bootstrap the subscribers table from config.EMAIL_RECIPIENTS.

    Runs once when the table is empty. Each existing recipient is added as
    `status='active', tier='paid', paid_until=NULL` so that current digest
    behavior is preserved across the schema migration. Returns the number
    of rows actually inserted (0 if the table was already non-empty).
    """
    with get_conn() as conn:
        existing = conn.execute("SELECT COUNT(*) AS n FROM subscribers").fetchone()
        if existing["n"] > 0:
            return 0
    inserted = 0
    for email in config.EMAIL_RECIPIENTS:
        if not email:
            continue
        try:
            add_subscriber(email=email, tier="paid", status="active")
            inserted += 1
        except sqlite3.IntegrityError:
            # Race or pre-existing row — fine, skip silently
            pass
    if inserted:
        logger.info("Seeded %d subscriber(s) from EMAIL_RECIPIENTS", inserted)
    return inserted
