"""Authentication helpers — Magic Link email + FastAPI session dependencies.

Two responsibilities, kept together because they share the SMTP and session
plumbing:

1. Sending the one-time Magic Link / welcome emails.
2. FastAPI `Depends(...)` helpers that resolve the session cookie to a
   Subscriber and gate routes by tier.

Design notes
------------
- We deliberately do NOT import notifier.py here — notifier.py is the
  *digest* sender; this module owns the *transactional* email path. They
  share Gmail SMTP credentials via config but keep separate templates.
- `require_paid` and `require_subscriber` redirect (not 401) for HTML
  routes so the browser flow is "navigate to /login?next=...". For
  JSON/API routes, hosts can pass `as_api=True` (or use the API-flavored
  dep below) to get a clean 401 instead.
"""

import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional

from fastapi import Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse

import config
import subscribers
from subscribers import Subscriber

logger = logging.getLogger(__name__)


# ── Email senders ──────────────────────────────────────────────────────────

def _send_smtp(to_email: str, subject: str, html_body: str, text_body: str = "") -> None:
    """Send a single transactional email via Gmail SMTP. Raises on failure."""
    if not (config.EMAIL_SENDER and config.EMAIL_APP_PASSWORD):
        raise RuntimeError("EMAIL_SENDER / EMAIL_APP_PASSWORD not configured")
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = config.EMAIL_SENDER
    msg["To"]      = to_email
    if text_body:
        msg.attach(MIMEText(text_body, "plain"))
    msg.attach(MIMEText(html_body, "html"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(config.EMAIL_SENDER, config.EMAIL_APP_PASSWORD)
        server.sendmail(config.EMAIL_SENDER, to_email, msg.as_string())


def _magic_link_url(token: str, next_path: str = "/") -> str:
    next_q = ""
    if next_path and next_path != "/":
        from urllib.parse import quote
        next_q = f"&next={quote(next_path)}"
    return f"{config.BASE_URL}/auth/verify?token={token}{next_q}"


def send_magic_link_email(email: str, token: str, next_path: str = "/") -> None:
    """Email a one-time login link to the given address."""
    url = _magic_link_url(token, next_path)
    ttl = config.MAGIC_LINK_TTL_MINUTES
    html = f"""<html><body style='font-family:-apple-system,BlinkMacSystemFont,sans-serif;
  max-width:520px;margin:auto;padding:32px;background:#fff;color:#222;'>
  <div style='background:#0f3460;color:#fff;padding:18px 22px;border-radius:10px 10px 0 0;'>
    <h1 style='margin:0;font-size:18px;'>看牛韵新闻 · 登录链接</h1>
  </div>
  <div style='padding:24px;border:1px solid #e5e7eb;border-top:none;border-radius:0 0 10px 10px;'>
    <p style='font-size:14px;line-height:1.6;color:#374151;margin:0 0 16px;'>
      点击下方按钮即可登录。此链接 <b>{ttl} 分钟内</b>有效,且只能使用一次。
    </p>
    <p style='margin:24px 0;'>
      <a href='{url}' style='display:inline-block;font-size:14px;color:#fff;
         background:#0f3460;padding:12px 24px;border-radius:8px;
         text-decoration:none;font-weight:600;'>登录</a>
    </p>
    <p style='font-size:12px;color:#888;line-height:1.5;margin:16px 0 0;'>
      如果按钮无法点击,请复制以下链接到浏览器:<br>
      <span style='color:#555;word-break:break-all;'>{url}</span>
    </p>
    <p style='font-size:12px;color:#aaa;margin:24px 0 0;border-top:1px solid #eee;padding-top:16px;'>
      如果你没有请求登录,可以忽略这封邮件。
    </p>
  </div>
</body></html>"""
    text = f"看牛韵新闻 — 登录链接 ({ttl} 分钟内有效):\n{url}\n"
    _send_smtp(email, "看牛韵新闻 · 登录链接", html, text)


def send_login_code_email(email: str, code: str) -> None:
    """Email a 6-digit verification code (alternative to Magic Link)."""
    ttl = config.LOGIN_CODE_TTL_MINUTES
    # Display the code as "123 456" for easier reading.
    code_spaced = f"{code[:3]} {code[3:]}" if len(code) == 6 else code
    html = f"""<html><body style='font-family:-apple-system,BlinkMacSystemFont,sans-serif;
  max-width:520px;margin:auto;padding:32px;background:#fff;color:#222;'>
  <div style='background:#0f3460;color:#fff;padding:18px 22px;border-radius:10px 10px 0 0;'>
    <h1 style='margin:0;font-size:18px;'>看牛韵新闻 · 登录验证码</h1>
  </div>
  <div style='padding:24px;border:1px solid #e5e7eb;border-top:none;border-radius:0 0 10px 10px;'>
    <p style='font-size:14px;line-height:1.6;color:#374151;margin:0 0 16px;'>
      你的登录验证码,请在 <b>{ttl} 分钟内</b>使用,且只能使用一次。
    </p>
    <div style='margin:24px 0;text-align:center;'>
      <span style='display:inline-block;font-size:32px;letter-spacing:8px;
                   font-family:ui-monospace,SFMono-Regular,Menlo,monospace;
                   color:#0f3460;background:#f4f6fb;padding:18px 28px;
                   border-radius:10px;font-weight:700;'>{code_spaced}</span>
    </div>
    <p style='font-size:12px;color:#888;line-height:1.5;margin:16px 0 0;'>
      在登录页面输入上述 6 位数字即可完成登录。
    </p>
    <p style='font-size:12px;color:#aaa;margin:24px 0 0;border-top:1px solid #eee;padding-top:16px;'>
      如果你没有请求登录,可以忽略这封邮件,你的账号是安全的。
    </p>
  </div>
</body></html>"""
    text = f"看牛韵新闻 — 登录验证码 ({ttl} 分钟内有效): {code}\n"
    _send_smtp(email, "看牛韵新闻 · 登录验证码", html, text)


def send_welcome_email(email: str, name: str, token: str) -> None:
    """Welcome a newly invited subscriber with their first Magic Link."""
    url = _magic_link_url(token, "/account")
    greeting = f"嗨 {name}," if name else "你好,"
    html = f"""<html><body style='font-family:-apple-system,BlinkMacSystemFont,sans-serif;
  max-width:520px;margin:auto;padding:32px;background:#fff;color:#222;'>
  <div style='background:#0f3460;color:#fff;padding:20px 24px;border-radius:10px 10px 0 0;'>
    <h1 style='margin:0;font-size:20px;'>欢迎加入「看牛韵新闻」🎉</h1>
  </div>
  <div style='padding:24px;border:1px solid #e5e7eb;border-top:none;border-radius:0 0 10px 10px;'>
    <p style='font-size:14px;line-height:1.7;color:#374151;'>{greeting}</p>
    <p style='font-size:14px;line-height:1.7;color:#374151;'>
      你已被加入订阅名单。从今天起,你会在每天 07:00 / 12:00 / 20:00
      收到一封新闻 digest,涵盖 AI、美股、创投、宏观、预测市场等。
    </p>
    <p style='font-size:14px;line-height:1.7;color:#374151;'>
      点击下方按钮设置登录,即可访问会员专享的研究 dashboard:
    </p>
    <p style='margin:24px 0;'>
      <a href='{url}' style='display:inline-block;font-size:14px;color:#fff;
         background:#0f3460;padding:12px 24px;border-radius:8px;
         text-decoration:none;font-weight:600;'>设置登录 →</a>
    </p>
    <p style='font-size:12px;color:#888;line-height:1.5;'>
      这个链接 {config.MAGIC_LINK_TTL_MINUTES} 分钟内有效。如果错过,
      下次需要登录时,在 <a href='{config.BASE_URL}/login' style='color:#0f3460;'>{config.BASE_URL}/login</a>
      输入邮箱即可获取新链接。
    </p>
  </div>
</body></html>"""
    text = (f"{greeting}\n欢迎加入「看牛韵新闻」。\n"
            f"设置登录:{url}\n(链接 {config.MAGIC_LINK_TTL_MINUTES} 分钟内有效)\n")
    _send_smtp(email, "欢迎加入「看牛韵新闻」", html, text)


def send_application_received_email(email: str, name: str = "") -> None:
    """Confirm to the applicant that we've received their membership request."""
    greeting = f"嗨 {name}," if name else "你好,"
    html = f"""<html><body style='font-family:-apple-system,BlinkMacSystemFont,sans-serif;
  max-width:520px;margin:auto;padding:32px;background:#fff;color:#222;'>
  <div style='background:#0f3460;color:#fff;padding:18px 22px;border-radius:10px 10px 0 0;'>
    <h1 style='margin:0;font-size:18px;'>看牛韵新闻 · 已收到你的会员申请</h1>
  </div>
  <div style='padding:24px;border:1px solid #e5e7eb;border-top:none;border-radius:0 0 10px 10px;'>
    <p style='font-size:14px;line-height:1.7;color:#374151;'>{greeting}</p>
    <p style='font-size:14px;line-height:1.7;color:#374151;'>
      我们已经收到你的会员申请,稍后会人工审核。审核通过后,
      你会收到一封欢迎邮件,内含登录链接,即可访问会员专享内容。
    </p>
    <p style='font-size:14px;line-height:1.7;color:#374151;'>
      会员专享: AI 板块轮动 dashboard、关键词智能匹配新闻、
      每天 07:00 / 12:00 / 20:00 三封 digest。
    </p>
    <p style='font-size:12px;color:#aaa;margin:24px 0 0;border-top:1px solid #eee;padding-top:16px;'>
      如果你没有提交过申请,可以忽略这封邮件。
    </p>
  </div>
</body></html>"""
    text = (f"{greeting}\n我们已经收到你的会员申请,稍后会人工审核。\n"
            "审核通过后,你会收到一封欢迎邮件,内含登录链接。\n")
    _send_smtp(email, "看牛韵新闻 · 已收到你的会员申请", html, text)


# ── FastAPI dependencies ───────────────────────────────────────────────────

def current_subscriber(request: Request) -> Optional[Subscriber]:
    """Resolve the session cookie to a Subscriber. None if not logged in."""
    sid = request.cookies.get(config.SESSION_COOKIE_NAME)
    return subscribers.get_by_session(sid)


def require_subscriber(request: Request) -> Subscriber:
    """Reject unauthenticated requests.

    HTML routes get a 302 to /login?next=<path>. API routes (path starts
    with /api/) get a clean 401 JSON response.
    """
    sub = current_subscriber(request)
    if sub is not None:
        return sub
    if request.url.path.startswith("/api/"):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="login required")
    from urllib.parse import quote
    next_q = quote(request.url.path) if request.url.path != "/" else ""
    target = f"/login?next={next_q}" if next_q else "/login"
    raise HTTPException(
        status_code=status.HTTP_302_FOUND,
        headers={"Location": target},
        detail="redirect to login",
    )


def require_admin(request: Request,
                  sub: Subscriber = Depends(require_subscriber)) -> Subscriber:
    """Reject non-admin users. Admin = email in config.ADMIN_EMAILS."""
    if subscribers.is_admin(sub):
        return sub
    if request.url.path.startswith("/api/"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail="admin required")
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="admin access required",
    )


def require_paid(request: Request,
                 sub: Subscriber = Depends(require_subscriber)) -> Subscriber:
    """Reject non-paid subscribers.

    Logged-in but unpaid users are sent to /account so they see their
    current tier + an upgrade CTA (when payment is wired up).
    """
    if subscribers.is_paid(sub):
        return sub
    if request.url.path.startswith("/api/"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail="paid tier required")
    raise HTTPException(
        status_code=status.HTTP_302_FOUND,
        headers={"Location": "/account?paywall=1"},
        detail="redirect to paywall",
    )
