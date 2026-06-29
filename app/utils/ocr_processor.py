import re

from langchain_core.documents import Document
from langchain_text_splitters import RecursiveCharacterTextSplitter

from config import Config
from utils.document_identity import document_hash, source_hash
from utils.logging_config import PDF_LOGGER as log
from utils.settings_store import get_settings


def process_ocr_text(
    file_path: str,
    text: str,
    settings_path: str | None = None,
    ocr_text_path: str | None = None,
) -> list[Document]:
    log.info("Processing OCR text: %s", file_path)
    cleaned = re.sub(r"\s+", " ", text or "")
    cleaned = re.sub(r"[^\w\s.,;:!?()\-'']", " ", cleaned).strip()
    if not cleaned:
        log.warning("Empty OCR text for file")
        return []

    rag_settings = get_settings(settings_path or Config.paths.settings_file)["rag"]
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=rag_settings["chunk_size"],
        chunk_overlap=rag_settings["chunk_overlap"],
        length_function=len,
        separators=["\n\n", "\n", ". ", " ", ""],
    )
    chunks = splitter.split_text(cleaned)
    doc_id = document_hash(file_path)
    src_id = source_hash(file_path)

    return [
        Document(
            page_content=chunk,
            metadata={
                "source": file_path,
                "source_type": "ocr",
                "ocr_text_path": ocr_text_path or "",
                "document_id": doc_id,
                "source_id": src_id,
                "chunk_id": index,
                "chunk_length": len(chunk),
                "ocr_used": True,
            },
        )
        for index, chunk in enumerate(chunks)
    ]
