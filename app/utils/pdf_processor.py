import re
from langchain_community.document_loaders import PyPDFLoader
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_core.documents import Document
from config import Config
from utils.logging_config import PDF_LOGGER as log
from utils.decorators import log_execution
from utils.document_identity import document_hash, source_hash
from utils.settings_store import get_settings

def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in {"pdf"}


def extract_pdf_text(file_path):
    pages = PyPDFLoader(file_path).load()
    log.info(f"Pagine estratte: {len(pages)}")
    full_text = "\n\n".join(page.page_content for page in pages)
    return clean_extracted_text(full_text)


def clean_extracted_text(text):
    cleaned = re.sub(r"\s+", " ", text or "")
    return re.sub(r"[^\w\s.,;:!?()\-'']", " ", cleaned).strip()


@log_execution(logger=None)
def process_pdf(file_path, settings_path=None):
    log.info(f"Carico PDF: {file_path}")
    document_id = document_hash(file_path)
    source_id = source_hash(file_path)
    full_text = extract_pdf_text(file_path)
    log.info(f"Testo pulito: {len(full_text)} caratteri")
    
    rag_settings = get_settings(settings_path or Config.paths.settings_file)["rag"]
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=rag_settings["chunk_size"],
        chunk_overlap=rag_settings["chunk_overlap"],
        length_function=len,
        separators=["\n\n", "\n", ". ", " ", ""]
    )
    texts = splitter.split_text(full_text)
    
    documents = [
        Document(
            page_content=chunk,
            metadata={
                "source": file_path,
                "document_id": document_id,
                "source_id": source_id,
                "chunk_id": i,
                "chunk_length": len(chunk),
            },
        )
        for i, chunk in enumerate(texts)
    ]
    if not documents:
        log.warning("Nessun chunk generato dal PDF")
        return []

    avg_len = sum(len(d.page_content) for d in documents) // len(documents)
    log.info(f"{len(documents)} chunk generati (media {avg_len} char)")
    return documents
