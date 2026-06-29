import re

from langchain_core.documents import Document
from langchain_text_splitters import RecursiveCharacterTextSplitter

from config import Config
from utils.document_identity import document_hash, source_hash
from utils.logging_config import PDF_LOGGER as log
from utils.settings_store import get_settings


TEXT_EXTENSIONS = {"txt", "md", "csv"}


def process_text_file(file_path: str, settings_path: str | None = None) -> list[Document]:
    log.info("Carico documento testuale: %s", file_path)
    document_id = document_hash(file_path)
    source_id = source_hash(file_path)
    full_text = extract_text_file(file_path)
    log.info("Testo pulito: %s caratteri", len(full_text))

    if not full_text:
        log.warning("Nessun testo indicizzabile nel documento")
        return []

    rag_settings = get_settings(settings_path or Config.paths.settings_file)["rag"]
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=rag_settings["chunk_size"],
        chunk_overlap=rag_settings["chunk_overlap"],
        length_function=len,
        separators=["\n\n", "\n", ". ", " ", ""],
    )
    texts = splitter.split_text(full_text)
    source_type = source_type_for_text_extension(file_path)

    documents = [
        Document(
            page_content=chunk,
            metadata={
                "source": file_path,
                "source_type": source_type,
                "document_id": document_id,
                "source_id": source_id,
                "chunk_id": i,
                "chunk_length": len(chunk),
            },
        )
        for i, chunk in enumerate(texts)
    ]
    if not documents:
        log.warning("Nessun chunk generato dal documento testuale")
        return []

    avg_len = sum(len(d.page_content) for d in documents) // len(documents)
    log.info("%s chunk generati (media %s char)", len(documents), avg_len)
    return documents


def extract_text_file(file_path: str) -> str:
    with open(file_path, "rb") as source:
        raw = source.read()

    if _looks_binary(raw):
        raise ValueError("Il file sembra binario, non testo")

    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("latin-1")
    return clean_text(text)


def clean_text(text: str) -> str:
    text = str(text or "").replace("\x00", " ")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[\t\f\v ]+", " ", text)
    text = re.sub(r"\n[ \t]+", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def source_type_for_text_extension(file_path: str) -> str:
    extension = file_path.rsplit(".", 1)[-1].lower() if "." in file_path else ""
    if extension == "md":
        return "markdown"
    if extension == "csv":
        return "csv"
    return "text"


def _looks_binary(raw: bytes) -> bool:
    if not raw:
        return False
    sample = raw[:4096]
    if b"\x00" in sample:
        return True
    control_count = sum(1 for byte in sample if byte < 9 or 13 < byte < 32)
    return control_count / max(1, len(sample)) > 0.05
