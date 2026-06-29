"""IMAP email ingestion plugin using only Python standard libraries."""

from __future__ import annotations

import email
import html
import imaplib
import os
import re
from datetime import datetime, timezone
from email.message import Message
from email.utils import getaddresses, parsedate_to_datetime

from utils.data_ingestion.base import IngestionAttachment, IngestionItem, SyncContext, SyncResult
from utils.document_indexer import DOCUMENT_INDEX_EXTENSIONS
from utils.validators import ValidationError


class EmailIngester:
    plugin_id = "email_imap"
    display_name = "Email IMAP"

    def validate_config(self, config: dict) -> dict:
        config = dict(config or {})
        host = str(config.get("host") or "").strip()
        username = str(config.get("username") or "").strip()
        password_env = str(config.get("password_env") or "").strip()
        password = str(config.get("password") or "").strip()
        if not host:
            raise ValidationError("host IMAP obbligatorio", "host")
        if not username:
            raise ValidationError("username IMAP obbligatorio", "username")
        if not password_env and not password:
            raise ValidationError("password o password_env obbligatorio", "password")

        return {
            "host": host,
            "port": _int(config.get("port"), 993),
            "use_ssl": _as_bool(config.get("use_ssl"), True),
            "username": username,
            "password_env": password_env,
            "password": password,
            "folder": str(config.get("folder") or "INBOX").strip() or "INBOX",
            "from_contains": str(config.get("from_contains") or "").strip(),
            "subject_contains": str(config.get("subject_contains") or "").strip(),
            "since": str(config.get("since") or "").strip(),
            "max_messages": max(1, min(_int(config.get("max_messages"), 25), 500)),
            "include_body": _as_bool(config.get("include_body"), True),
            "include_attachments": _as_bool(config.get("include_attachments"), True),
        }

    def sync(self, context: SyncContext, source_config: dict, cursor: dict | None = None) -> SyncResult:
        config = self.validate_config(source_config)
        password = config.get("password") or os.getenv(config["password_env"], "")
        if not password:
            raise ValidationError("Variabile ambiente password non configurata", "password_env")

        mailbox = imaplib.IMAP4_SSL(config["host"], config["port"]) if config["use_ssl"] else imaplib.IMAP4(config["host"], config["port"])
        try:
            mailbox.login(config["username"], password)
            status, _ = mailbox.select(config["folder"], readonly=True)
            if status != "OK":
                raise ValidationError("Cartella IMAP non selezionabile", "folder")

            last_uid = _int((cursor or {}).get("last_uid"), 0)
            status, data = mailbox.uid("search", None, f"UID {last_uid + 1}:*")
            if status != "OK":
                raise ValidationError("Ricerca IMAP fallita", "email")
            uids = (data[0] or b"").split()
            uids = uids[: config["max_messages"]]

            items = []
            errors = []
            newest_uid = last_uid
            for raw_uid in uids:
                uid = raw_uid.decode("ascii", errors="ignore")
                newest_uid = max(newest_uid, _int(uid, newest_uid))
                try:
                    status, message_data = mailbox.uid("fetch", raw_uid, "(RFC822)")
                    if status != "OK" or not message_data:
                        errors.append({"remote_id": uid, "error": "fetch failed"})
                        continue
                    raw_message = _message_bytes(message_data)
                    if not raw_message:
                        errors.append({"remote_id": uid, "error": "empty message"})
                        continue
                    item = parse_email_message(raw_message, uid, config)
                    if item:
                        items.append(item)
                except Exception as exc:
                    errors.append({"remote_id": uid, "error": str(exc)})

            return SyncResult(items=items, cursor={"last_uid": newest_uid}, errors=errors)
        finally:
            try:
                mailbox.logout()
            except Exception:
                pass


def parse_email_message(raw_message: bytes, uid: str, config: dict | None = None) -> IngestionItem | None:
    config = config or {}
    message = email.message_from_bytes(raw_message)
    subject = _decode_header(message.get("Subject", "")).strip() or "(no subject)"
    sender = _decode_header(message.get("From", "")).strip()
    recipients = _addresses(message.get_all("To", []) + message.get_all("Cc", []))
    message_id = _decode_header(message.get("Message-ID", "")).strip() or uid
    thread_id = _decode_header(message.get("In-Reply-To", "") or message.get("References", "") or message_id).strip()
    updated_at = _message_date(message)

    if not _matches_filters(sender, subject, updated_at, config):
        return None

    body = _extract_body(message)
    if not body and not config.get("include_attachments", True):
        return None

    metadata = {
        "source_type": "email",
        "ingestion_plugin": EmailIngester.plugin_id,
        "remote_id": uid,
        "remote_url": "",
        "remote_updated_at": updated_at,
        "subject": subject,
        "sender": sender,
        "recipients": ", ".join(recipients),
        "thread_id": thread_id,
        "message_id": message_id,
    }
    content = _email_markdown(subject, sender, recipients, updated_at, body) if config.get("include_body", True) else ""
    attachments = _extract_attachments(message, uid, metadata) if config.get("include_attachments", True) else []
    return IngestionItem(
        content=content,
        filename=f"{subject}.md",
        extension="md",
        remote_id=uid,
        updated_at=updated_at,
        metadata=metadata,
        attachments=attachments,
    )


def _extract_body(message: Message) -> str:
    plain_parts = []
    html_parts = []
    for part in message.walk() if message.is_multipart() else [message]:
        if part.get_content_disposition() == "attachment":
            continue
        content_type = part.get_content_type()
        payload = part.get_payload(decode=True)
        if not payload:
            continue
        charset = part.get_content_charset() or "utf-8"
        text = payload.decode(charset, errors="replace")
        if content_type == "text/plain":
            plain_parts.append(text)
        elif content_type == "text/html":
            html_parts.append(_html_to_text(text))
    body = "\n\n".join(plain_parts).strip()
    return body or "\n\n".join(html_parts).strip()


def _extract_attachments(message: Message, uid: str, parent_metadata: dict) -> list[IngestionAttachment]:
    attachments = []
    for index, part in enumerate(message.walk() if message.is_multipart() else []):
        filename = _decode_header(part.get_filename() or "").strip()
        if not filename:
            continue
        extension = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        if extension not in DOCUMENT_INDEX_EXTENSIONS:
            continue
        payload = part.get_payload(decode=True)
        if not payload:
            continue
        attachment_id = f"{uid}:attachment:{index}"
        metadata = {
            **parent_metadata,
            "source_type": "email_attachment",
            "remote_id": attachment_id,
            "attachment_id": attachment_id,
            "attachment_filename": filename,
        }
        attachments.append(
            IngestionAttachment(
                filename=filename,
                content=payload,
                extension=extension,
                remote_id=attachment_id,
                updated_at=parent_metadata.get("remote_updated_at", ""),
                metadata=metadata,
            )
        )
    return attachments


def _email_markdown(subject: str, sender: str, recipients: list[str], updated_at: str, body: str) -> str:
    header = [
        f"# {subject}",
        "",
        f"From: {sender}",
        f"To: {', '.join(recipients)}",
        f"Date: {updated_at}",
        "",
    ]
    return "\n".join(header) + (body or "")


def _matches_filters(sender: str, subject: str, updated_at: str, config: dict) -> bool:
    from_filter = str(config.get("from_contains") or "").lower()
    if from_filter and from_filter not in sender.lower():
        return False
    subject_filter = str(config.get("subject_contains") or "").lower()
    if subject_filter and subject_filter not in subject.lower():
        return False
    since = str(config.get("since") or "").strip()
    if since and updated_at:
        try:
            message_date = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
            since_date = datetime.fromisoformat(since.replace("Z", "+00:00"))
            if since_date.tzinfo is None:
                since_date = since_date.replace(tzinfo=timezone.utc)
            if message_date < since_date:
                return False
        except (TypeError, ValueError):
            return True
    return True


def _decode_header(value: str) -> str:
    decoded = email.header.decode_header(value or "")
    parts = []
    for part, charset in decoded:
        if isinstance(part, bytes):
            parts.append(part.decode(charset or "utf-8", errors="replace"))
        else:
            parts.append(str(part))
    return "".join(parts)


def _message_date(message: Message) -> str:
    try:
        parsed = parsedate_to_datetime(message.get("Date", ""))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).isoformat(timespec="seconds")
    except Exception:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _addresses(values: list[str]) -> list[str]:
    return [address for _, address in getaddresses(values) if address]


def _html_to_text(value: str) -> str:
    value = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", value)
    value = re.sub(r"(?i)<br\s*/?>", "\n", value)
    value = re.sub(r"(?i)</p>", "\n\n", value)
    value = re.sub(r"<[^>]+>", " ", value)
    value = html.unescape(value)
    return re.sub(r"[ \t]+", " ", value).strip()


def _message_bytes(message_data) -> bytes:
    for item in message_data:
        if isinstance(item, tuple) and len(item) >= 2 and isinstance(item[1], bytes):
            return item[1]
    return b""


def _int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_bool(value, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}
