"""Contracts shared by in-repo data ingestion plugins."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class SyncContext:
    upload_folder: str
    settings_file: str
    file_index: str


@dataclass
class IngestionAttachment:
    filename: str
    content: bytes
    extension: str
    remote_id: str
    updated_at: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class IngestionItem:
    content: str
    filename: str
    extension: str
    remote_id: str
    content_bytes: bytes | None = None
    updated_at: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    attachments: list[IngestionAttachment] = field(default_factory=list)


@dataclass
class SyncResult:
    items: list[IngestionItem] = field(default_factory=list)
    cursor: dict[str, Any] = field(default_factory=dict)
    errors: list[dict[str, str]] = field(default_factory=list)


class BaseIngester(Protocol):
    plugin_id: str
    display_name: str

    def validate_config(self, config: dict) -> dict:
        ...

    def sync(self, context: SyncContext, source_config: dict, cursor: dict | None = None) -> SyncResult:
        ...
