#!/usr/bin/env python3
"""
Google Drive MCP Server
=======================
Local MCP server for Google Drive — list, read, upload, download, search, share.
Run via stdio — register in your MCP client config.

Tools:
  drive_list          — List files/folders
  drive_search        — Search files by name, type, content
  drive_read          — Read file content (exports Google Docs/Sheets to text/CSV)
  drive_download      — Download file to local path
  drive_upload        — Upload local file to Drive
  drive_create        — Create new Google Doc/Sheet/Folder
  drive_move          — Move file to folder
  drive_share         — Share file with email (read/write/comment)
  drive_permissions   — List/manage file permissions
  drive_trash         — Move file to trash

Auth: OAuth2 via Google Cloud Console credentials.json
Token stored at: ~/.config/claude-2-mail/drive_token.json
"""

import asyncio
import io
import json
import logging
import mimetypes
import os
import re
import fcntl
from pathlib import Path
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool


class _RedactFormatter(logging.Formatter):
    """Redact sensitive data (emails, tokens, passwords) from log output."""
    _email_re = re.compile(r'[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}')
    _token_re = re.compile(r'(token|password|secret|key)["\s:=]+["\']?([^\s"\'&]+)', re.IGNORECASE)

    def format(self, record: logging.LogRecord) -> str:
        msg = super().format(record)
        msg = self._email_re.sub('[REDACTED_EMAIL]', msg)
        msg = self._token_re.sub(r'\1=[REDACTED]', msg)
        return msg


log = logging.getLogger("drive-mcp")
_handler = logging.StreamHandler()
_handler.setFormatter(_RedactFormatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
log.addHandler(_handler)
log.setLevel(logging.INFO)

# ---------------------------------------------------------------------------
# Config paths
# ---------------------------------------------------------------------------

CONFIG_DIR = Path(os.environ.get("DRIVE_CONFIG_DIR", "~/.config/claude-2-mail")).expanduser()
TOKEN_PATH = CONFIG_DIR / "drive_token.json"
CREDS_PATH = CONFIG_DIR / "credentials.json"
LOCK_PATH = CONFIG_DIR / ".drive_auth.lock"

# Narrow OAuth scopes — only what we need
SCOPES = [
    "https://www.googleapis.com/auth/drive.file",
    "https://www.googleapis.com/auth/documents.readonly",
]

# ---------------------------------------------------------------------------
# Centralized auth with file locking (prevents race conditions)
# ---------------------------------------------------------------------------

try:
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build as gbuild
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.http import MediaIoBaseDownload, MediaFileUpload
    GOOGLE_LIBS = True
except ImportError:
    GOOGLE_LIBS = False


def _get_creds():
    """Get credentials with file-based locking to prevent race conditions."""
    if not GOOGLE_LIBS:
        raise RuntimeError(
            "Google API libraries not installed.\n"
            "Run: pip install google-api-python-client google-auth-httplib2 google-auth-oauthlib"
        )

    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(LOCK_PATH, "w") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            creds = None
            if TOKEN_PATH.exists():
                creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)

            if not creds or not creds.valid:
                if creds and creds.expired and creds.refresh_token:
                    creds.refresh()
                else:
                    if not CREDS_PATH.exists():
                        raise FileNotFoundError(
                            f"Google OAuth credentials not found: {CREDS_PATH}\n"
                            "Download from Google Cloud Console → APIs & Services → Credentials → OAuth 2.0 → Desktop app"
                        )
                    flow = InstalledAppFlow.from_client_secrets_file(str(CREDS_PATH), SCOPES)
                    creds = flow.run_local_server(port=0)
                with open(TOKEN_PATH, "w") as f:
                    f.write(creds.to_json())
            return creds
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)


def get_drive_service():
    return gbuild("drive", "v3", credentials=_get_creds())


def get_docs_service():
    return gbuild("docs", "v1", credentials=_get_creds())


def get_sheets_service():
    return gbuild("sheets", "v4", credentials=_get_creds())


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

def _validate_email(email: str) -> bool:
    """Basic email format validation."""
    return bool(re.match(r'^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$', email))


def _validate_dest_path(dest: str) -> Path:
    """Validate destination path — must be under home or /tmp."""
    p = Path(dest).expanduser().resolve()
    home = Path.home().resolve()
    tmp = Path("/tmp").resolve()
    if not (str(p).startswith(str(home)) or str(p).startswith(str(tmp))):
        raise ValueError(f"Destination must be under home or /tmp: {dest}")
    return p


# ---------------------------------------------------------------------------
# MIME type maps
# ---------------------------------------------------------------------------

MIME_TYPE_MAP = {
    "doc": "application/vnd.google-apps.document",
    "document": "application/vnd.google-apps.document",
    "sheet": "application/vnd.google-apps.spreadsheet",
    "spreadsheet": "application/vnd.google-apps.spreadsheet",
    "folder": "application/vnd.google-apps.folder",
    "pdf": "application/pdf",
    "txt": "text/plain",
    "md": "text/markdown",
    "csv": "text/csv",
    "json": "application/json",
}

GOOGLE_MIME_EXPORT = {
    "application/vnd.google-apps.document": ("text/plain", ".txt"),
    "application/vnd.google-apps.spreadsheet": ("text/csv", ".csv"),
    "application/vnd.google-apps.presentation": ("text/plain", ".txt"),
}


# ---------------------------------------------------------------------------
# MCP Server
# ---------------------------------------------------------------------------

server = Server("google-drive")


@server.list_tools()
async def list_tools():
    return [
        Tool(
            name="drive_list",
            description="List files and folders in Google Drive. Can filter by folder, type, or query.",
            inputSchema={
                "type": "object",
                "properties": {
                    "folder_id": {"type": "string", "description": "Parent folder ID (default: root)", "default": "root"},
                    "query": {"type": "string", "description": "Search query (e.g. 'name contains \"report\"')"},
                    "file_type": {"type": "string", "description": "Filter by type: folder, document, sheet, pdf, image, etc."},
                    "page_size": {"type": "integer", "description": "Max results (default: 50, max: 100)", "default": 50},
                    "order_by": {"type": "string", "description": "Sort order: 'name', 'modifiedTime desc', 'createdTime desc'", "default": "name"},
                },
            },
        ),
        Tool(
            name="drive_search",
            description="Search files across Google Drive by name, content, or metadata.",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search text (matches name and content)"},
                    "file_type": {"type": "string", "description": "Filter by type: document, sheet, pdf, folder, image, video"},
                    "folder_id": {"type": "string", "description": "Search within this folder only"},
                    "page_size": {"type": "integer", "description": "Max results (default: 20)", "default": 20},
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="drive_read",
            description="Read file content. Google Docs/Sheets are exported to text/CSV. Returns file content as string.",
            inputSchema={
                "type": "object",
                "properties": {
                    "file_id": {"type": "string", "description": "Google Drive file ID"},
                    "file_name": {"type": "string", "description": "File name (alternative to file_id, searches by name)"},
                },
            },
        ),
        Tool(
            name="drive_download",
            description="Download a file from Google Drive to a local path under home or /tmp.",
            inputSchema={
                "type": "object",
                "properties": {
                    "file_id": {"type": "string", "description": "Google Drive file ID"},
                    "destination": {"type": "string", "description": "Local file path (default: ~/Downloads/FILENAME)"},
                },
                "required": ["file_id"],
            },
        ),
        Tool(
            name="drive_upload",
            description="Upload a local file to Google Drive. Optionally convert to Google Doc/Sheet format.",
            inputSchema={
                "type": "object",
                "properties": {
                    "file_path": {"type": "string", "description": "Local file path to upload"},
                    "file_name": {"type": "string", "description": "Name in Drive (default: same as local file)"},
                    "folder_id": {"type": "string", "description": "Destination folder ID (default: root)", "default": "root"},
                    "convert_to_google": {"type": "boolean", "description": "Convert to Google Docs format (for .txt, .md, .docx)", "default": False},
                },
                "required": ["file_path"],
            },
        ),
        Tool(
            name="drive_create",
            description="Create a new Google Doc, Sheet, or Folder in Drive.",
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Name of the new file/folder"},
                    "type": {"type": "string", "description": "Type: document, sheet, folder (default: folder)", "default": "folder"},
                    "folder_id": {"type": "string", "description": "Parent folder ID (default: root)", "default": "root"},
                    "content": {"type": "string", "description": "Initial content (for documents only)"},
                },
                "required": ["name"],
            },
        ),
        Tool(
            name="drive_move",
            description="Move a file to a different folder in Drive. Default: add to new folder, keep existing parents.",
            inputSchema={
                "type": "object",
                "properties": {
                    "file_id": {"type": "string", "description": "File ID to move"},
                    "folder_id": {"type": "string", "description": "Destination folder ID"},
                    "remove_from_all": {"type": "boolean", "description": "DANGEROUS: Remove from all other parents (default: false)", "default": False},
                },
                "required": ["file_id", "folder_id"],
            },
        ),
        Tool(
            name="drive_share",
            description="Share a file or folder with an email address. Set permission level.",
            inputSchema={
                "type": "object",
                "properties": {
                    "file_id": {"type": "string", "description": "File ID to share"},
                    "email": {"type": "string", "description": "Email address to share with"},
                    "role": {"type": "string", "description": "Permission: reader, commenter, writer (default: reader)", "default": "reader"},
                    "notify": {"type": "boolean", "description": "Send notification email (default: true)", "default": True},
                    "message": {"type": "string", "description": "Custom message in notification email"},
                },
                "required": ["file_id", "email"],
            },
        ),
        Tool(
            name="drive_permissions",
            description="List or remove permissions on a file.",
            inputSchema={
                "type": "object",
                "properties": {
                    "file_id": {"type": "string", "description": "File ID"},
                    "revoke_email": {"type": "string", "description": "Revoke access for this email (optional)"},
                },
                "required": ["file_id"],
            },
        ),
        Tool(
            name="drive_trash",
            description="Move a file to trash (does not permanently delete).",
            inputSchema={
                "type": "object",
                "properties": {
                    "file_id": {"type": "string", "description": "File ID to trash"},
                },
                "required": ["file_id"],
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    try:
        # ---- DRIVE LIST ----
        if name == "drive_list":
            folder_id = arguments.get("folder_id", "root")
            query = arguments.get("query")
            file_type = arguments.get("file_type")
            page_size = min(arguments.get("page_size", 50), 100)
            order_by = arguments.get("order_by", "name")

            service = get_drive_service()
            q_parts = [f"'{folder_id}' in parents", "trashed = false"]
            if query:
                q_parts.append(f"name contains '{query}'")
            if file_type:
                mime = MIME_TYPE_MAP.get(file_type.lower())
                if mime:
                    q_parts.append(f"mimeType = '{mime}'")
                elif file_type.lower() == "folder":
                    q_parts.append("mimeType = 'application/vnd.google-apps.folder'")

            q = " and ".join(q_parts)
            results = service.files().list(
                q=q, pageSize=page_size, orderBy=order_by,
                fields="files(id, name, mimeType, size, modifiedTime, createdTime, parents, webViewLink)"
            ).execute()
            files = results.get("files", [])
            return [TextContent(type="text", text=json.dumps({"status": "ok", "files": files, "count": len(files)}, indent=2))]

        # ---- DRIVE SEARCH ----
        elif name == "drive_search":
            query = arguments["query"]
            file_type = arguments.get("file_type")
            folder_id = arguments.get("folder_id")
            page_size = arguments.get("page_size", 20)

            service = get_drive_service()
            q_parts = [f"fullText contains '{query}' or name contains '{query}'", "trashed = false"]
            if file_type:
                mime = MIME_TYPE_MAP.get(file_type.lower())
                if mime:
                    q_parts.append(f"mimeType = '{mime}'")
            if folder_id:
                q_parts.append(f"'{folder_id}' in parents")

            q = " and ".join(q_parts)
            results = service.files().list(
                q=q, pageSize=page_size,
                fields="files(id, name, mimeType, size, modifiedTime, webViewLink)"
            ).execute()
            files = results.get("files", [])
            return [TextContent(type="text", text=json.dumps({"status": "ok", "files": files, "count": len(files)}, indent=2))]

        # ---- DRIVE READ ----
        elif name == "drive_read":
            file_id = arguments.get("file_id")
            file_name = arguments.get("file_name")

            service = get_drive_service()

            if not file_id and file_name:
                results = service.files().list(
                    q=f"name = '{file_name}' and trashed = false",
                    pageSize=1, fields="files(id, name, mimeType)"
                ).execute()
                items = results.get("files", [])
                if not items:
                    return [TextContent(type="text", text=json.dumps({"status": "error", "message": f"File not found: {file_name}"}, indent=2))]
                file_id = items[0]["id"]
                mime = items[0]["mimeType"]
            else:
                meta = service.files().get(fileId=file_id, fields="name, mimeType").execute()
                mime = meta["mimeType"]

            if mime == "application/vnd.google-apps.document":
                docs = get_docs_service()
                doc = docs.documents().get(documentId=file_id).execute()
                content = []
                for elem in doc.get("body", {}).get("content", []):
                    if "paragraph" in elem:
                        for run in elem["paragraph"].get("elements", []):
                            if "textRun" in run:
                                content.append(run["textRun"].get("content", ""))
                text = "".join(content)
                return [TextContent(type="text", text=json.dumps({"status": "ok", "content": text, "file_id": file_id}, indent=2))]

            elif mime == "application/vnd.google-apps.spreadsheet":
                sheets = get_sheets_service()
                spreadsheet = sheets.spreadsheets().get(spreadsheetId=file_id).execute()
                all_rows = []
                for sheet in spreadsheet.get("sheets", []):
                    title = sheet["properties"]["title"]
                    result = sheets.spreadsheets().values().get(
                        spreadsheetId=file_id, range=title
                    ).execute()
                    values = result.get("values", [])
                    all_rows.append(f"## {title}")
                    for row in values:
                        all_rows.append(", ".join(str(c) for c in row))
                text = "\n".join(all_rows)
                return [TextContent(type="text", text=json.dumps({"status": "ok", "content": text, "file_id": file_id}, indent=2))]

            else:
                request = service.files().get_media(fileId=file_id)
                fh = io.BytesIO()
                downloader = MediaIoBaseDownload(fh, request)
                done = False
                while not done:
                    _, done = downloader.next_chunk()
                content = fh.getvalue()
                try:
                    text = content.decode("utf-8")
                except UnicodeDecodeError:
                    text = f"[Binary file, {len(content)} bytes]"
                return [TextContent(type="text", text=json.dumps({"status": "ok", "content": text, "file_id": file_id, "size": len(content)}, indent=2))]

        # ---- DRIVE DOWNLOAD ----
        elif name == "drive_download":
            file_id = arguments["file_id"]
            destination = arguments.get("destination")

            service = get_drive_service()
            meta = service.files().get(fileId=file_id, fields="name, mimeType").execute()

            if not destination:
                destination = str(Path.home() / "Downloads" / meta["name"])
            dest_path = _validate_dest_path(destination)

            dest_path.parent.mkdir(parents=True, exist_ok=True)

            export_info = GOOGLE_MIME_EXPORT.get(meta["mimeType"])
            if export_info:
                export_mime, ext = export_info
                if not str(dest_path).endswith(ext):
                    dest_path = dest_path.with_suffix(ext)
                request = service.files().export_media(fileId=file_id, mimeType=export_mime)
            else:
                request = service.files().get_media(fileId=file_id)

            with open(dest_path, "wb") as f:
                downloader = MediaIoBaseDownload(f, request)
                done = False
                while not done:
                    _, done = downloader.next_chunk()

            return [TextContent(type="text", text=json.dumps({"status": "ok", "downloaded_to": str(dest_path), "file_name": meta["name"]}, indent=2))]

        # ---- DRIVE UPLOAD ----
        elif name == "drive_upload":
            file_path = Path(arguments["file_path"]).expanduser()
            if not file_path.exists():
                return [TextContent(type="text", text=json.dumps({"status": "error", "message": f"File not found: {file_path}"}, indent=2))]

            file_name = arguments.get("file_name", file_path.name)
            folder_id = arguments.get("folder_id", "root")
            convert = arguments.get("convert_to_google", False)

            service = get_drive_service()
            mime_type, _ = mimetypes.guess_type(str(file_path))
            mime_type = mime_type or "application/octet-stream"

            file_metadata = {"name": file_name, "parents": [folder_id]}

            if convert and mime_type in ("text/plain", "text/markdown", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"):
                file_metadata["mimeType"] = "application/vnd.google-apps.document"

            media = MediaFileUpload(str(file_path), mimetype=mime_type, resumable=True)
            uploaded = service.files().create(
                body=file_metadata, media_body=media,
                fields="id, name, webViewLink"
            ).execute()

            return [TextContent(type="text", text=json.dumps({"status": "ok", "file": uploaded}, indent=2))]

        # ---- DRIVE CREATE ----
        elif name == "drive_create":
            name_val = arguments["name"]
            type_val = arguments.get("type", "folder")
            folder_id = arguments.get("folder_id", "root")
            content = arguments.get("content", "")

            service = get_drive_service()
            mime = MIME_TYPE_MAP.get(type_val.lower(), "application/vnd.google-apps.folder")

            file_metadata = {"name": name_val, "mimeType": mime, "parents": [folder_id]}
            created = service.files().create(body=file_metadata, fields="id, name, webViewLink").execute()

            if mime == "application/vnd.google-apps.document" and content:
                docs = get_docs_service()
                docs.documents().batchUpdate(
                    documentId=created["id"],
                    body={"requests": [{"insertText": {"location": {"index": 1}, "text": content}}]}
                ).execute()

            return [TextContent(type="text", text=json.dumps({"status": "ok", "file": created}, indent=2))]

        # ---- DRIVE MOVE ----
        elif name == "drive_move":
            file_id = arguments["file_id"]
            folder_id = arguments["folder_id"]
            remove_all = arguments.get("remove_from_all", False)

            service = get_drive_service()
            meta = service.files().get(fileId=file_id, fields="parents").execute()
            prev_parents = ",".join(meta.get("parents", [])) if not remove_all else ""

            service.files().update(
                fileId=file_id, addParents=folder_id, removeParents=prev_parents, fields="id, parents"
            ).execute()

            return [TextContent(type="text", text=json.dumps({"status": "ok", "moved": file_id, "to_folder": folder_id}, indent=2))]

        # ---- DRIVE SHARE ----
        elif name == "drive_share":
            file_id = arguments["file_id"]
            email = arguments["email"]
            role = arguments.get("role", "reader")
            notify = arguments.get("notify", True)
            message = arguments.get("message", "")

            if not _validate_email(email):
                return [TextContent(type="text", text=json.dumps({"status": "error", "message": f"Invalid email: {email}"}, indent=2))]

            service = get_drive_service()
            permission = {"type": "user", "role": role, "emailAddress": email}
            result = service.permissions().create(
                fileId=file_id, body=permission,
                sendNotificationEmail=notify, emailMessage=message or None,
                fields="id, role, emailAddress"
            ).execute()

            return [TextContent(type="text", text=json.dumps({"status": "ok", "permission": result}, indent=2))]

        # ---- DRIVE PERMISSIONS ----
        elif name == "drive_permissions":
            file_id = arguments["file_id"]
            revoke_email = arguments.get("revoke_email")

            service = get_drive_service()

            if revoke_email:
                if not _validate_email(revoke_email):
                    return [TextContent(type="text", text=json.dumps({"status": "error", "message": f"Invalid email: {revoke_email}"}, indent=2))]
                perms = service.permissions().list(fileId=file_id, fields="permissions(id, emailAddress, role)").execute()
                revoked = []
                for p in perms.get("permissions", []):
                    if p.get("emailAddress", "").lower() == revoke_email.lower():
                        service.permissions().delete(fileId=file_id, permissionId=p["id"]).execute()
                        revoked.append(p["id"])
                return [TextContent(type="text", text=json.dumps({"status": "ok", "revoked": revoked}, indent=2))]

            perms = service.permissions().list(fileId=file_id, fields="permissions(id, emailAddress, role, type)").execute()
            return [TextContent(type="text", text=json.dumps({"status": "ok", "permissions": perms.get("permissions", [])}, indent=2))]

        # ---- DRIVE TRASH ----
        elif name == "drive_trash":
            file_id = arguments["file_id"]
            service = get_drive_service()
            service.files().update(fileId=file_id, body={"trashed": True}).execute()
            return [TextContent(type="text", text=json.dumps({"status": "ok", "trashed": file_id}, indent=2))]

        else:
            return [TextContent(type="text", text=json.dumps({"status": "error", "message": f"Unknown tool: {name}"}, indent=2))]

    except Exception as e:
        log.exception("Tool call failed: %s", name)
        return [TextContent(type="text", text=json.dumps({"status": "error", "message": str(e)}, indent=2))]


async def main():
    log.info("Drive MCP starting")
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def main_sync():
    """Synchronous entry point for pip/pipx/uvx."""
    asyncio.run(main())

if __name__ == "__main__":
    main_sync()
