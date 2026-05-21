"""Admin CLI — invite a new subscriber and email them a welcome link.

Usage
-----
    python invite.py user@example.com
    python invite.py user@example.com --name "Alice"
    python invite.py user@example.com --tier free --no-welcome
    python invite.py --list
    python invite.py user@example.com --status paused        # toggle existing
    python invite.py user@example.com --revoke                # delete subscriber

Membership requests (apply-for-access flow)
-------------------------------------------
    python invite.py --requests                  # list pending applications
    python invite.py --approve 7                 # approve by request id
    python invite.py --approve alice@example.com # approve by email
    python invite.py --reject 7                  # reject by id + send rejection email

Notes
-----
- Default tier is `paid` (since this is an invite-only system).
- Default behavior sends a welcome email (containing the first Magic Link).
- If the address already exists, you can update fields via --status / --tier;
  use --revoke to remove a subscriber entirely.
- --approve auto-creates/upgrades the subscriber to `paid` and sends the
  welcome email (same path as `invite.py <email>`).
"""

import argparse
import sys
import sqlite3
from datetime import datetime

import config
import storage
import subscribers
import auth


def _print_row(sub: subscribers.Subscriber) -> None:
    paid_flag = "✓ paid" if subscribers.is_paid(sub) else "  free"
    name = sub.name or ""
    print(f"  [{sub.id:>3}] {sub.email:<32} {sub.status:<8} {sub.tier:<5} "
          f"{paid_flag}   {name}")


def cmd_list(args: argparse.Namespace) -> int:
    with storage.get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM subscribers ORDER BY id"
        ).fetchall()
    if not rows:
        print("No subscribers yet. Run `python invite.py you@example.com` to add the first one.")
        return 0
    print(f"\n{len(rows)} subscriber(s):\n")
    print(f"  {'ID':>3}  {'email':<32} {'status':<8} {'tier':<5} {'access':<7}  name")
    print("  " + "-" * 70)
    for r in rows:
        _print_row(subscribers.Subscriber.from_row(r))
    print()
    return 0


def cmd_revoke(email: str) -> int:
    sub = subscribers.get_by_email(email)
    if not sub:
        print(f"× No subscriber with email {email!r}", file=sys.stderr)
        return 1
    with storage.get_conn() as conn:
        conn.execute("DELETE FROM sessions WHERE subscriber_id = ?", (sub.id,))
        conn.execute("DELETE FROM magic_links WHERE email = ?", (sub.email,))
        conn.execute("DELETE FROM subscribers WHERE id = ?", (sub.id,))
    print(f"✓ Revoked subscriber {sub.email} (id={sub.id}); sessions and tokens cleared.")
    return 0


def cmd_update(sub: subscribers.Subscriber,
               status: str | None, tier: str | None,
               name: str | None, paid_until: str | None) -> int:
    """Update an existing subscriber's fields and report the change."""
    fields, params = [], []
    if status: fields.append("status = ?"); params.append(status)
    if tier:   fields.append("tier = ?");   params.append(tier)
    if name is not None: fields.append("name = ?"); params.append(name or None)
    if paid_until is not None:
        fields.append("paid_until = ?"); params.append(paid_until or None)
    if not fields:
        print("× Email already exists. Pass --status/--tier/--name/--paid-until to update,")
        print("  or --revoke to delete.", file=sys.stderr)
        return 1
    fields.append("updated_at = ?")
    params.append(datetime.now().isoformat(timespec="seconds"))
    params.append(sub.id)
    with storage.get_conn() as conn:
        conn.execute(f"UPDATE subscribers SET {', '.join(fields)} WHERE id = ?", params)
    print(f"✓ Updated subscriber {sub.email}:")
    _print_row(subscribers.get_by_id(sub.id))  # type: ignore[arg-type]
    return 0


def cmd_add(args: argparse.Namespace) -> int:
    email = args.email.lower().strip()
    existing = subscribers.get_by_email(email)
    if existing:
        # Already on the list — fall through to update mode
        return cmd_update(existing, args.status, args.tier,
                          args.name, args.paid_until)

    try:
        sub = subscribers.add_subscriber(
            email=email,
            name=args.name or "",
            tier=args.tier,
            status=args.status,
            paid_until=args.paid_until or None,
        )
    except sqlite3.IntegrityError as e:
        print(f"× Failed to add {email}: {e}", file=sys.stderr)
        return 1

    print(f"✓ Added subscriber:")
    _print_row(sub)

    if args.no_welcome:
        print("  (welcome email skipped — pass without --no-welcome to send)")
        return 0

    try:
        token = subscribers.create_magic_link(email)
        auth.send_welcome_email(email, args.name or "", token)
        print(f"✓ Welcome email sent to {email}")
        print(f"  Magic Link valid for {config.MAGIC_LINK_TTL_MINUTES} minutes")
    except Exception as e:
        print(f"⚠ Subscriber added but welcome email failed: {e}", file=sys.stderr)
        print(f"  You can resend later via the /login page", file=sys.stderr)
        return 2
    return 0


def cmd_list_requests() -> int:
    pending = subscribers.list_pending_requests()
    if not pending:
        print("No pending membership requests.")
        return 0
    print(f"\n{len(pending)} pending request(s):\n")
    print(f"  {'ID':>3}  {'email':<32} {'submitted':<20}  name / source")
    print("  " + "-" * 80)
    for r in pending:
        meta = r.name or ""
        if r.source:
            meta = f"{meta} | {r.source}" if meta else r.source
        print(f"  [{r.id:>3}] {r.email:<32} {r.created_at:<20}  {meta}")
        if r.reason:
            # Indent reason for readability; truncate over-long bodies
            reason = r.reason.strip().replace("\n", " ")
            if len(reason) > 200:
                reason = reason[:200] + "…"
            print(f"        理由: {reason}")
    print()
    return 0


def _resolve_request(ident: str) -> "subscribers.MembershipRequest | None":
    """Accept either a numeric id or an email and return the matching request."""
    ident = (ident or "").strip()
    if not ident:
        return None
    if ident.isdigit():
        return subscribers.get_request_by_id(int(ident))
    return subscribers.get_pending_request_by_email(ident)


def cmd_approve_request(ident: str, reviewed_by: str) -> int:
    req = _resolve_request(ident)
    if req is None:
        print(f"× No request matches {ident!r}", file=sys.stderr)
        return 1
    if req.status != "pending":
        print(f"× Request {req.id} is already {req.status}.", file=sys.stderr)
        return 1
    try:
        req, sub = subscribers.approve_request(req.id, reviewed_by=reviewed_by)
    except ValueError as e:
        print(f"× {e}", file=sys.stderr)
        return 1
    print(f"✓ Approved request {req.id} for {req.email}")
    _print_row(sub)
    try:
        token = subscribers.create_magic_link(sub.email)
        auth.send_welcome_email(sub.email, sub.name or "", token)
        print(f"✓ Welcome email sent to {sub.email}")
    except Exception as e:
        print(f"⚠ Approval saved but welcome email failed: {e}", file=sys.stderr)
        return 2
    return 0


def cmd_reject_request(ident: str, reviewed_by: str) -> int:
    req = _resolve_request(ident)
    if req is None:
        print(f"× No request matches {ident!r}", file=sys.stderr)
        return 1
    if req.status != "pending":
        print(f"× Request {req.id} is already {req.status}.", file=sys.stderr)
        return 1
    req = subscribers.reject_request(req.id, reviewed_by=reviewed_by)
    print(f"✓ Rejected request {req.id} for {req.email}")
    try:
        auth.send_application_rejected_email(req.email, req.name or "")
        print(f"✓ Rejection email sent to {req.email}")
    except Exception as e:
        print(f"⚠ Rejection saved but email failed: {e}", file=sys.stderr)
        return 2
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Invite a new subscriber or manage existing ones.",
        epilog="Example: python invite.py alice@example.com --name Alice",
    )
    p.add_argument("email", nargs="?",
                   help="Email address to invite (omit when using --list)")
    p.add_argument("--list", action="store_true",
                   help="List all current subscribers and exit")
    p.add_argument("--name", default="",
                   help="Display name (optional)")
    p.add_argument("--tier", choices=["free", "paid"], default="paid",
                   help="Subscription tier (default: paid)")
    p.add_argument("--status",
                   choices=["active", "invited", "paused", "churned"],
                   default="active",
                   help="Account status (default: active)")
    p.add_argument("--paid-until",
                   help="Optional ISO date for paid expiry (e.g. 2027-01-01)")
    p.add_argument("--no-welcome", action="store_true",
                   help="Skip the welcome email (no Magic Link sent)")
    p.add_argument("--revoke", action="store_true",
                   help="Delete the subscriber and all their sessions/tokens")
    p.add_argument("--requests", action="store_true",
                   help="List pending membership requests and exit")
    p.add_argument("--approve", metavar="ID|EMAIL",
                   help="Approve a pending request (by id or email); "
                        "creates paid subscriber + sends welcome email")
    p.add_argument("--reject", metavar="ID|EMAIL",
                   help="Reject a pending request (no email sent)")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    # Ensure schema is in place before any DB op
    storage.init_db()

    if args.list:
        return cmd_list(args)
    if args.requests:
        return cmd_list_requests()
    reviewed_by = "cli"
    if args.approve:
        return cmd_approve_request(args.approve, reviewed_by=reviewed_by)
    if args.reject:
        return cmd_reject_request(args.reject, reviewed_by=reviewed_by)
    if not args.email:
        print("× email argument is required (or use --list / --requests / "
              "--approve / --reject)", file=sys.stderr)
        return 1
    if args.revoke:
        return cmd_revoke(args.email.lower().strip())
    return cmd_add(args)


if __name__ == "__main__":
    sys.exit(main())
