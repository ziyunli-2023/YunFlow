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


# ── Membership requests (apply-for-access flow) ───────────────────────────
#
# Anyone (member or not) can submit a request via /login → 申请会员 tab. The
# row lands in `membership_requests` with status='pending'. An admin (CLI
# invite.py or /admin web page) reviews and either approves (creates a paid
# subscriber + sends welcome email) or rejects (no email).

@dataclass
class MembershipRequest:
    id: int
    email: str
    name: Optional[str]
    reason: Optional[str]
    source: Optional[str]
    status: str
    created_at: str
    reviewed_at: Optional[str] = None
    reviewed_by: Optional[str] = None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "MembershipRequest":
        return cls(
            id=row["id"],
            email=row["email"],
            name=row["name"],
            reason=row["reason"],
            source=row["source"],
            status=row["status"],
            created_at=row["created_at"],
            reviewed_at=row["reviewed_at"],
            reviewed_by=row["reviewed_by"],
        )


class DuplicatePendingRequestError(Exception):
    """Raised when the same email already has a pending request."""


def create_membership_request(email: str, name: str = "", reason: str = "",
                              source: str = "") -> MembershipRequest:
    """Insert a new pending application. Raises DuplicatePendingRequestError
    if a pending row already exists for the same email (to prevent spam)."""
    email = (email or "").lower().strip()
    if not email:
        raise ValueError("email is required")
    with get_conn() as conn:
        existing = conn.execute(
            "SELECT id FROM membership_requests "
            "WHERE email = ? AND status = 'pending'",
            (email,)
        ).fetchone()
        if existing:
            raise DuplicatePendingRequestError(
                f"a pending request already exists for {email}"
            )
        cur = conn.execute(
            """INSERT INTO membership_requests
               (email, name, reason, source, status, created_at)
               VALUES (?, ?, ?, ?, 'pending', ?)""",
            (email, (name or None), (reason or None), (source or None), _now())
        )
        req_id = cur.lastrowid
        row = conn.execute(
            "SELECT * FROM membership_requests WHERE id = ?", (req_id,)
        ).fetchone()
    logger.info("Membership request received: %s (id=%d)", email, req_id)
    return MembershipRequest.from_row(row)


def list_pending_requests() -> list[MembershipRequest]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM membership_requests "
            "WHERE status = 'pending' ORDER BY created_at"
        ).fetchall()
    return [MembershipRequest.from_row(r) for r in rows]


def list_all_requests(limit: int = 100) -> list[MembershipRequest]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM membership_requests "
            "ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
    return [MembershipRequest.from_row(r) for r in rows]


def get_request_by_id(req_id: int) -> Optional[MembershipRequest]:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM membership_requests WHERE id = ?", (req_id,)
        ).fetchone()
    return MembershipRequest.from_row(row) if row else None


def get_pending_request_by_email(email: str) -> Optional[MembershipRequest]:
    email = (email or "").lower().strip()
    if not email:
        return None
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM membership_requests "
            "WHERE email = ? AND status = 'pending' "
            "ORDER BY created_at DESC LIMIT 1", (email,)
        ).fetchone()
    return MembershipRequest.from_row(row) if row else None


def approve_request(req_id: int, reviewed_by: str = "") -> tuple[MembershipRequest, Subscriber]:
    """Mark request approved and create/upgrade the subscriber to paid.

    Returns (request, subscriber). Idempotent on the subscriber side: if a
    subscriber row already exists for the email, it is upgraded to
    tier='paid' status='active' instead of erroring.
    Raises ValueError if req_id is unknown or not pending.
    """
    req = get_request_by_id(req_id)
    if req is None:
        raise ValueError(f"unknown request id: {req_id}")
    if req.status != "pending":
        raise ValueError(f"request {req_id} is {req.status}, not pending")
    now = _now()
    # Create or upgrade the subscriber
    existing = get_by_email(req.email)
    if existing is None:
        sub = add_subscriber(
            email=req.email, name=req.name or "",
            tier="paid", status="active",
        )
    else:
        with get_conn() as conn:
            conn.execute(
                "UPDATE subscribers SET tier='paid', status='active', "
                "updated_at=? WHERE id=?",
                (now, existing.id)
            )
        sub = get_by_id(existing.id)  # type: ignore[assignment]
    with get_conn() as conn:
        conn.execute(
            "UPDATE membership_requests SET status='approved', "
            "reviewed_at=?, reviewed_by=? WHERE id=?",
            (now, (reviewed_by or None), req_id)
        )
    logger.info("Membership approved: %s (req=%d, sub=%d, by=%s)",
                req.email, req_id, sub.id, reviewed_by or "?")
    return get_request_by_id(req_id), sub  # type: ignore[return-value]


def reject_request(req_id: int, reviewed_by: str = "") -> MembershipRequest:
    """Mark request rejected. No email is sent."""
    req = get_request_by_id(req_id)
    if req is None:
        raise ValueError(f"unknown request id: {req_id}")
    if req.status != "pending":
        raise ValueError(f"request {req_id} is {req.status}, not pending")
    with get_conn() as conn:
        conn.execute(
            "UPDATE membership_requests SET status='rejected', "
            "reviewed_at=?, reviewed_by=? WHERE id=?",
            (_now(), (reviewed_by or None), req_id)
        )
    logger.info("Membership rejected: %s (req=%d, by=%s)",
                req.email, req_id, reviewed_by or "?")
    return get_request_by_id(req_id)  # type: ignore[return-value]


# ── Admin check (BUSINESS RULE) ────────────────────────────────────────────

def is_admin(sub: Optional[Subscriber]) -> bool:
    """True if `sub.email` is in config.ADMIN_EMAILS allowlist."""
    if sub is None:
        return False
    if not config.ADMIN_EMAILS:
        return False
    return (sub.email or "").lower() in config.ADMIN_EMAILS


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
