"""Allowlisted ingestion plugin registry."""

from __future__ import annotations

from utils.data_ingestion.base import BaseIngester
from utils.data_ingestion.drive_ingester import MicrosoftDriveIngester
from utils.data_ingestion.email_ingester import EmailIngester
from utils.data_ingestion.folder_ingester import FolderIngester
from utils.validators import ValidationError


_INGESTERS: dict[str, BaseIngester] = {
    EmailIngester.plugin_id: EmailIngester(),
    MicrosoftDriveIngester.plugin_id: MicrosoftDriveIngester(),
    FolderIngester.plugin_id: FolderIngester(),
}


def available_plugins() -> list[dict]:
    return [
        {"id": plugin_id, "display_name": ingester.display_name}
        for plugin_id, ingester in sorted(_INGESTERS.items())
    ]


def get_ingester(plugin_id: str) -> BaseIngester:
    plugin_id = str(plugin_id or "").strip()
    ingester = _INGESTERS.get(plugin_id)
    if not ingester:
        raise ValidationError("Plugin ingestion non disponibile", "plugin", code="not_found")
    return ingester
