"""Ephemeral retrieval over chat attachments.

Attachments processed here are embedded in memory for the current request only.
They are never written to Chroma or the persistent file index.
"""

from __future__ import annotations

import csv
import io
import math
import os
from typing import Iterable

from langchain_core.documents import Document
from langchain_text_splitters import RecursiveCharacterTextSplitter

from config import Config
from utils.logging_config import APP_LOGGER as log
from utils.providers.embedding_factory import EmbeddingFactory
from utils.settings_store import get_settings
from utils.text_processor import clean_text, extract_text_file


TEXT_EXTENSIONS = {"txt", "md", "csv", "tsv", "json"}
SPREADSHEET_EXTENSIONS = {"xlsx", "xls", "parquet"}


def retrieve_attachment_context(
    query: str,
    attachments: list[dict],
    *,
    settings_path: str | None = None,
    top_k: int | None = None,
) -> list[Document]:
    """Return the most relevant temporary chunks from chat attachments."""
    documents = build_attachment_documents(attachments, settings_path=settings_path)
    if not documents:
        return []

    top_k = top_k or int(os.getenv("CHAT_ATTACHMENT_RAG_K", "4"))
    top_k = max(1, min(top_k, len(documents)))

    provider = EmbeddingFactory.get_provider(_embedding_model(settings_path))
    query_embedding = provider.encode_query(query)
    doc_embeddings = provider.encode_documents([doc.page_content for doc in documents])

    scored = []
    for doc, embedding in zip(documents, doc_embeddings):
        score = _cosine_similarity(query_embedding, embedding)
        doc.metadata = {**(doc.metadata or {}), "attachment_score": round(score, 6)}
        scored.append((score, doc))
    scored.sort(key=lambda item: item[0], reverse=True)
    return [doc for _score, doc in scored[:top_k]]


def build_attachment_documents(
    attachments: list[dict],
    *,
    settings_path: str | None = None,
) -> list[Document]:
    settings = get_settings(settings_path or Config.paths.settings_file)
    rag = settings["rag"]
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=rag["chunk_size"],
        chunk_overlap=rag["chunk_overlap"],
        length_function=len,
        separators=["\n\n", "\n", ". ", " ", ""],
    )

    documents: list[Document] = []
    for attachment in attachments:
        text = _attachment_text(attachment)
        if not text:
            continue
        chunks = splitter.split_text(text)
        for index, chunk in enumerate(chunks):
            documents.append(
                Document(
                    page_content=chunk,
                    metadata={
                        "source": attachment.get("name") or "attachment",
                        "source_type": "temporary_attachment",
                        "attachment_id": attachment.get("id", ""),
                        "attachment_filename": attachment.get("name", ""),
                        "chunk_id": index,
                        "chunk_length": len(chunk),
                        "temporary_attachment": True,
                    },
                )
            )
    return documents


def _attachment_text(attachment: dict) -> str:
    path = str(attachment.get("path") or "")
    name = str(attachment.get("name") or path)
    extension = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    try:
        if extension == "pdf":
            from utils.pdf_processor import extract_pdf_text

            return extract_pdf_text(path)
        if extension in TEXT_EXTENSIONS:
            if extension == "tsv":
                return _tabular_text(path, delimiter="\t")
            if extension == "csv":
                return _tabular_text(path, delimiter=",")
            return extract_text_file(path)
        if extension in SPREADSHEET_EXTENSIONS:
            return _spreadsheet_text(path, extension)
    except Exception as exc:
        log.warning("Temporary attachment RAG skipped %s: %s", name, exc)
    return ""


def _tabular_text(path: str, delimiter: str) -> str:
    with open(path, "r", encoding="utf-8-sig", errors="replace", newline="") as handle:
        sample = handle.read(200_000)
    rows = list(csv.reader(io.StringIO(sample), delimiter=delimiter))
    output = io.StringIO()
    for row in rows[:200]:
        output.write(" | ".join(cell.strip() for cell in row))
        output.write("\n")
    return clean_text(output.getvalue())


def _spreadsheet_text(path: str, extension: str) -> str:
    try:
        import pandas as pd
    except ImportError as exc:
        raise RuntimeError("pandas non disponibile per leggere spreadsheet temporanei") from exc

    if extension == "parquet":
        frame = pd.read_parquet(path)
        return clean_text(frame.head(200).to_csv(index=False))

    sheets = pd.read_excel(path, sheet_name=None, nrows=200)
    output = io.StringIO()
    for sheet_name, frame in sheets.items():
        output.write(f"Sheet: {sheet_name}\n")
        output.write(frame.to_csv(index=False))
        output.write("\n")
    return clean_text(output.getvalue())


def _embedding_model(settings_path: str | None = None) -> str:
    settings = get_settings(settings_path or Config.paths.settings_file)
    return settings["rag"]["embedding_model"]


def _cosine_similarity(left: Iterable[float], right: Iterable[float]) -> float:
    left_values = [float(value) for value in left]
    right_values = [float(value) for value in right]
    if not left_values or not right_values or len(left_values) != len(right_values):
        return 0.0
    dot = sum(a * b for a, b in zip(left_values, right_values))
    left_norm = math.sqrt(sum(a * a for a in left_values))
    right_norm = math.sqrt(sum(b * b for b in right_values))
    if left_norm == 0.0 or right_norm == 0.0:
        return 0.0
    return dot / (left_norm * right_norm)
