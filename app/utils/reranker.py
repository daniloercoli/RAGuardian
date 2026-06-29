"""
ReRanker basato su Cross-Encoder per migliorare la qualità del retrieval RAG.
Può usare BGE-Reranker-v2-M3 locale o modelli remote (API OpenAI-compatible).
"""

import json
import logging
import re
from typing import List, Optional

from langchain_core.documents import Document

#logger = logging.getLogger("rag_service.reranker")
from .logging_config import RERANKER_LOGGER as logger

class BaseReranker:
    """Astrazione per reranker."""

    def rerank(self, query: str, docs: List[Document], top_n: int) -> List[Document]:
        raise NotImplementedError


# ------------------------------------------------------------------
# BGE Reranker locale
# ------------------------------------------------------------------

class BGEReranker(BaseReranker):
    """BGE-Reranker-v2-M3 locale."""

    def __init__(self, model_name: str = "BAAI/bge-reranker-v2-m3", device: str = "cuda"):
        try:
            from sentence_transformers import CrossEncoder
        except ImportError:
            raise ImportError(
                "sentence-transformers è richiesto per il ReRanker locale. "
                "Installa con: pip install sentence-transformers"
            )

        import torch

        if device == "cuda":
            if torch.cuda.is_available():
                actual_device = "cuda"
                logger.info("ReRanker LOCALE: CUDA disponibile (%d GPU)", torch.cuda.device_count())
            else:
                actual_device = "cpu"
                logger.warning("ReRanker LOCALE: CUDA non disponibile, uso CPU")
        elif device == "mps":
            if torch.backends.mps.is_available():
                actual_device = "mps"
                logger.info("ReRanker LOCALE: MPS (Apple Silicon) disponibile")
            else:
                actual_device = "cpu"
                logger.warning("ReRanker LOCALE: MPS non disponibile, uso CPU")
        else:
            actual_device = device

        logger.info("ReRanker LOCALE: carico '%s' su %s", model_name, actual_device)
        self.model = CrossEncoder(model_name, device=actual_device)
        logger.info("ReRanker LOCALE: caricato con successo")

    def rerank(self, query: str, docs: List[Document], top_n: int) -> List[Document]:
        if not docs or top_n <= 0:
            return []

        logger.info("ReRanker LOCALE: %d documenti → top %d", len(docs), top_n)
        pairs = [(query, doc.page_content) for doc in docs]
        scores = self.model.predict(pairs)
        scored_docs = [
            (_with_reranker_score(doc, _score_to_float(score)), _score_to_float(score))
            for doc, score in zip(docs, scores)
        ]
        scored_docs.sort(key=lambda item: item[1], reverse=True)
        result = [doc for doc, _score in scored_docs[:top_n]]
        logger.info("ReRanker LOCALE: restituiti %d documenti", len(result))
        return result


# ------------------------------------------------------------------
# Remote Reranker
# ------------------------------------------------------------------

def _is_reranker_model(model: str) -> bool:
    """Indica se il nome del modello appartiene a un modello specializzato di tipo reranker."""
    return "reranker" in model.lower() or "rerank" in model.lower()


class RemoteReranker(BaseReranker):
    """
    Reranker remoto tramite API OpenAI-compatible.

    Supporta due modalità:
      - /v1/rerank batch endpoint per modelli specializzati (Qwen3-Reranker, ecc.)
      - /v1/chat/completions per modelli chat generici
    """

    def __init__(
        self,
        base_url: str,
        api_key: Optional[str],
        model: str,
        timeout: int = 60,
        mode: str = "auto",
    ):
        try:
            from openai import OpenAI
        except ImportError:
            raise ImportError("openai è richiesto per il ReRanker remoto. "
                              "Installa con: pip install openai")

        try:
            import requests  # noqa: F401
        except ImportError:
            raise ImportError("requests è richiesto per il ReRanker remoto. "
                              "Installa con: pip install requests")

        self.base_url = base_url
        self.api_key = api_key or "openai-compatible-reranker"
        self.model = model
        self.timeout = timeout
        self.mode = _normalize_remote_mode(mode)
        self.use_rerank_endpoint = (
            self.mode == "rerank"
            or (self.mode == "auto" and _is_reranker_model(model))
        )
        self.client = OpenAI(api_key=self.api_key, base_url=base_url)

        mode = "endpoint /rerank" if self.use_rerank_endpoint else "endpoint /chat/completions"
        logger.info("ReRanker REMOTO: modello '%s', %s", model, mode)

    def _rerank_batch(self, query: str, docs: List[Document]) -> List[float]:
        """Chiamata batch a /v1/rerank (endpoint specializzato)."""
        import requests

        url = self.base_url.rstrip("/") + "/rerank"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.model,
            "query": query,
            "documents": [doc.page_content for doc in docs],
        }

        logger.info("ReRanker REMOTO: POST %s (%d documenti)", url, len(docs))
        resp = requests.post(url, json=payload, headers=headers, timeout=self.timeout)
        resp.raise_for_status()
        data = resp.json()
        results = {item["index"]: item.get("relevance_score", 0.0) for item in data.get("results", [])}
        return [results.get(i, 0.0) for i, _ in enumerate(docs)]

    def _rerank_chat(self, query: str, docs: List[Document]) -> List[float]:
        """Scoring tramite /v1/chat/completions (batch di 5)."""
        logger.info("ReRanker REMOTO: chat scoring (%d documenti)", len(docs))
        scores = [0.0] * len(docs)
        batch_size = 5

        for i in range(0, len(docs), batch_size):
            batch = docs[i:i + batch_size]
            for offset, doc in enumerate(batch):
                try:
                    response = self.client.chat.completions.create(
                        model=self.model,
                        messages=[{
                            "role": "user",
                            "content": f"Valuta la rilevanza di questo documento rispetto alla query. Rispondi SOLO con un numero da 0 a 10.\n\nQuery: {query}\nDocumento: {doc.page_content}"
                        }],
                        max_tokens=3,
                        temperature=0.0,
                    )
                    content = response.choices[0].message.content or ""
                    scores[i + offset] = _parse_remote_score(content)
                except Exception:
                    scores[i + offset] = 0.0

        return scores

    def rerank(self, query: str, docs: List[Document], top_n: int) -> List[Document]:
        if not docs or top_n <= 0:
            return []
        if len(docs) <= top_n:
            return docs

        log_prefix = "ReRanker REMOTO"
        try:
            if self.use_rerank_endpoint:
                logger.info("%s: batch /rerank (%d docs)", log_prefix, len(docs))
                raw_scores = self._rerank_batch(query, docs)
            else:
                logger.info("%s: batch /chat/completions (%d docs)", log_prefix, len(docs))
                raw_scores = self._rerank_chat(query, docs)

            scored_docs = [
                (_with_reranker_score(doc, score), score)
                for doc, score in zip(docs, raw_scores)
            ]
            scored_docs.sort(key=lambda x: x[1], reverse=True)
            result = [doc for doc, _ in scored_docs[:top_n]]
            logger.info("%s: restituiti %d documenti", log_prefix, len(result))
            return result

        except Exception as e:
            logger.error("ReRanker REMOTO: errore %s", e)
            return docs[:top_n]


# ------------------------------------------------------------------
# Dummy Reranker
# ------------------------------------------------------------------

class DummyReranker(BaseReranker):
    """Reranker dummy quando disabilitato (pass-through)."""

    def rerank(self, query: str, docs: List[Document], top_n: int) -> List[Document]:
        return docs[:top_n]


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _score_to_float(score) -> float:
    try:
        if hasattr(score, "item"):
            return float(score.item())
        return float(score)
    except (TypeError, ValueError):
        return 0.0


def _parse_remote_score(content: str) -> float:
    match = re.search(r"-?\d+(?:\.\d+)?", str(content or ""))
    if not match:
        raise ValueError("Nessun punteggio numerico nella risposta")
    return _score_to_float(match.group(0))


def _with_reranker_score(doc: Document, score: float) -> Document:
    metadata = dict(doc.metadata or {})
    metadata["reranker_score"] = score
    return Document(page_content=doc.page_content, metadata=metadata)


# ------------------------------------------------------------------
# Factory
# ------------------------------------------------------------------

def get_reranker(
    enabled: bool = True,
    model_name: str = "BAAI/bge-reranker-v2-m3",
    base_url: Optional[str] = None,
    api_key: Optional[str] = None,
    mode: str = "auto",
) -> BaseReranker:
    """Factory per ottenere l'istanza del reranker corretta."""
    if not enabled:
        logger.info("ReRanker DISABILITATO: uso DummyReranker")
        return DummyReranker()

    if base_url and api_key:
        try:
            return RemoteReranker(base_url=base_url, api_key=api_key, model=model_name, mode=mode)
        except Exception as e:
            logger.warning("ReRanker REMOTO: impossibile caricare (%s), uso DummyReranker", e)
            return DummyReranker()

    try:
        return BGEReranker(model_name=model_name)
    except Exception as e:
        logger.warning("ReRanker LOCALE: impossibile caricare (%s), uso DummyReranker", e)
        return DummyReranker()


def _normalize_remote_mode(value: str) -> str:
    selected = str(value or "auto").strip().lower().replace("-", "_")
    if selected in {"chat", "chat_completion", "chat_completions"}:
        return "chat_completions"
    if selected in {"rerank", "reranker", "rerank_endpoint"}:
        return "rerank"
    return "auto"
