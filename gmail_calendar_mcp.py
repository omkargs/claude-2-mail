#!/usr/bin/env python3
"""
Gmail + Google Calendar MCP Server
===================================
Local MCP server for Gmail (IMAP/SMTP) and Google Calendar.
Run via stdio — register in your MCP client config.

Tools:
  mail_list         — List recent emails
  mail_read         — Read full email by UID
  mail_search       — Search emails
  mail_draft        — Draft a reply or new email
  mail_send         — Send an approved email
  mail_auto_reply   — Auto-reply to whitelisted senders
  calendar_list     — List upcoming events
  calendar_create   — Create calendar event
  calendar_update   — Update/delete calendar event

Config: ~/.config/claude-2-mail/config.json
Secrets: ~/.config/claude-2-mail/.secrets (chmod 600)
"""

import asyncio
import email
import email.utils
import imaplib
import json
import logging
import os
import re
import smtplib
import ssl
import uuid
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

CONFIG_PATH = Path(
    os.environ.get("GMAIL_MAIL_CONFIG", "~/.config/claude-2-mail/config.json")
).expanduser()


class _RedactFormatter(logging.Formatter):
    """Redact sensitive data (emails, tokens, passwords) from log output."""

    _email_re = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")
    _token_re = re.compile(r'(token|password|secret|key)["\s:=]+["\']?([^\s"\'&]+)', re.IGNORECASE)

    def format(self, record: logging.LogRecord) -> str:
        msg = super().format(record)
        msg = self._email_re.sub("[REDACTED_EMAIL]", msg)
        msg = self._token_re.sub(r"\1=[REDACTED]", msg)
        return msg


log = logging.getLogger("gmail-calendar-mcp")
_handler = logging.StreamHandler()
_handler.setFormatter(_RedactFormatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
log.addHandler(_handler)
log.setLevel(logging.INFO)


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        raise FileNotFoundError(
            f"Config not found at {CONFIG_PATH}\n\n"
            "Fix: Copy template and configure:\n"
            f"  mkdir -p $(dirname {CONFIG_PATH})\n"
            f"  cp /path/to/claude-2-mail/config/config.template.json {CONFIG_PATH}\n"
            f"  nano {CONFIG_PATH}\n"
        )
    with open(CONFIG_PATH) as f:
        cfg = json.load(f)
    env_password = os.environ.get("GMAIL_APP_PASSWORD", "")
    if not env_password:
        raise EnvironmentError(
            "GMAIL_APP_PASSWORD env var not set.\n\n"
            "Fix: Add to your shell profile (~/.bashrc or ~/.zshrc):\n"
            "  export GMAIL_APP_PASSWORD='your-16-char-app-password'\n\n"
            "Get yours at: https://myaccount.google.com/apppasswords\n"
            "(Requires 2-Step Verification enabled)\n"
        )
    cfg["email"]["password"] = env_password
    return cfg


# ---------------------------------------------------------------------------
# IMAP helpers
# ---------------------------------------------------------------------------


def imap_connect(cfg: dict) -> imaplib.IMAP4_SSL:
    ctx = ssl.create_default_context()
    conn = imaplib.IMAP4_SSL(cfg["email"]["imap_host"], cfg["email"]["imap_port"], ssl_context=ctx)
    conn.login(cfg["email"]["address"], cfg["email"]["password"])
    return conn


def imap_logout(conn: imaplib.IMAP4_SSL) -> None:
    """Safely logout from IMAP, ignoring errors."""
    try:
        conn.logout()
    except Exception:
        pass


def imap_search(conn: imaplib.IMAP4_SSL, criteria: str) -> list[str]:
    status, data = conn.search(None, criteria)
    if status != "OK" or not data[0]:
        return []
    return data[0].decode().split()


def fetch_email(conn: imaplib.IMAP4_SSL, uid: str) -> dict | None:
    status, data = conn.fetch(uid.encode(), "(RFC822)")
    if status != "OK" or not data or not data[0] or not data[0][1]:
        return None
    msg = email.message_from_bytes(data[0][1])
    subject = msg.get("Subject", "(no subject)")
    sender = msg.get("From", "unknown")
    to = msg.get("To", "")
    date_str = msg.get("Date", "")
    date_parsed = ""
    try:
        parsed = email.utils.parsedate_to_datetime(date_str)
        date_parsed = parsed.isoformat()
    except Exception:
        date_parsed = date_str

    body = ""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                charset = part.get_content_charset() or "utf-8"
                body = part.get_payload(decode=True).decode(charset, errors="replace")
                break
    else:
        charset = msg.get_content_charset() or "utf-8"
        body = msg.get_payload(decode=True).decode(charset, errors="replace")

    return {
        "uid": uid,
        "subject": subject,
        "from": sender,
        "to": to,
        "date": date_parsed,
        "body": body.strip(),
    }


def _imap_escape(value: str) -> str:
    """Escape double quotes in user input to prevent IMAP injection."""
    return value.replace('"', '\\"')


def build_search_query(
    sender: str | None = None,
    subject: str | None = None,
    body: str | None = None,
    since: str | None = None,
    before: str | None = None,
    unread_only: bool = False,
) -> str:
    """Build IMAP search criteria string."""
    parts = []
    if unread_only:
        parts.append("UNSEEN")
    if sender:
        parts.append(f'FROM "{_imap_escape(sender)}"')
    if subject:
        parts.append(f'SUBJECT "{_imap_escape(subject)}"')
    if body:
        parts.append(f'BODY "{_imap_escape(body)}"')
    if since:
        parts.append(f'SINCE "{_imap_escape(since)}"')
    if before:
        parts.append(f'BEFORE "{_imap_escape(before)}"')
    if not parts:
        parts.append("ALL")
    return " ".join(parts)


# ---------------------------------------------------------------------------
# SMTP helpers
# ---------------------------------------------------------------------------


def smtp_send(
    cfg: dict, to: str, subject: str, body: str, reply_to_msg_id: str | None = None
) -> dict:
    ctx = ssl.create_default_context()
    with smtplib.SMTP_SSL(
        cfg["email"]["smtp_host"], cfg["email"]["smtp_port"], context=ctx
    ) as smtp:
        smtp.login(cfg["email"]["address"], cfg["email"]["password"])

        msg = MIMEMultipart()
        msg["From"] = f'{cfg["email"]["display_name"]} <{cfg["email"]["address"]}>'
        msg["To"] = to
        msg["Subject"] = subject
        if reply_to_msg_id:
            msg["In-Reply-To"] = reply_to_msg_id
            msg["References"] = reply_to_msg_id
        msg.attach(MIMEText(body, "plain", "utf-8"))

        smtp.send_message(msg)

    return {
        "status": "sent",
        "to": to,
        "subject": subject,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# Draft store (in-memory, per session)
# ---------------------------------------------------------------------------

_drafts: dict[str, dict] = {}


def store_draft(to: str, subject: str, body: str, reply_uid: str | None = None) -> str:
    draft_id = str(uuid.uuid4())[:12]
    _drafts[draft_id] = {
        "id": draft_id,
        "to": to,
        "subject": subject,
        "body": body,
        "reply_uid": reply_uid,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    return draft_id


# ---------------------------------------------------------------------------
# Auto-reply helpers
# ---------------------------------------------------------------------------


def is_whitelisted(cfg: dict, sender_email: str) -> dict | None:
    """Check if sender is in auto-reply whitelist. Returns sender config or None."""
    senders = cfg.get("auto_reply", {}).get("senders", [])
    match = re.search(r"<([^>]+)>", sender_email)
    clean = match.group(1).lower() if match else sender_email.lower().strip()
    for s in senders:
        if s["email"].lower() == clean:
            return s
    return None


# ---------------------------------------------------------------------------
# Google Calendar helpers
# ---------------------------------------------------------------------------

try:
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build as gbuild
    from google_auth_oauthlib.flow import InstalledAppFlow

    GOOGLE_LIBS = True
except ImportError:
    GOOGLE_LIBS = False


def _get_calendar_service(cfg: dict):
    """Get Google Calendar API service. Uses OAuth2 credentials."""
    config_dir = Path.home() / ".config" / "claude-2-mail"
    token_path = config_dir / "calendar_token.json"
    creds_path = config_dir / "credentials.json"

    if not GOOGLE_LIBS:
        raise RuntimeError(
            "Google API libraries not installed.\n"
            "Run: pip install google-api-python-client google-auth-httplib2 google-auth-oauthlib"
        )

    creds = None
    if token_path.exists():
        creds = Credentials.from_authorized_user_file(
            str(token_path), ["https://www.googleapis.com/auth/calendar"]
        )

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            from google.auth.transport.requests import Request

            creds.refresh(Request())
        else:
            if not creds_path.exists():
                raise FileNotFoundError(
                    f"Google OAuth credentials not found: {creds_path}\n"
                    "Download from Google Cloud Console → APIs & Services → Credentials → OAuth 2.0 → Desktop app"
                )
            flow = InstalledAppFlow.from_client_secrets_file(
                str(creds_path), ["https://www.googleapis.com/auth/calendar"]
            )
            creds = flow.run_local_server(port=0)
        with open(token_path, "w") as tf:
            tf.write(creds.to_json())

    return gbuild("calendar", "v3", credentials=creds)


# ---------------------------------------------------------------------------
# MCP Server
# ---------------------------------------------------------------------------

server = Server("gmail-calendar")


@server.list_tools()
async def list_tools():
    return [
        Tool(
            name="mail_list",
            description="List recent emails from a folder. Supports unread-only filter.",
            inputSchema={
                "type": "object",
                "properties": {
                    "folder": {
                        "type": "string",
                        "description": "IMAP folder (default: INBOX)",
                        "default": "INBOX",
                    },
                    "count": {
                        "type": "integer",
                        "description": "Max emails to return (default: 20)",
                        "default": 20,
                    },
                    "unread_only": {
                        "type": "boolean",
                        "description": "Only unread emails",
                        "default": False,
                    },
                },
            },
        ),
        Tool(
            name="mail_read",
            description="Read full email by UID. Returns subject, from, to, date, body.",
            inputSchema={
                "type": "object",
                "properties": {
                    "uid": {
                        "type": "string",
                        "description": "Email UID from mail_list or mail_search",
                    },
                    "folder": {
                        "type": "string",
                        "description": "IMAP folder (default: INBOX)",
                        "default": "INBOX",
                    },
                },
                "required": ["uid"],
            },
        ),
        Tool(
            name="mail_search",
            description="Search emails by sender, subject, body text, or date range.",
            inputSchema={
                "type": "object",
                "properties": {
                    "sender": {"type": "string", "description": "Filter by sender email/name"},
                    "subject": {"type": "string", "description": "Filter by subject text"},
                    "body": {"type": "string", "description": "Filter by body text"},
                    "since": {"type": "string", "description": "Since date (YYYY-MM-DD)"},
                    "before": {"type": "string", "description": "Before date (YYYY-MM-DD)"},
                    "unread_only": {
                        "type": "boolean",
                        "description": "Only unread",
                        "default": False,
                    },
                    "count": {
                        "type": "integer",
                        "description": "Max results (default: 20)",
                        "default": 20,
                    },
                    "folder": {
                        "type": "string",
                        "description": "IMAP folder (default: INBOX)",
                        "default": "INBOX",
                    },
                },
            },
        ),
        Tool(
            name="mail_draft",
            description="Draft a reply to an existing email or compose a new one. Returns draft for approval before sending.",
            inputSchema={
                "type": "object",
                "properties": {
                    "to": {"type": "string", "description": "Recipient email"},
                    "subject": {"type": "string", "description": "Email subject"},
                    "body": {"type": "string", "description": "Email body text"},
                    "reply_uid": {
                        "type": "string",
                        "description": "UID of email being replied to (auto-fills to/subject)",
                    },
                    "folder": {
                        "type": "string",
                        "description": "Folder for reply lookup (default: INBOX)",
                        "default": "INBOX",
                    },
                },
                "required": ["body"],
            },
        ),
        Tool(
            name="mail_send",
            description="Send an approved email. Use draft_id from mail_draft, or provide to/subject/body directly.",
            inputSchema={
                "type": "object",
                "properties": {
                    "draft_id": {"type": "string", "description": "Draft ID from mail_draft"},
                    "to": {"type": "string", "description": "Recipient (if not using draft_id)"},
                    "subject": {"type": "string", "description": "Subject (if not using draft_id)"},
                    "body": {"type": "string", "description": "Body (if not using draft_id)"},
                },
            },
        ),
        Tool(
            name="mail_auto_reply",
            description="Auto-reply to whitelisted senders. Checks inbox for unread emails from whitelisted senders and sends configured reply.",
            inputSchema={
                "type": "object",
                "properties": {
                    "folder": {
                        "type": "string",
                        "description": "IMAP folder (default: INBOX)",
                        "default": "INBOX",
                    },
                    "dry_run": {
                        "type": "boolean",
                        "description": "Show what would be replied to without sending",
                        "default": False,
                    },
                },
            },
        ),
        Tool(
            name="calendar_list",
            description="List upcoming calendar events.",
            inputSchema={
                "type": "object",
                "properties": {
                    "days": {
                        "type": "integer",
                        "description": "Number of days ahead (default: 7)",
                        "default": 7,
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Max events (default: 50)",
                        "default": 50,
                    },
                    "calendar_id": {
                        "type": "string",
                        "description": "Calendar ID (default: primary)",
                        "default": "primary",
                    },
                },
            },
        ),
        Tool(
            name="calendar_create",
            description="Create a new calendar event.",
            inputSchema={
                "type": "object",
                "properties": {
                    "summary": {"type": "string", "description": "Event title"},
                    "start": {
                        "type": "string",
                        "description": "Start time (ISO 8601, e.g. 2026-06-22T15:00:00+05:30)",
                    },
                    "end": {"type": "string", "description": "End time (ISO 8601)"},
                    "description": {"type": "string", "description": "Event description"},
                    "location": {"type": "string", "description": "Event location"},
                    "reminder_minutes": {
                        "type": "integer",
                        "description": "Reminder before event in minutes (default: 15)",
                        "default": 15,
                    },
                    "calendar_id": {
                        "type": "string",
                        "description": "Calendar ID (default: primary)",
                        "default": "primary",
                    },
                },
                "required": ["summary", "start", "end"],
            },
        ),
        Tool(
            name="calendar_update",
            description="Update or delete an existing calendar event.",
            inputSchema={
                "type": "object",
                "properties": {
                    "event_id": {"type": "string", "description": "Event ID to update/delete"},
                    "summary": {"type": "string", "description": "New title"},
                    "start": {"type": "string", "description": "New start time (ISO 8601)"},
                    "end": {"type": "string", "description": "New end time (ISO 8601)"},
                    "description": {"type": "string", "description": "New description"},
                    "location": {"type": "string", "description": "New location"},
                    "delete": {
                        "type": "boolean",
                        "description": "Delete this event instead of updating",
                        "default": False,
                    },
                    "calendar_id": {
                        "type": "string",
                        "description": "Calendar ID (default: primary)",
                        "default": "primary",
                    },
                },
                "required": ["event_id"],
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    try:
        cfg = load_config()

        # ---- MAIL LIST ----
        if name == "mail_list":
            folder = arguments.get("folder", "INBOX")
            count = arguments.get("count", 20)
            unread = arguments.get("unread_only", False)
            conn = imap_connect(cfg)
            try:
                conn.select(folder)
                criteria = "UNSEEN" if unread else "ALL"
                uids = imap_search(conn, criteria)
                uids = uids[-count:]
                uids.reverse()
                emails = []
                for uid in uids:
                    e = fetch_email(conn, uid)
                    if e:
                        e["body_preview"] = e["body"][:200] + (
                            "..." if len(e["body"]) > 200 else ""
                        )
                        del e["body"]
                        emails.append(e)
            finally:
                imap_logout(conn)
            return [
                TextContent(
                    type="text",
                    text=json.dumps(
                        {"status": "ok", "emails": emails, "count": len(emails)}, indent=2
                    ),
                )
            ]

        # ---- MAIL READ ----
        elif name == "mail_read":
            uid = arguments["uid"]
            folder = arguments.get("folder", "INBOX")
            conn = imap_connect(cfg)
            try:
                conn.select(folder)
                e = fetch_email(conn, uid)
            finally:
                imap_logout(conn)
            if not e:
                return [
                    TextContent(
                        type="text",
                        text=json.dumps(
                            {"status": "error", "message": f"Email UID {uid} not found"}, indent=2
                        ),
                    )
                ]
            return [
                TextContent(type="text", text=json.dumps({"status": "ok", "email": e}, indent=2))
            ]

        # ---- MAIL SEARCH ----
        elif name == "mail_search":
            folder = arguments.get("folder", "INBOX")
            count = arguments.get("count", 20)
            query = build_search_query(
                sender=arguments.get("sender"),
                subject=arguments.get("subject"),
                body=arguments.get("body"),
                since=arguments.get("since"),
                before=arguments.get("before"),
                unread_only=arguments.get("unread_only", False),
            )
            conn = imap_connect(cfg)
            try:
                conn.select(folder)
                uids = imap_search(conn, query)
                uids = uids[-count:]
                uids.reverse()
                emails = []
                for uid in uids:
                    e = fetch_email(conn, uid)
                    if e:
                        e["body_preview"] = e["body"][:200] + (
                            "..." if len(e["body"]) > 200 else ""
                        )
                        del e["body"]
                        emails.append(e)
            finally:
                imap_logout(conn)
            return [
                TextContent(
                    type="text",
                    text=json.dumps(
                        {"status": "ok", "emails": emails, "count": len(emails), "query": query},
                        indent=2,
                    ),
                )
            ]

        # ---- MAIL DRAFT ----
        elif name == "mail_draft":
            body = arguments["body"]
            to = arguments.get("to", "")
            subject = arguments.get("subject", "")
            reply_uid = arguments.get("reply_uid")

            if reply_uid:
                folder = arguments.get("folder", "INBOX")
                conn = imap_connect(cfg)
                try:
                    conn.select(folder)
                    original = fetch_email(conn, reply_uid)
                finally:
                    imap_logout(conn)
                if not original:
                    return [
                        TextContent(
                            type="text",
                            text=json.dumps(
                                {
                                    "status": "error",
                                    "message": f"Original email UID {reply_uid} not found in {folder}",
                                },
                                indent=2,
                            ),
                        )
                    ]
                to = to or original["from"]
                if not subject:
                    subj = original["subject"]
                    subject = subj if subj.startswith("Re: ") else f"Re: {subj}"

            draft_id = store_draft(to=to, subject=subject, body=body, reply_uid=reply_uid)
            draft = _drafts[draft_id]
            return [
                TextContent(
                    type="text",
                    text=json.dumps(
                        {
                            "status": "ok",
                            "draft": draft,
                            "message": "Draft created. Show to user for approval, then call mail_send with draft_id.",
                        },
                        indent=2,
                    ),
                )
            ]

        # ---- MAIL SEND ----
        elif name == "mail_send":
            draft_id = arguments.get("draft_id")
            if draft_id and draft_id in _drafts:
                draft = _drafts[draft_id]
                to = draft["to"]
                subject = draft["subject"]
                body = draft["body"]
            else:
                to = arguments.get("to", "")
                subject = arguments.get("subject", "")
                body = arguments.get("body", "")

            if not to:
                return [
                    TextContent(
                        type="text",
                        text=json.dumps(
                            {
                                "status": "error",
                                "message": "No recipient. Provide draft_id or to/subject/body.",
                            },
                            indent=2,
                        ),
                    )
                ]

            if not re.match(r"^[^@]+@[^@]+\.[^@]+$", to):
                return [
                    TextContent(
                        type="text",
                        text=json.dumps(
                            {"status": "error", "message": f"Invalid recipient email: {to}"},
                            indent=2,
                        ),
                    )
                ]

            result = smtp_send(cfg, to, subject, body)
            if draft_id and draft_id in _drafts:
                del _drafts[draft_id]
            return [TextContent(type="text", text=json.dumps(result, indent=2))]

        # ---- MAIL AUTO REPLY ----
        elif name == "mail_auto_reply":
            if not cfg.get("auto_reply", {}).get("enabled", False):
                return [
                    TextContent(
                        type="text",
                        text=json.dumps(
                            {
                                "status": "ok",
                                "message": "Auto-reply disabled in config",
                                "replied": [],
                            },
                            indent=2,
                        ),
                    )
                ]

            folder = arguments.get("folder", "INBOX")
            dry_run = arguments.get("dry_run", False)
            default_template = cfg.get("auto_reply", {}).get(
                "default_template", "Thanks for reaching out. I'll respond shortly."
            )
            replied = []

            conn = imap_connect(cfg)
            try:
                conn.select(folder)
                uids = imap_search(conn, "UNSEEN")
                for uid in uids:
                    e = fetch_email(conn, uid)
                    if not e:
                        continue
                    sender_cfg = is_whitelisted(cfg, e["from"])
                    if sender_cfg:
                        template = sender_cfg.get("template", default_template)
                        match = re.search(r"<([^>]+)>", e["from"])
                        reply_to = match.group(1) if match else e["from"].strip()
                        subj = e["subject"]
                        reply_subject = subj if subj.startswith("Re: ") else f"Re: {subj}"

                        if dry_run:
                            replied.append(
                                {
                                    "to": reply_to,
                                    "subject": reply_subject,
                                    "would_send": template,
                                    "dry_run": True,
                                }
                            )
                        else:
                            smtp_send(cfg, reply_to, reply_subject, template)
                            replied.append({"to": reply_to, "subject": reply_subject, "sent": True})
            finally:
                imap_logout(conn)
            return [
                TextContent(
                    type="text",
                    text=json.dumps(
                        {"status": "ok", "replied": replied, "count": len(replied)}, indent=2
                    ),
                )
            ]

        # ---- CALENDAR LIST ----
        elif name == "calendar_list":
            days = arguments.get("days", 7)
            max_results = arguments.get("max_results", 50)
            calendar_id = arguments.get("calendar_id", "primary")

            service = _get_calendar_service(cfg)
            now = datetime.now(timezone.utc).isoformat()
            end = (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()

            events_result = (
                service.events()
                .list(
                    calendarId=calendar_id,
                    timeMin=now,
                    timeMax=end,
                    maxResults=max_results,
                    singleEvents=True,
                    orderBy="startTime",
                )
                .execute()
            )

            events = events_result.get("items", [])
            formatted = []
            for ev in events:
                formatted.append(
                    {
                        "id": ev["id"],
                        "summary": ev.get("summary", "(no title)"),
                        "start": ev["start"].get("dateTime", ev["start"].get("date")),
                        "end": ev["end"].get("dateTime", ev["end"].get("date")),
                        "location": ev.get("location", ""),
                        "description": ev.get("description", ""),
                    }
                )
            return [
                TextContent(
                    type="text",
                    text=json.dumps(
                        {"status": "ok", "events": formatted, "count": len(formatted)}, indent=2
                    ),
                )
            ]

        # ---- CALENDAR CREATE ----
        elif name == "calendar_create":
            summary = arguments["summary"]
            start = arguments["start"]
            end = arguments["end"]
            calendar_id = arguments.get("calendar_id", "primary")
            reminder = arguments.get("reminder_minutes", 15)

            service = _get_calendar_service(cfg)
            event_body = {
                "summary": summary,
                "start": {"dateTime": start},
                "end": {"dateTime": end},
                "reminders": {
                    "useDefault": False,
                    "overrides": [{"method": "popup", "minutes": reminder}],
                },
            }
            if arguments.get("description"):
                event_body["description"] = arguments["description"]
            if arguments.get("location"):
                event_body["location"] = arguments["location"]

            created = service.events().insert(calendarId=calendar_id, body=event_body).execute()
            return [
                TextContent(
                    type="text",
                    text=json.dumps(
                        {
                            "status": "ok",
                            "event": {
                                "id": created["id"],
                                "summary": created["summary"],
                                "start": created["start"],
                                "end": created["end"],
                            },
                        },
                        indent=2,
                    ),
                )
            ]

        # ---- CALENDAR UPDATE ----
        elif name == "calendar_update":
            event_id = arguments["event_id"]
            calendar_id = arguments.get("calendar_id", "primary")
            service = _get_calendar_service(cfg)

            if arguments.get("delete"):
                service.events().delete(calendarId=calendar_id, eventId=event_id).execute()
                return [
                    TextContent(
                        type="text",
                        text=json.dumps({"status": "ok", "deleted": event_id}, indent=2),
                    )
                ]

            event = service.events().get(calendarId=calendar_id, eventId=event_id).execute()
            if arguments.get("summary"):
                event["summary"] = arguments["summary"]
            if arguments.get("start"):
                event["start"] = {"dateTime": arguments["start"]}
            if arguments.get("end"):
                event["end"] = {"dateTime": arguments["end"]}
            if arguments.get("description"):
                event["description"] = arguments["description"]
            if arguments.get("location"):
                event["location"] = arguments["location"]

            updated = (
                service.events()
                .update(calendarId=calendar_id, eventId=event_id, body=event)
                .execute()
            )
            return [
                TextContent(
                    type="text",
                    text=json.dumps(
                        {
                            "status": "ok",
                            "event": {"id": updated["id"], "summary": updated["summary"]},
                        },
                        indent=2,
                    ),
                )
            ]

        else:
            return [
                TextContent(
                    type="text",
                    text=json.dumps(
                        {"status": "error", "message": f"Unknown tool: {name}"}, indent=2
                    ),
                )
            ]

    except Exception as e:
        log.exception("Tool call failed: %s", name)
        return [
            TextContent(
                type="text", text=json.dumps({"status": "error", "message": str(e)}, indent=2)
            )
        ]


async def main():
    log.info("Gmail+Calendar MCP starting (config=%s)", CONFIG_PATH)
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def main_sync():
    """Synchronous entry point for pip/pipx/uvx."""
    asyncio.run(main())


if __name__ == "__main__":
    main_sync()
