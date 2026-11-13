"""Notifications: in-app (DB + websocket), Telegram and e-mail, controlled per event kind."""
from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage

import httpx
from sqlalchemy import select

from app.core.config import get_settings
from app.db.models import Notification, NotificationSetting
from app.services.events import publish

log = logging.getLogger("notify")

KINDS = ["SIGNAL_BUY", "SIGNAL_SELL", "SIGNAL_A_PLUS", "TRADE_EXECUTED", "TRADE_REJECTED", "TRADE_CLOSED", "STOP_LOSS_HIT",
         "TAKE_PROFIT_HIT", "BREAK_EVEN", "DAILY_LOSS_LIMIT", "DRAWDOWN_WARNING", "MT5_DISCONNECTED", "MT5_RECONNECTED",
         "CLAUDE_ERROR", "RISK_REJECTION", "EXECUTION_FAILURE", "SERVER_FAILURE", "KILL_SWITCH"]
CRITICAL = {"DAILY_LOSS_LIMIT", "MT5_DISCONNECTED", "EXECUTION_FAILURE", "SERVER_FAILURE", "KILL_SWITCH", "DRAWDOWN_WARNING"}


def _settings_for(db, kind: str) -> NotificationSetting:
    s = db.scalar(select(NotificationSetting).where(NotificationSetting.kind == kind))
    if s is None:
        s = NotificationSetting(kind=kind, in_app=True, telegram=kind in CRITICAL, email=kind in CRITICAL)
    return s


def send_telegram(text: str) -> bool:
    cfg = get_settings()
    if not (cfg.telegram_bot_token and cfg.telegram_chat_id):
        return False
    try:
        r = httpx.post(f"https://api.telegram.org/bot{cfg.telegram_bot_token}/sendMessage",
                       json={"chat_id": cfg.telegram_chat_id, "text": text[:4000]}, timeout=5)
        return r.status_code == 200
    except httpx.HTTPError as exc:
        log.warning("telegram failed: %s", exc)
        return False


def send_email(subject: str, body: str) -> bool:
    cfg = get_settings()
    if not (cfg.smtp_host and cfg.smtp_to and cfg.smtp_from):
        return False
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, cfg.smtp_from, cfg.smtp_to
    msg.set_content(body)
    try:
        with smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=10) as s:
            s.starttls()
            if cfg.smtp_user:
                s.login(cfg.smtp_user, cfg.smtp_password)
            s.send_message(msg)
        return True
    except (OSError, smtplib.SMTPException) as exc:
        log.warning("email failed: %s", exc)
        return False


def notify(db, kind: str, title: str, body: str = "", data: dict | None = None, severity: str | None = None) -> Notification:
    st = _settings_for(db, kind)
    sev = severity or ("critical" if kind in CRITICAL else "info")
    channels = []
    if st.telegram and send_telegram(f"[RR] {title}\n{body}"):
        channels.append("telegram")
    if st.email and send_email(f"[RR] {title}", body):
        channels.append("email")
    n = Notification(kind=kind, severity=sev, title=title, body=body, data=data or {}, channels=channels + ["in_app"])
    if st.in_app:
        db.add(n)
        db.flush()
        publish("notification", {"id": n.id, "kind": kind, "severity": sev, "title": title, "body": body})
    return n
