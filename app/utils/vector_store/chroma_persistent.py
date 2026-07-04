import os
import re
from collections import Counter
from math import sqrt
from typing import Callable, Optional

from config import Config
from utils.document_identity import source_hash
from utils.logging_config import CHROMA_LOGGER as log
from utils.providers import EmbeddingFactory
from .base import VectorStore


# Cert SSL config (caricato prima di qualsiasi import problematico)
cert_path = os.getenv("CA_CERT_PATH")
if cert_path and os.path.exists(cert_path):
    os.environ["SSL_CERT_FILE"] = cert_path
    os.environ["REQUESTS_CA_BUNDLE"] = cert_path
    os.environ["HDF5_USE_FILE_LOCKING"] = "FALSE"

try:
    import chromadb
except ImportError:
    chromadb = None


def _default_chroma_client():
    if chromadb is None:
        raise RuntimeError("chromadb non installato. Installa le dipendenze runtime con requirements.txt.")
    return chromadb.PersistentClient(path=Config.paths.chroma_persist_dir)


def _default_embedding_provider():
    return EmbeddingFactory.get_provider()


class ChromaPersistentVectorStore(VectorStore):
    backend = "chroma_persistent"
    collection_name = "documents"

    def __init__(
        self,
        client_factory: Optional[Callable] = None,
        embedding_provider_factory: Optional[Callable] = None,
        collection_name: Optional[str] = None,
    ):
        self._client_factory = client_factory or _default_chroma_client
        self._embedding_provider_factory = embedding_provider_factory or _default_embedding_provider
        self.collection_name = collection_name or self.collection_name

    def add_documents(self, documents):
        if not documents:
            log.warning("Nessun documento da aggiungere")
            return None

        log.info(f"Aggiungo {len(documents)} documenti a Chroma...")
        collection = self._collection()

        texts = [doc.page_content for doc in documents]
        metadatas = [doc.metadata for doc in documents]
        ids = [document_chunk_id(doc.metadata, i) for i, doc in enumerate(documents)]

        embeddings = self._embedding_provider().encode_documents(texts)

        collection.add(documents=texts, metadatas=metadatas, ids=ids, embeddings=embeddings)
        log.info(f"{len(documents)} documenti aggiunti. DB contiene {collection.count()} documenti totali")
        return collection

    def delete_by_source(self, source: str) -> int:
        collection = self._collection()
        existing = collection.get(where={"source": source})
        ids = existing.get("ids", []) if existing else []
        if not ids:
            log.info(f"Nessun chunk Chroma da cancellare per source={source}")
            return 0

        collection.delete(ids=ids)
        log.info(f"Cancellati {len(ids)} chunk Chroma per source={source}")
        return len(ids)

    def find_document_by_id(self, document_id: str, exclude_source: Optional[str] = None) -> Optional[dict]:
        collection = self._collection()
        existing = collection.get(where={"document_id": document_id}, include=["metadatas"])
        ids = existing.get("ids", []) if existing else []
        metadatas = existing.get("metadatas", []) if existing else []
        for chunk_id, metadata in zip(ids, metadatas):
            metadata = metadata or {}
            source = metadata.get("source")
            if exclude_source and source == exclude_source:
                continue
            return {
                "document_id": document_id,
                "source": source,
                "chunk_id": chunk_id,
                "chunks": len(ids),
            }
        return None

    def query(self, query: str, k: Optional[int] = None):
        k = k or Config.rag.query_k
        log.info(f"Query: '{query}' (top {k})")

        docs, _, _ = self._query_documents(query, k=k, include_embeddings=False)

        log.info(f"Trovati {len(docs)} risultati")
        return docs

    def query_with_rerank(
        self,
        query: str,
        k: int = 5,
        top_n: int = 20,
        reranker=None,
        score_threshold: float = 0.0,
        diversity_mode: str = "none",
        mmr_lambda: float = 0.7,
        mmr_candidate_pool: Optional[int] = None,
    ):
        from utils.reranker import DummyReranker

        if reranker is None:
            reranker = DummyReranker()

        log.info(f"Query con ReRanker: '{query}' (recupero {top_n}, finale {k})")

        diversity_mode = _diversity_mode(diversity_mode)
        if diversity_mode == "mmr":
            candidate_pool = _mmr_pool_size(top_n, mmr_candidate_pool)
            retrieved_docs, embeddings, _ = self._query_documents(query, k=candidate_pool, include_embeddings=True)
            docs = _mmr_documents(retrieved_docs, embeddings, limit=top_n, mmr_lambda=mmr_lambda)
            log.info(
                f"MMR: recuperati {len(retrieved_docs)} candidati, "
                f"selezionati {len(docs)} per reranking, lambda={_clamp_mmr_lambda(mmr_lambda)}"
            )
            if docs:
                log.info("MMR: candidati: %s", _document_labels(docs))
        elif diversity_mode == "source_diversity":
            retrieved_docs = self.query(query, k=_rerank_pool_size(top_n))
            docs = _diverse_documents(retrieved_docs, limit=top_n, max_per_source=_max_chunks_per_source(k))
            log.info(
                f"ReRanker: recuperati {len(retrieved_docs)} documenti dal vector DB, "
                f"{len(docs)} candidati dopo diversity"
            )
        else:
            docs = self.query(query, k=top_n)
            log.info(f"ReRanker: recuperati {len(docs)} documenti dal vector DB")
        if docs:
            log.info("ReRanker: candidati dal vector DB: %s", _document_labels(docs))

        reranked_docs = reranker.rerank(query, docs, k)
        if score_threshold > 0:
            before_filter = len(reranked_docs)
            reranked_docs = [
                doc for doc in reranked_docs
                if _reranker_score(doc) is None or _reranker_score(doc) >= score_threshold
            ]
            log.info(
                f"ReRanker: soglia {score_threshold} applicata "
                f"({before_filter} -> {len(reranked_docs)} documenti)"
            )
        log.info(f"ReRanker: {len(docs)} -> {len(reranked_docs)} documenti finali")

        return reranked_docs

    def _query_documents(self, query: str, k: int, include_embeddings: bool):
        collection = self._collection()
        query_emb = self._embedding_provider().encode_query(query)
        include = ["documents", "metadatas"]
        if include_embeddings:
            include.extend(["embeddings", "distances"])

        results = collection.query(query_embeddings=[query_emb], n_results=k, include=include)
        docs = []
        embeddings = []
        distances = []

        documents = _first_result_list(results.get("documents"))
        if len(documents):
            from langchain_core.documents import Document

            metadatas = _first_result_list(results.get("metadatas"))
            embeddings = _first_result_list(results.get("embeddings")) if include_embeddings else []
            distances = _first_result_list(results.get("distances")) if include_embeddings else []
            for index, doc in enumerate(documents):
                metadata = dict(metadatas[index] or {}) if index < len(metadatas) else {}
                if include_embeddings:
                    score = _cosine_similarity(query_emb, embeddings[index] if index < len(embeddings) else None)
                    if score is None and index < len(distances):
                        score = _distance_score(distances[index])
                    if score is not None:
                        metadata["chroma_score"] = round(score, 6)
                docs.append(Document(page_content=str(doc), metadata=metadata))

        return docs, embeddings, query_emb

    def reset_collection(self):
        client = self._client()
        try:
            client.delete_collection(name=self.collection_name)
            log.info("Collection Chroma 'documents' cancellata")
        except Exception as e:
            message = str(e).lower()
            if "does not exist" not in message and "not found" not in message:
                raise
            log.info("Collection Chroma 'documents' non presente; verra' creata")
        return client.get_or_create_collection(name=self.collection_name, embedding_function=None)

    def status(self) -> dict:
        collection = self._collection()
        return {
            "vector_store_backend": self.backend,
            "collection": self.collection_name,
            "documents_count": collection.count(),
        }

    def _client(self):
        return self._client_factory()

    def _collection(self):
        return self._client().get_or_create_collection(name=self.collection_name, embedding_function=None)

    def _embedding_provider(self):
        return self._embedding_provider_factory()


def document_chunk_id(metadata: dict, fallback_index: int) -> str:
    document_id = str(metadata.get("document_id") or metadata.get("source") or "document")
    source_id = str(metadata.get("source_id") or source_hash(str(metadata.get("source") or document_id)))
    chunk_id = metadata.get("chunk_id", fallback_index)
    safe_document_id = re.sub(r"[^A-Za-z0-9_.-]+", "-", document_id).strip("-") or "document"
    safe_source_id = re.sub(r"[^A-Za-z0-9_.-]+", "-", source_id).strip("-") or "source"
    return f"{safe_source_id}:{safe_document_id}:chunk:{chunk_id}"


def _reranker_score(doc) -> Optional[float]:
    try:
        score = doc.metadata.get("reranker_score")
        if score is None:
            return None
        return float(score)
    except (AttributeError, TypeError, ValueError):
        return None


def _rerank_pool_size(top_n: int) -> int:
    return min(max(top_n, top_n * 4), 200)


def _mmr_pool_size(top_n: int, configured: Optional[int]) -> int:
    if configured is None:
        return _rerank_pool_size(top_n)
    try:
        value = int(configured)
    except (TypeError, ValueError):
        value = _rerank_pool_size(top_n)
    return min(max(top_n, value), 200)


def _max_chunks_per_source(final_k: int) -> int:
    return max(1, min(3, final_k))


def _diversity_mode(value: str) -> str:
    selected = str(value or "none").strip().lower()
    return selected if selected in {"none", "source_diversity", "mmr"} else "none"


def _clamp_mmr_lambda(value) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = 0.7
    return max(0.0, min(1.0, parsed))


def _mmr_documents(docs, embeddings, limit: int, mmr_lambda: float):
    if embeddings is None or len(embeddings) < len(docs):
        return docs[:limit]
    if any(_cosine_similarity(embedding, embedding) is None for embedding in embeddings[:len(docs)]):
        return docs[:limit]

    mmr_lambda = _clamp_mmr_lambda(mmr_lambda)
    candidates = []
    for index, (doc, embedding) in enumerate(zip(docs, embeddings)):
        relevance = _metadata_float(doc, "chroma_score")
        if relevance is None:
            relevance = 1.0 - (index / max(len(docs), 1))
        candidates.append({
            "doc": doc,
            "embedding": embedding,
            "index": index,
            "relevance": relevance,
        })

    selected = []
    while candidates and len(selected) < limit:
        scored = []
        for position, candidate in enumerate(candidates):
            similarity = 0.0
            if selected:
                similarity = max(
                    _cosine_similarity(candidate["embedding"], item["embedding"]) or 0.0
                    for item in selected
                )
            score = (mmr_lambda * candidate["relevance"]) - ((1.0 - mmr_lambda) * similarity)
            scored.append((score, position, candidate))

        score, position, best = max(scored, key=lambda item: (item[0], -item[2]["index"]))
        best["doc"].metadata = {
            **(best["doc"].metadata or {}),
            "mmr_score": round(score, 6),
        }
        selected.append(best)
        del candidates[position]

    return [item["doc"] for item in selected]


def _diverse_documents(docs, limit: int, max_per_source: int):
    selected = []
    overflow = []
    counts = Counter()

    for doc in docs:
        source = _document_source(doc)
        if counts[source] < max_per_source:
            selected.append(doc)
            counts[source] += 1
            if len(selected) >= limit:
                return selected
        else:
            overflow.append(doc)

    for doc in overflow:
        if len(selected) >= limit:
            break
        selected.append(doc)
    return selected


def _document_source(doc) -> str:
    metadata = doc.metadata or {}
    return str(metadata.get("source") or metadata.get("document_id") or "document")


def _document_labels(docs) -> str:
    labels = []
    for index, doc in enumerate(docs, start=1):
        metadata = doc.metadata or {}
        source = _document_source(doc)
        name = os.path.basename(source) or source
        chunk = metadata.get("chunk_id")
        score = _metadata_float(doc, "mmr_score")
        if score is None:
            score = _metadata_float(doc, "chroma_score")
        label = f"{index}:{name}" + (f" chunk={chunk}" if chunk is not None else "")
        if score is not None:
            label = f"{label} score={score:.4f}"
        labels.append(label)
    return ", ".join(labels)


def _metadata_float(doc, key: str) -> Optional[float]:
    try:
        value = (doc.metadata or {}).get(key)
        if value is None:
            return None
        return float(value)
    except (AttributeError, TypeError, ValueError):
        return None


def _cosine_similarity(left, right) -> Optional[float]:
    if left is None or right is None:
        return None
    try:
        pairs = [(float(a), float(b)) for a, b in zip(left, right)]
    except (TypeError, ValueError):
        return None
    if not pairs:
        return None
    left_norm = sqrt(sum(a * a for a, _ in pairs))
    right_norm = sqrt(sum(b * b for _, b in pairs))
    if left_norm == 0 or right_norm == 0:
        return None
    return sum(a * b for a, b in pairs) / (left_norm * right_norm)


def _distance_score(value) -> Optional[float]:
    try:
        return -float(value)
    except (TypeError, ValueError):
        return None


def _first_result_list(value):
    if value is None:
        return []
    try:
        return value[0] if len(value) else []
    except (TypeError, IndexError):
        return []
