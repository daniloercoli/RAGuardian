"""Microsoft OneDrive/SharePoint ingestion plugin via Microsoft Graph."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from utils.data_ingestion.base import IngestionItem, SyncContext, SyncResult
from utils.document_indexer import DOCUMENT_INDEX_EXTENSIONS
from utils.validators import ValidationError


class MicrosoftDriveIngester:
    plugin_id = "microsoft_drive"
    display_name = "Microsoft Drive"

    def validate_config(self, config: dict) -> dict:
        config = dict(config or {})
        token_env = str(config.get("token_env") or "").strip()
        token = str(config.get("token") or "").strip()
        if not token_env and not token:
            raise ValidationError("token o token_env Microsoft Graph obbligatorio", "token")

        extensions = _extensions(config.get("include_extensions") or "pdf,txt,md")
        if not extensions:
            raise ValidationError("Nessuna estensione supportata configurata", "include_extensions")

        return {
            "token_env": token_env,
            "token": token,
            "base_url": str(config.get("base_url") or "https://graph.microsoft.com/v1.0").strip().rstrip("/"),
            "drive_id": str(config.get("drive_id") or "").strip(),
            "folder_path": _clean_folder_path(config.get("folder_path")),
            "item_id": str(config.get("item_id") or "").strip(),
            "recursive": _as_bool(config.get("recursive"), True),
            "max_files": max(1, min(_int(config.get("max_files"), 50), 1000)),
            "max_file_size_mb": max(1, min(_int(config.get("max_file_size_mb"), 10), 100)),
            "include_extensions": sorted(extensions),
        }

    def sync(self, context: SyncContext, source_config: dict, cursor: dict | None = None) -> SyncResult:
        config = self.validate_config(source_config)
        token = config.get("token") or os.getenv(config["token_env"], "")
        if not token:
            raise ValidationError("Variabile ambiente token Microsoft Graph non configurata", "token_env")

        client = _GraphClient(config["base_url"], token)
        seen_cursor = dict((cursor or {}).get("items", {}))
        next_cursor = {}
        items = []
        errors = []
        listed = 0
        max_file_size = config["max_file_size_mb"] * 1024 * 1024

        for drive_item in client.walk_children(
            drive_id=config["drive_id"],
            folder_path=config["folder_path"],
            item_id=config["item_id"],
            recursive=config["recursive"],
        ):
            if len(items) >= config["max_files"]:
                break
            listed += 1
            if "file" not in drive_item:
                continue

            name = str(drive_item.get("name") or "")
            extension = _extension(name)
            if extension not in config["include_extensions"]:
                continue
            size = _int(drive_item.get("size"), 0)
            item_id = str(drive_item.get("id") or "")
            etag = str(drive_item.get("eTag") or drive_item.get("cTag") or "")
            last_modified = str(drive_item.get("lastModifiedDateTime") or "")
            cursor_key = item_id or name
            cursor_value = etag or last_modified
            next_cursor[cursor_key] = cursor_value
            if seen_cursor.get(cursor_key) == cursor_value:
                continue
            if size > max_file_size:
                errors.append({"remote_id": cursor_key, "error": "file exceeds max_file_size_mb"})
                continue

            try:
                content = client.download_item(config["drive_id"], item_id)
                items.append(_item_from_drive_item(config, drive_item, content, extension))
            except Exception as exc:
                errors.append({"remote_id": cursor_key, "error": str(exc)})

        return SyncResult(
            items=items,
            cursor={
                "items": next_cursor,
                "last_sync": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "listed": listed,
            },
            errors=errors,
        )


class _GraphClient:
    def __init__(self, base_url: str, token: str):
        self.base_url = base_url
        self.token = token

    def walk_children(
        self,
        *,
        drive_id: str,
        folder_path: str,
        item_id: str,
        recursive: bool,
    ):
        queue = [self._children_path(drive_id, folder_path, item_id)]
        while queue:
            path = queue.pop(0)
            for item in self._paged_json(path):
                yield item
                if recursive and item.get("folder") and item.get("id"):
                    queue.append(self._children_path(drive_id, "", str(item["id"])))

    def download_item(self, drive_id: str, item_id: str) -> bytes:
        if not item_id:
            raise ValidationError("Drive item id mancante", "item_id")
        if drive_id:
            path = f"/drives/{quote(drive_id)}/items/{quote(item_id)}/content"
        else:
            path = f"/me/drive/items/{quote(item_id)}/content"
        return self._request(path)

    def _children_path(self, drive_id: str, folder_path: str, item_id: str) -> str:
        query = urlencode(
            {
                "$select": "id,name,eTag,cTag,size,webUrl,lastModifiedDateTime,parentReference,file,folder",
                "$top": "200",
            }
        )
        if item_id:
            base = f"/drives/{quote(drive_id)}/items/{quote(item_id)}/children" if drive_id else f"/me/drive/items/{quote(item_id)}/children"
        elif folder_path:
            safe_path = quote(folder_path.strip("/"))
            base = f"/drives/{quote(drive_id)}/root:/{safe_path}:/children" if drive_id else f"/me/drive/root:/{safe_path}:/children"
        else:
            base = f"/drives/{quote(drive_id)}/root/children" if drive_id else "/me/drive/root/children"
        return f"{base}?{query}"

    def _paged_json(self, path_or_url: str):
        next_url = self._url(path_or_url)
        while next_url:
            payload = json.loads(self._request(next_url).decode("utf-8"))
            for item in payload.get("value", []):
                yield item
            next_url = payload.get("@odata.nextLink")

    def _request(self, path_or_url: str) -> bytes:
        request = Request(
            self._url(path_or_url),
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/json",
            },
        )
        try:
            with urlopen(request, timeout=30) as response:
                return response.read()
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise ValidationError(f"Microsoft Graph HTTP {exc.code}: {detail[:300]}", "microsoft_graph") from exc
        except URLError as exc:
            raise ValidationError(f"Microsoft Graph non raggiungibile: {exc}", "microsoft_graph") from exc

    def _url(self, path_or_url: str) -> str:
        if path_or_url.startswith("https://"):
            return path_or_url
        return f"{self.base_url}{path_or_url}"


def _item_from_drive_item(config: dict, drive_item: dict, content: bytes, extension: str) -> IngestionItem:
    item_id = str(drive_item.get("id") or "")
    name = str(drive_item.get("name") or item_id or "drive-item")
    parent = drive_item.get("parentReference") or {}
    mime_type = (drive_item.get("file") or {}).get("mimeType", "")
    metadata = {
        "source_type": "microsoft_drive",
        "ingestion_plugin": MicrosoftDriveIngester.plugin_id,
        "remote_id": item_id,
        "remote_url": drive_item.get("webUrl", ""),
        "remote_updated_at": drive_item.get("lastModifiedDateTime", ""),
        "drive_id": config.get("drive_id") or parent.get("driveId", ""),
        "item_id": item_id,
        "parent_id": parent.get("id", ""),
        "etag": drive_item.get("eTag") or drive_item.get("cTag") or "",
        "file_name": name,
        "mime_type": mime_type,
        "size": drive_item.get("size", 0),
    }
    return IngestionItem(
        content=_decode_if_text(content, extension),
        content_bytes=content,
        filename=name,
        extension=extension,
        remote_id=item_id,
        updated_at=str(drive_item.get("lastModifiedDateTime") or ""),
        metadata=metadata,
    )


def _decode_if_text(content: bytes, extension: str) -> str:
    if extension not in {"txt", "md"}:
        return ""
    try:
        return content.decode("utf-8-sig")
    except UnicodeDecodeError:
        return content.decode("latin-1", errors="replace")


def _extensions(value) -> set[str]:
    if isinstance(value, list):
        parts = value
    else:
        parts = str(value or "").replace(";", ",").split(",")
    return {
        str(part).strip().lower().lstrip(".")
        for part in parts
        if str(part).strip().lower().lstrip(".") in DOCUMENT_INDEX_EXTENSIONS
    }


def _extension(filename: str) -> str:
    return filename.rsplit(".", 1)[-1].lower() if "." in filename else ""


def _clean_folder_path(value) -> str:
    return str(value or "").strip().strip("/")


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
