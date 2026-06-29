import platform
import re
from importlib.metadata import PackageNotFoundError, version
from typing import List, Optional

from .embedding_base import BaseEmbeddingProvider
from ..logging_config import EMBEDDING_LOGGER as log


MIN_TORCH_VERSION = (2, 4)


class SentenceTransformerProvider(BaseEmbeddingProvider):
    """Local sentence-transformers provider"""
    
    MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"
    
    def __init__(self, model_name: Optional[str] = None):
        _ensure_modern_torch_available()
        self.model_name = model_name or self.MODEL_NAME
        log.info(f"Caricamento modello sentence-transformers: {self.model_name}")
        from sentence_transformers import SentenceTransformer
        self._model = SentenceTransformer(self.model_name, device="cpu")
        self._dimensions = int(self._model.get_sentence_embedding_dimension() or 384)
        log.info(f"Modello caricato! ({self.dimensions()} dimensioni)")
    
    @property
    def provider_name(self) -> str:
        return "SentenceTransformer (local)"
    
    def encode_documents(self, texts: List[str]) -> List[List[float]]:
        log.info(f"Embedding locale (docs): {len(texts)} documenti")
        embeddings = self._model.encode(
            texts,
            normalize_embeddings=True,
            show_progress_bar=False
        )
        return embeddings.tolist()
    
    def encode_query(self, query: str) -> List[float]:
        log.info(f"Embedding locale (query): '{query[:60]}...'")
        embedding = self._model.encode(
            query,
            normalize_embeddings=True,
            show_progress_bar=False
        )
        return embedding.tolist()
    
    def dimensions(self) -> int:
        return self._dimensions


def _ensure_modern_torch_available() -> None:
    try:
        torch_version = version("torch")
    except PackageNotFoundError as exc:
        raise ValueError(_local_embeddings_error("torch non installato")) from exc

    parsed = _major_minor(torch_version)
    if parsed < MIN_TORCH_VERSION:
        raise ValueError(_local_embeddings_error(f"torch {torch_version} installato"))


def _major_minor(raw_version: str) -> tuple[int, int]:
    match = re.match(r"^(\d+)\.(\d+)", raw_version)
    if not match:
        return (0, 0)
    return int(match.group(1)), int(match.group(2))


def _local_embeddings_error(reason: str) -> str:
    host = f"{platform.system()} {platform.machine()}"
    macos_intel_note = ""
    if platform.system() == "Darwin" and platform.machine() == "x86_64":
        macos_intel_note = (
            " Questa piattaforma e' macOS x86_64: pip espone solo torch 2.2.x, "
            "quindi gli embeddings locali moderni non sono installabili qui."
        )
    return (
        f"Embeddings locali non disponibili ({reason}; host={host}). "
        "Il provider local richiede torch>=2.4, sentence-transformers 5.x e transformers 5.x."
        f"{macos_intel_note} Usa Provider embeddings=regolo in /admin/config, "
        "sapendo che con Regolo il testo dei documenti e delle query viene inviato a Regolo "
        "per generare gli embeddings. In alternativa esegui il servizio su Linux/Windows/macOS arm64 "
        "per embeddings locali moderni."
    )
