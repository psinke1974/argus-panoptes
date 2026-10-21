"""Weekly digest of stale work packages, sent to a chat webhook and/or by e-mail.

Runs inside the glue: a small scheduler checks once a minute whether the digest is due (configured weekday and hour,
at most once per day, remembered across restarts). A manual trigger exists at POST /digest/send."""
import asyncio
import datetime as dt
import json
import logging
import re
import smtplib
from email.message import EmailMessage
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

from . import config
from .stale import stale_markdown

log = logging.getLogger("panoptes.digest")


def configured() -> bool:
    return bool(config.DIGEST_PROJECTS and (config.DIGEST_WEBHOOK_URL or (config.DIGEST_SMTP_HOST and config.DIGEST_MAIL_TO)))


def to_slack(md: str) -> str:
    """Markdown -> Slack mrkdwn: headings as bold lines, **bold** as *bold*, [text](url) as <url|text>."""
    out = []
    for line in md.splitlines():
        line = re.sub(r"^#{1,6}\s+(.*)$", r"*\1*", line)
        line = re.sub(r"\*\*(.+?)\*\*", r"*\1*", line)
        line = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"<\2|\1>", line)
        out.append(line.replace("\n- ", "\n• ") if not line.startswith("- ") else "• " + line[2:])
    return "\n".join(out)


async def build_digest(op) -> tuple[str, int]:
    """(markdown, number of stale items across all configured projects)"""
    parts, total = [], 0
    for project in config.DIGEST_PROJECTS:
        try:
            items = await op.stale(project, config.DIGEST_DAYS)
        except Exception as e:  # one unreachable project must not block the others
            parts.append(f"# {project}\n_Could not read this project: {e}_\n")
            continue
        total += len(items)
        parts.append(stale_markdown(items, project, config.DIGEST_DAYS))
    return "\n\n".join(parts), total


async def send_webhook(text: str, client: httpx.AsyncClient | None = None) -> None:
    body = to_slack(text) if config.DIGEST_WEBHOOK_FORMAT == "slack" else text
    async with (client or httpx.AsyncClient(timeout=20)) as c:
        r = await c.post(config.DIGEST_WEBHOOK_URL, json={"text": body})
        r.raise_for_status()


def send_email(subject: str, text: str) -> None:
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, config.DIGEST_MAIL_FROM or config.DIGEST_SMTP_USER, ", ".join(config.DIGEST_MAIL_TO)
    msg.set_content(text)
    with smtplib.SMTP(config.DIGEST_SMTP_HOST, config.DIGEST_SMTP_PORT, timeout=30) as s:
        if config.DIGEST_SMTP_STARTTLS:
            s.starttls()
        if config.DIGEST_SMTP_USER:
            s.login(config.DIGEST_SMTP_USER, config.DIGEST_SMTP_PASSWORD)
        s.send_message(msg)


async def send_digest(op) -> dict:
    text, total = await build_digest(op)
    sent = []
    if config.DIGEST_WEBHOOK_URL:
        await send_webhook(text)
        sent.append("webhook")
    if config.DIGEST_SMTP_HOST and config.DIGEST_MAIL_TO:
        await asyncio.to_thread(send_email, f"Panoptes: {total} stale work package(s)", text)
        sent.append("email")
    return {"stale": total, "sent_via": sent}


def due(now: dt.datetime, last_sent: dt.date | None, weekday: int, hour: int) -> bool:
    """True on the configured weekday from the configured hour on, once per day."""
    return now.weekday() == weekday and now.hour >= hour and (last_sent is None or last_sent < now.date())


def load_last_sent() -> dt.date | None:
    try:
        return dt.date.fromisoformat(json.loads(Path(config.DIGEST_STATE_FILE).read_text())["last_sent"])
    except (OSError, ValueError, KeyError):
        return None


def save_last_sent(day: dt.date) -> None:
    try:
        p = Path(config.DIGEST_STATE_FILE)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"last_sent": day.isoformat()}))
    except OSError:
        log.warning("could not persist the digest state; a restart on a digest day may send it twice")


async def scheduler(op) -> None:
    tz = ZoneInfo(config.DIGEST_TZ)
    last = load_last_sent()
    while True:
        await asyncio.sleep(60)
        now = dt.datetime.now(tz)
        if due(now, last, config.DIGEST_WEEKDAY, config.DIGEST_HOUR):
            try:
                log.info("weekly digest: %s", await send_digest(op))
                last = now.date()
                save_last_sent(last)
            except Exception:
                log.exception("weekly digest failed; retrying in a minute")
