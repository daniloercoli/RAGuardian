import os
import time
import hashlib
import math
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, Generator, List, Optional
from urllib.parse import unquote, urlsplit

from config import Config
from utils import RAG_LOGGER as log
from utils.cache import RAGCache
from utils.chroma_manager import (
    query_chroma,
    query_chroma_by_embedding,
    query_chroma_with_rerank,
)
from utils.index_lock import (
    assert_distributed_locks_healthy,
    register_lifecycle_invalidator,
)
from utils.conversation_memory import (
    fallback_summary,
    format_turns,
    get_conversation_store,
)
from utils.reranker import get_reranker
from utils.file_index import FileIndex
from utils.providers.provider_factory import ProviderFactory
from utils.providers.embedding_factory import EmbeddingFactory
from utils.providers.registry import ProviderRegistry
from utils.provider_config import resolve_api_key
from utils.retry import ErrorUtils
from utils.settings_store import get_settings
from .model_defaults import load_builtin_reranker_providers


_cache = RAGCache()


def _invalidate_lifecycle_runtime_caches() -> None:
    clear_cache()
    ProviderFactory.reset_cache()


register_lifecycle_invalidator(
    "rag-provider-runtime",
    _invalidate_lifecycle_runtime_caches,
)


def query_rag(
    query: str,
    model: Optional[str] = None,
    provider: Optional[str] = None,
    stream: bool = False,
    temperature: Optional[float] = None,
    k: Optional[int] = None,
    settings_path: Optional[str] = None,
    file_index_path: Optional[str] = None,
    collection_name: Optional[str] = None,
    conversation_id: Optional[str] = None,
    client_context: Optional[dict] = None,
    response_language: Optional[str] = None,
    public: bool = False,
    custom_system_prompt: Optional[str] = None,
    extra_context_docs: Optional[list] = None,
    knowledge_base_targets: Optional[list[dict]] = None,
    conversation_knowledge_base_ids: Optional[list[str]] = None,
    conversation_prompt_context: Optional[str] = None,
    conversation_retrieval_context: Optional[str] = None,
    append_conversation_turn: bool = True,
):
    if stream:
        return query_rag_stream(
            query,
            model=model,
            provider=provider,
            temperature=temperature,
            k=k,
            settings_path=settings_path,
            file_index_path=file_index_path,
            collection_name=collection_name,
            conversation_id=conversation_id,
            client_context=client_context,
            response_language=response_language,
            public=public,
            custom_system_prompt=custom_system_prompt,
            extra_context_docs=extra_context_docs,
            knowledge_base_targets=knowledge_base_targets,
            conversation_knowledge_base_ids=conversation_knowledge_base_ids,
            conversation_prompt_context=conversation_prompt_context,
            conversation_retrieval_context=conversation_retrieval_context,
            append_conversation_turn=append_conversation_turn,
        )
    return query_rag_non_stream(
        query,
        model=model,
        provider=provider,
        temperature=temperature,
        k=k,
        settings_path=settings_path,
        file_index_path=file_index_path,
        collection_name=collection_name,
        conversation_id=conversation_id,
        client_context=client_context,
        response_language=response_language,
        public=public,
        custom_system_prompt=custom_system_prompt,
        extra_context_docs=extra_context_docs,
        knowledge_base_targets=knowledge_base_targets,
        conversation_knowledge_base_ids=conversation_knowledge_base_ids,
        conversation_prompt_context=conversation_prompt_context,
        conversation_retrieval_context=conversation_retrieval_context,
        append_conversation_turn=append_conversation_turn,
    )


def query_rag_non_stream(
    query: str,
    model: Optional[str] = None,
    provider: Optional[str] = None,
    temperature: Optional[float] = None,
    k: Optional[int] = None,
    settings_path: Optional[str] = None,
    file_index_path: Optional[str] = None,
    collection_name: Optional[str] = None,
    conversation_id: Optional[str] = None,
    client_context: Optional[dict] = None,
    response_language: Optional[str] = None,
    public: bool = False,
    custom_system_prompt: Optional[str] = None,
    extra_context_docs: Optional[list] = None,
    knowledge_base_targets: Optional[list[dict]] = None,
    conversation_knowledge_base_ids: Optional[list[str]] = None,
    conversation_prompt_context: Optional[str] = None,
    conversation_retrieval_context: Optional[str] = None,
    append_conversation_turn: bool = True,
) -> Dict[str, object]:
    settings = _load_settings(settings_path)
    rag = settings["rag"]
    effective_response_language = _normalize_response_language(response_language)
    provider_id, selected_model, provider_config = ProviderFactory.resolve(model=model, provider=provider, settings=settings)
    effective_k = k or rag["query_k"]
    effective_temperature = temperature if temperature is not None else rag["temperature"]

    log.info(
        f"RAG Query: '{query}' "
        f"(provider={provider_id}, model={selected_model}, temperature={effective_temperature}, k={effective_k})"
    )

    conversation_context = _conversation_context(
        conversation_id,
        frozen_context=conversation_prompt_context,
    )
    retrieval_query = _retrieval_query(
        query,
        conversation_id,
        frozen_context=conversation_retrieval_context,
    )
    context_docs = _get_context(
        retrieval_query,
        effective_k,
        selected_model,
        settings,
        collection_name=collection_name,
        conversation_id=conversation_id,
        knowledge_base_targets=knowledge_base_targets,
    )
    assert_distributed_locks_healthy()
    context_docs = _merge_context_docs(context_docs, extra_context_docs)
    answer = "".join(
        generate_response(
            query,
            context_docs,
            model=selected_model,
            provider=provider_id,
            temperature=effective_temperature,
            settings=settings,
            conversation_context=conversation_context,
            client_context=client_context,
            response_language=effective_response_language,
            custom_system_prompt=custom_system_prompt,
        )
    )
    if append_conversation_turn:
        _append_conversation_turn(
            conversation_id,
            query=query,
            answer=answer,
            provider=provider_id,
            model=selected_model,
            temperature=effective_temperature,
            settings=settings,
            knowledge_base_ids=conversation_knowledge_base_ids,
        )
    result = {
        "answer": answer,
        "model": selected_model,
        "provider": provider_id,
        "provider_name": provider_config.get("name", provider_id),
        "response_language": effective_response_language,
        "context": _serialize_context(
            context_docs,
            file_index_path=file_index_path,
            knowledge_base_targets=knowledge_base_targets,
            include_downloads=not public,
        ),
        "sources": _serialize_sources(context_docs),
        "usage": None,
    }
    if conversation_id:
        result["conversation_id"] = conversation_id
    return result


def prepare_rag_context(
    query: str,
    model: Optional[str] = None,
    provider: Optional[str] = None,
    temperature: Optional[float] = None,
    k: Optional[int] = None,
    settings_path: Optional[str] = None,
    collection_name: Optional[str] = None,
    conversation_id: Optional[str] = None,
    response_language: Optional[str] = None,
    use_cache: bool = True,
    extra_context_docs: Optional[list] = None,
    knowledge_base_targets: Optional[list[dict]] = None,
    conversation_prompt_context: Optional[str] = None,
    conversation_retrieval_context: Optional[str] = None,
) -> Dict[str, object]:
    """Resolve model settings and retrieve context without generating an answer."""
    settings = _load_settings(settings_path)
    rag = settings["rag"]
    effective_response_language = _normalize_response_language(response_language)
    provider_id, selected_model, provider_config = ProviderFactory.resolve(
        model=model,
        provider=provider,
        settings=settings,
    )
    effective_k = k or rag["query_k"]
    effective_temperature = temperature if temperature is not None else rag["temperature"]
    conversation_context = _conversation_context(
        conversation_id,
        frozen_context=conversation_prompt_context,
    )
    retrieval_query = _retrieval_query(
        query,
        conversation_id,
        frozen_context=conversation_retrieval_context,
    )
    context_docs = _get_context(
        retrieval_query,
        effective_k,
        selected_model,
        settings,
        use_cache=use_cache,
        collection_name=collection_name,
        conversation_id=conversation_id,
        knowledge_base_targets=knowledge_base_targets,
    )
    assert_distributed_locks_healthy()
    context_docs = _merge_context_docs(context_docs, extra_context_docs)
    return {
        "settings": settings,
        "provider": provider_id,
        "model": selected_model,
        "provider_config": provider_config,
        "temperature": effective_temperature,
        "k": effective_k,
        "response_language": effective_response_language,
        "conversation_context": conversation_context,
        "context_docs": context_docs,
    }


def query_rag_stream(
    query: str,
    model: Optional[str] = None,
    provider: Optional[str] = None,
    temperature: Optional[float] = None,
    k: Optional[int] = None,
    settings_path: Optional[str] = None,
    file_index_path: Optional[str] = None,
    collection_name: Optional[str] = None,
    conversation_id: Optional[str] = None,
    client_context: Optional[dict] = None,
    response_language: Optional[str] = None,
    public: bool = False,
    custom_system_prompt: Optional[str] = None,
    extra_context_docs: Optional[list] = None,
    knowledge_base_targets: Optional[list[dict]] = None,
    conversation_knowledge_base_ids: Optional[list[str]] = None,
    conversation_prompt_context: Optional[str] = None,
    conversation_retrieval_context: Optional[str] = None,
    append_conversation_turn: bool = True,
) -> Generator[str, None, None]:
    settings = _load_settings(settings_path)
    rag = settings["rag"]
    effective_response_language = _normalize_response_language(response_language)
    provider_id, selected_model, _provider_config = ProviderFactory.resolve(model=model, provider=provider, settings=settings)
    effective_k = k or rag["query_k"]
    effective_temperature = temperature if temperature is not None else rag["temperature"]

    conversation_context = _conversation_context(
        conversation_id,
        frozen_context=conversation_prompt_context,
    )
    retrieval_query = _retrieval_query(
        query,
        conversation_id,
        frozen_context=conversation_retrieval_context,
    )
    context_docs = _get_context(
        retrieval_query,
        effective_k,
        selected_model,
        settings,
        use_cache=False,
        collection_name=collection_name,
        conversation_id=conversation_id,
        knowledge_base_targets=knowledge_base_targets,
    )
    assert_distributed_locks_healthy()
    context_docs = _merge_context_docs(context_docs, extra_context_docs)
    answer_parts = []
    for chunk in generate_response(
        query,
        context_docs,
        model=selected_model,
        provider=provider_id,
        stream=True,
        temperature=effective_temperature,
        settings=settings,
        conversation_context=conversation_context,
        client_context=client_context,
        response_language=effective_response_language,
        custom_system_prompt=custom_system_prompt,
    ):
        answer_parts.append(chunk)
        yield chunk

    if append_conversation_turn:
        _append_conversation_turn(
            conversation_id,
            query=query,
            answer="".join(answer_parts),
            provider=provider_id,
            model=selected_model,
            temperature=effective_temperature,
            settings=settings,
            knowledge_base_ids=conversation_knowledge_base_ids,
        )


def query_rag_stream_events(
    query: str,
    model: Optional[str] = None,
    provider: Optional[str] = None,
    temperature: Optional[float] = None,
    k: Optional[int] = None,
    settings_path: Optional[str] = None,
    file_index_path: Optional[str] = None,
    collection_name: Optional[str] = None,
    conversation_id: Optional[str] = None,
    client_context: Optional[dict] = None,
    response_language: Optional[str] = None,
    public: bool = False,
    custom_system_prompt: Optional[str] = None,
    extra_context_docs: Optional[list] = None,
    knowledge_base_targets: Optional[list[dict]] = None,
    conversation_knowledge_base_ids: Optional[list[str]] = None,
    conversation_prompt_context: Optional[str] = None,
    conversation_retrieval_context: Optional[str] = None,
    append_conversation_turn: bool = True,
) -> Generator[Dict[str, object], None, None]:
    try:
        settings = _load_settings(settings_path)
        rag = settings["rag"]
        effective_response_language = _normalize_response_language(response_language)
        provider_id, selected_model, provider_config = ProviderFactory.resolve(
            model=model,
            provider=provider,
            settings=settings,
        )
        effective_k = k or rag["query_k"]
        effective_temperature = temperature if temperature is not None else rag["temperature"]

        conversation_context = _conversation_context(
            conversation_id,
            frozen_context=conversation_prompt_context,
        )
        retrieval_query = _retrieval_query(
            query,
            conversation_id,
            frozen_context=conversation_retrieval_context,
        )
        context_docs = _get_context(
            retrieval_query,
            effective_k,
            selected_model,
            settings,
            use_cache=False,
            collection_name=collection_name,
            conversation_id=conversation_id,
            knowledge_base_targets=knowledge_base_targets,
        )
        assert_distributed_locks_healthy()
        context_docs = _merge_context_docs(context_docs, extra_context_docs)
        provider_name = provider_config.get("name", provider_id)
        meta_event = {
            "type": "meta",
            "model": selected_model,
            "provider": provider_id,
            "provider_name": provider_name,
            "response_language": effective_response_language,
        }
        if conversation_id:
            meta_event["conversation_id"] = conversation_id
        yield meta_event

        answer_parts = []
        for chunk in generate_response(
            query,
            context_docs,
            model=selected_model,
            provider=provider_id,
            stream=True,
            temperature=effective_temperature,
            settings=settings,
            conversation_context=conversation_context,
            client_context=client_context,
            response_language=effective_response_language,
            custom_system_prompt=custom_system_prompt,
        ):
            if not chunk:
                continue
            answer_parts.append(chunk)
            yield {"type": "token", "text": chunk}

        done_event = {
            "type": "done",
            "answer": "".join(answer_parts),
            "model": selected_model,
            "provider": provider_id,
            "provider_name": provider_name,
            "response_language": effective_response_language,
            "context": _serialize_context(
                context_docs,
                file_index_path=file_index_path,
                knowledge_base_targets=knowledge_base_targets,
                include_downloads=not public,
            ),
            "sources": _serialize_sources(context_docs),
            "usage": None,
        }
        if conversation_id:
            done_event["conversation_id"] = conversation_id
        yield done_event
        if append_conversation_turn:
            _append_conversation_turn(
                conversation_id,
                query=query,
                answer="".join(answer_parts),
                provider=provider_id,
                model=selected_model,
                temperature=effective_temperature,
                settings=settings,
                knowledge_base_ids=conversation_knowledge_base_ids,
            )
    except Exception as e:
        log.error(f"Errore streaming RAG: {e}")
        yield {
            "type": "error",
            "error": str(e),
            "status": "server_error",
        }


def search_and_response(query, model=None, stream=False, temperature=None, k=None):
    """Backward-compatible facade used by legacy /ask route."""
    if stream:
        return query_rag_stream(query, model=model, temperature=temperature, k=k)
    return query_rag_non_stream(query, model=model, temperature=temperature, k=k)["answer"]


def _client_context_block(client_context: Optional[dict]) -> str:
    if not client_context:
        return "Nessun contesto client fornito."

    labels = {
        "site_name": "Sito",
        "page_title": "Pagina",
        "page_url": "URL pagina",
        "post_type": "Tipo contenuto",
        "locale": "Lingua",
        "instructions": "Istruzioni client",
    }
    lines = []
    for key, label in labels.items():
        value = client_context.get(key)
        if value:
            lines.append(f"{label}: {value}")
    return "\n".join(lines) or "Nessun contesto client fornito."


def _normalize_response_language(response_language: Optional[str]) -> str:
    language = str(response_language or "auto").strip().lower().replace("_", "-")
    return language or "auto"


def _response_language_instruction(response_language: Optional[str]) -> str:
    language = _normalize_response_language(response_language)
    if language == "auto":
        return "Rispondi nella stessa lingua della domanda dell'utente."
    if language == "it":
        return "Rispondi in italiano."
    if language == "en":
        return "Answer in English."
    return f"Rispondi nella lingua indicata dal codice '{language}'."


def generate_response(
    query,
    context_docs=None,
    model=None,
    provider=None,
    stream=False,
    temperature=None,
    settings=None,
    conversation_context: str = "",
    client_context: Optional[dict] = None,
    response_language: Optional[str] = None,
    custom_system_prompt: Optional[str] = None,
):
    settings = settings or _load_settings()
    rag = settings["rag"]
    provider_id, selected_model, _provider_config = ProviderFactory.resolve(
        model=model,
        provider=provider,
        settings=settings,
    )
    temperature = temperature if temperature is not None else rag["temperature"]

    if not context_docs and not rag["use_internal_knowledge"]:
        yield "Nessun documento caricato. Carica PDF dalla pagina admin File."
        return

    if context_docs:
        context = "\n\n---\n\n".join(
            _document_prompt_block(doc) for doc in context_docs
        )
        sources = [os.path.basename(doc.metadata.get("source", "?")) for doc in context_docs]
    else:
        context = ""
        sources = []
    log.info(f"Context: {len(context_docs)} docs ({len(context)} char) — sources: {sources}")
    language_instruction = _response_language_instruction(response_language)

    if rag["use_internal_knowledge"]:
        system = f"""Sei un assistente esperto. Rispondi integrando le informazioni del contesto con la tua conoscenza interna.
- {language_instruction}
- Dai prioritaria importanza alle informazioni del contesto quando sono presenti
- Usa il contesto conversazionale solo per capire riferimenti, preferenze e follow-up
- Se il contesto è insufficiente, completa la risposta con la tua conoscenza interna
- Tieni presente che la conoscenza interna potrebbe contenere informazioni non aggiornate; preferisci sempre il contesto quando disponibile
- Mantieni le risposte concise ma complete"""
    else:
        system = f"""Sei un assistente esperto. Rispondi usando SOLO le informazioni del contesto documentale e della conversazione.
- {language_instruction}
- Non inventare informazioni
- Usa il contesto conversazionale solo per capire riferimenti, preferenze e follow-up
- Ammetti se il contesto è insufficiente
- Mantieni le risposte concise ma complete"""

    if custom_system_prompt:
        system = f"{custom_system_prompt}\n\n{system}"

    conversation_block = conversation_context.strip() or "Nessun contesto conversazionale precedente."
    client_block = _client_context_block(client_context)

    context_section = (
        f"--- CONTESTO DOCUMENTALE ---\n{context}\n---\n" if context else ""
    )
    prompt = f"""{context_section}--- CONTESTO CONVERSAZIONE ---
{conversation_block}
---
--- CONTESTO CLIENT ---
{client_block}
---
--- DOMANDA ATTUALE ---
{query}"""

    provider_instance = ProviderFactory.get_provider(
        model=selected_model,
        provider=provider_id,
        settings=settings,
    )
    log.info(f"Provider: {provider_instance.provider_name} (model={selected_model})")

    if os.getenv("LLM_DEBUG_LOG", "false").lower() == "true":
        log.info("DEBUG LLM CALL enabled; prompts are omitted from normal logs to avoid leaking secrets/data.")

    try:
        if stream:
            yield from ErrorUtils.retry_stream_with_backoff(
                provider_instance.generate_stream,
                args=[system, prompt, selected_model, temperature],
                max_retries=3,
            )
        else:
            response = ErrorUtils.retry_with_backoff(
                provider_instance.generate,
                args=[system, prompt, selected_model, temperature],
                max_retries=3,
            )
            yield response
    except Exception as e:
        log.error(f"Errore Provider {provider_instance.provider_name}: {e}")
        yield f"Errore: {e}"


def _document_prompt_block(doc) -> str:
    metadata = getattr(doc, "metadata", {}) or {}
    source = os.path.basename(str(metadata.get("source") or "documento"))
    knowledge_base_name = str(metadata.get("knowledge_base_name") or "").strip()
    if knowledge_base_name:
        return (
            f"[Knowledge base: {knowledge_base_name} | Fonte: {source}]\n"
            f"{doc.page_content}"
        )
    return f"[Fonte: {source}]\n{doc.page_content}"


def get_available_models():
    settings = _load_settings()
    return [
        {
            "id": model.id,
            "name": model.name,
            "provider": model.provider,
            "provider_name": model.provider_name,
            "value": f"{model.provider}:{model.id}",
        }
        for model in ProviderRegistry(settings).list_models()
    ]


def get_cache_stats():
    settings = get_settings(Config.paths.settings_file)
    return {
        "enabled": settings["rag"]["enable_cache"],
        "ttl": settings["rag"]["cache_ttl"],
        "size": _cache.size,
        "backend": _cache.backend,
    }


def clear_cache():
    _cache.clear()


def clear_cache_for_collection(collection_name: str) -> int:
    return _cache.clear_collection(collection_name)


def _conversation_context(
    conversation_id: Optional[str],
    *,
    frozen_context: Optional[str] = None,
) -> str:
    if frozen_context is not None:
        return frozen_context
    return get_conversation_store().render_for_prompt(conversation_id)


def _retrieval_query(
    query: str,
    conversation_id: Optional[str],
    *,
    frozen_context: Optional[str] = None,
) -> str:
    context = (
        frozen_context
        if frozen_context is not None
        else get_conversation_store().render_for_retrieval(conversation_id)
    )
    if not context:
        return query

    return f"""Contesto conversazionale precedente:
{context}

Domanda attuale:
{query}"""


def _append_conversation_turn(
    conversation_id: Optional[str],
    *,
    query: str,
    answer: str,
    provider: str,
    model: str,
    temperature: float,
    settings: dict,
    knowledge_base_ids: Optional[list[str]] = None,
) -> None:
    if not conversation_id or not answer or answer.startswith("Errore:"):
        return

    assert_distributed_locks_healthy()
    store = get_conversation_store()
    summary_job = store.append_turn(
        conversation_id,
        user=query,
        assistant=answer,
        knowledge_base_ids=knowledge_base_ids,
    )
    if not summary_job:
        return

    try:
        summary = _summarize_conversation(
            summary_job,
            provider=provider,
            model=model,
            temperature=temperature,
            settings=settings,
        )
    except Exception as e:
        log.warning(f"Riassunto conversazione non riuscito, uso fallback locale: {e}")
        summary = fallback_summary(summary_job)

    assert_distributed_locks_healthy()
    store.apply_summary(summary_job, summary)


def _summarize_conversation(
    summary_job,
    *,
    provider: str,
    model: str,
    temperature: float,
    settings: dict,
) -> str:
    provider_instance = ProviderFactory.get_provider(
        model=model,
        provider=provider,
        settings=settings,
    )
    system = """Sei un assistente che comprime memoria conversazionale per una chat RAG.
- Scrivi in italiano.
- Mantieni fatti, preferenze utente, decisioni, vincoli, riferimenti a documenti e questioni aperte.
- Rimuovi saluti, ripetizioni e dettagli non utili ai follow-up.
- Non aggiungere informazioni nuove.
- Produci un riassunto operativo entro circa 2500 caratteri."""
    previous_summary = summary_job.previous_summary or "Nessun riassunto precedente."
    turns = format_turns(summary_job.turns_to_summarize)
    prompt = f"""Riassunto precedente:
{previous_summary}

Turni da incorporare nel riassunto:
{turns}

Restituisci solo il nuovo riassunto aggiornato."""

    summary = ErrorUtils.retry_with_backoff(
        provider_instance.generate,
        args=[system, prompt, model, min(temperature, 0.2)],
        max_retries=2,
    )
    return summary or fallback_summary(summary_job)


def _get_context(
    query: str,
    k: int,
    model: str,
    settings: dict,
    use_cache: bool = True,
    collection_name: Optional[str] = None,
    conversation_id: Optional[str] = None,
    knowledge_base_targets: Optional[list[dict]] = None,
):
    from utils.metrics import get_metrics

    metrics = get_metrics()
    retrieval_start = time.time()
    cached_results = None
    if knowledge_base_targets:
        canonical_collections = sorted(
            str(target["collection_name"])
            for target in knowledge_base_targets
        )
        cache_query = f"multi-v2:{'|'.join(canonical_collections)}\n{query}"
    else:
        cache_query = f"{collection_name or 'documents'}\n{query}"
    cache_namespace = conversation_id or "stateless"
    if use_cache and settings["rag"]["enable_cache"]:
        cached_results = _cache.get(cache_query, k, model, namespace=cache_namespace)

    if cached_results:
        elapsed = time.time() - retrieval_start
        metrics.observe_retrieval(duration=elapsed, docs_count=len(cached_results), cache_hit=True)
        metrics.set_context_docs_count(len(cached_results))
        return cached_results

    if knowledge_base_targets:
        context_docs = _get_federated_context(
            query,
            k,
            settings,
            knowledge_base_targets,
        )
    # Usa reranker se abilitato
    elif settings["rag"].get("reranker_enabled", False):
        # Logica standard RAG con reranking:
        # 1. Recupera i candidati da ChromaDB, con eventuale diversity pre-reranker
        # 2. Re-ranka per rilevanza
        # 3. Restituisci query_k (k) documenti finali
        top_n = settings["rag"].get("reranker_top_n", 20)
        diversity_mode = settings["rag"].get("reranker_diversity_mode", "none")
        score_threshold = settings["rag"].get("reranker_threshold", 0.0)
        reranker_model = settings["rag"].get("reranker_model", "local/BAAI/bge-reranker-v2-m3")
        reranker_type = settings["rag"].get("reranker_type", "local")
        
        provider_id, provider_model = _split_reranker_model(reranker_model)
        if provider_id == "local" or reranker_type == "local" and not provider_model:
            reranker = get_reranker(
                enabled=True,
                model_name=provider_model or reranker_model.removeprefix("local/")
            )
        else:
            provider_id = provider_id or reranker_type
            provider = _find_reranker_provider(settings, provider_id)
            base_url = provider.get("base_url") if provider else None
            api_key = (
                settings["rag"].get("reranker_api_key")
                or settings["rag"].get("reranker_regolo_api_key")
                or (resolve_api_key(provider) if provider else "")
            )
            requires_api_key = bool(provider.get("requires_api_key", False)) if provider else False
            reranker_mode = provider.get("reranker_mode", "chat_completions") if provider else "chat_completions"

            if provider and provider.get("enabled", True) and base_url and (api_key or not requires_api_key):
                reranker = get_reranker(
                    enabled=True,
                    model_name=provider_model,
                    base_url=base_url,
                    api_key=api_key or "openai-compatible-reranker",
                    mode=reranker_mode,
                )
            else:
                log.warning(
                    f"Provider ReRanking '{provider_id}' non configurato o incompleto "
                    f"(found={bool(provider)}, url={bool(base_url)}, key={bool(api_key)}). "
                    f"Uso DummyReranker."
                )
                from utils.reranker import DummyReranker
                reranker = DummyReranker()
        
        # Recupera top_n documenti, poi re-ranka e restituisce k finali
        rerank_kwargs = {
            "k": k,
            "top_n": top_n,
            "reranker": reranker,
            "score_threshold": score_threshold,
            "diversity_mode": diversity_mode,
            "mmr_lambda": settings["rag"].get("reranker_mmr_lambda", 0.7),
            "mmr_candidate_pool": settings["rag"].get("reranker_mmr_candidate_pool"),
        }
        if collection_name:
            rerank_kwargs["collection_name"] = collection_name
        context_docs = query_chroma_with_rerank(query, **rerank_kwargs)
    else:
        if collection_name:
            context_docs = query_chroma(query, k=k, collection_name=collection_name)
        else:
            context_docs = query_chroma(query, k=k)

    elapsed = time.time() - retrieval_start
    metrics.observe_retrieval(
        duration=elapsed,
        docs_count=len(context_docs),
        cache_hit=False,
    )
    metrics.set_context_docs_count(len(context_docs))

    if use_cache and settings["rag"]["enable_cache"]:
        _cache.set(cache_query, context_docs, k, model, namespace=cache_namespace)
    return context_docs


def _get_federated_context(
    query: str,
    k: int,
    settings: dict,
    knowledge_base_targets: list[dict],
) -> list:
    """Retrieve one globally ranked context from multiple Chroma collections."""

    targets = [dict(target) for target in knowledge_base_targets]
    if not targets:
        return []
    rag = settings["rag"]
    reranker_enabled = bool(rag.get("reranker_enabled", False))
    diversity_mode = str(
        rag.get("reranker_diversity_mode", "none")
    ).strip().lower()
    top_n = max(k, int(rag.get("reranker_top_n", 20)))
    mmr_pool = rag.get("reranker_mmr_candidate_pool")
    try:
        mmr_pool = int(mmr_pool) if mmr_pool is not None else 0
    except (TypeError, ValueError):
        mmr_pool = 0
    if reranker_enabled:
        global_budget = max(
            k,
            top_n,
            mmr_pool if diversity_mode == "mmr" else 0,
        )
    else:
        global_budget = max(k, k * 2)
    global_budget = min(200, global_budget)
    per_collection = min(
        50,
        max(k, math.ceil(global_budget / len(targets))),
    )

    query_embedding = EmbeddingFactory.get_provider().encode_query(query)
    include_embeddings = diversity_mode == "mmr"

    def retrieve(target: dict):
        docs, embeddings = query_chroma_by_embedding(
            query_embedding,
            k=per_collection,
            collection_name=target["collection_name"],
            include_embeddings=include_embeddings,
        )
        return target, docs, embeddings

    retrieved = []
    with ThreadPoolExecutor(max_workers=min(4, len(targets))) as executor:
        futures = [executor.submit(retrieve, target) for target in targets]
        for future in as_completed(futures):
            retrieved.append(future.result())
    assert_distributed_locks_healthy()

    grouped = {}
    for target, docs, embeddings in retrieved:
        for local_index, doc in enumerate(docs, start=1):
            metadata = dict(getattr(doc, "metadata", {}) or {})
            rrf_score = round(1.0 / (60 + local_index), 9)
            origin = _knowledge_base_origin(
                target,
                metadata,
                local_rank=local_index,
                rrf_score=rrf_score,
            )
            metadata.update(
                {
                    "knowledge_base_id": target["knowledge_base_id"],
                    "knowledge_base_name": target["knowledge_base_name"],
                    "knowledge_base_origins": [origin],
                    "local_rank": local_index,
                    "rrf_score": rrf_score,
                }
            )
            if include_embeddings and local_index <= len(embeddings):
                metadata["_rag_embedding"] = embeddings[local_index - 1]
            from langchain_core.documents import Document

            candidate = Document(
                page_content=str(getattr(doc, "page_content", "") or ""),
                metadata=metadata,
            )
            content_key = hashlib.sha256(
                _normalized_document_content(candidate.page_content).encode("utf-8")
            ).hexdigest()
            grouped.setdefault(content_key, []).append(candidate)

    candidates = []
    for duplicates in grouped.values():
        primary = min(duplicates, key=_federated_candidate_sort_key)
        origins = {}
        rrf_scores_by_knowledge_base = {}
        for duplicate in duplicates:
            duplicate_metadata = duplicate.metadata or {}
            knowledge_base_id = str(
                duplicate_metadata.get("knowledge_base_id") or ""
            )
            rrf_score = float(duplicate_metadata.get("rrf_score") or 0.0)
            rrf_scores_by_knowledge_base[knowledge_base_id] = max(
                rrf_scores_by_knowledge_base.get(knowledge_base_id, 0.0),
                rrf_score,
            )
            for origin in duplicate_metadata.get("knowledge_base_origins", []):
                key = (
                    str(origin.get("knowledge_base_id") or ""),
                    str(origin.get("source") or ""),
                    str(origin.get("chunk_id") or ""),
                )
                origins[key] = origin
        primary.metadata = {
            **primary.metadata,
            "rrf_score": round(sum(rrf_scores_by_knowledge_base.values()), 9),
            "knowledge_base_origins": [
                origins[key] for key in sorted(origins)
            ],
        }
        candidates.append(primary)

    candidates.sort(key=_federated_candidate_sort_key)
    candidates = candidates[:global_budget]

    if reranker_enabled:
        if diversity_mode == "mmr":
            candidates = _federated_mmr(
                candidates,
                limit=top_n,
                mmr_lambda=rag.get("reranker_mmr_lambda", 0.7),
            )
        elif diversity_mode == "source_diversity":
            candidates = _federated_source_diversity(
                candidates,
                limit=top_n,
                max_per_source=max(1, min(3, k)),
            )
        else:
            candidates = candidates[:top_n]
        reranker = _resolve_reranker(settings)
        candidates = reranker.rerank(query, candidates, k)
        threshold = float(rag.get("reranker_threshold", 0.0) or 0.0)
        if threshold > 0:
            candidates = [
                doc
                for doc in candidates
                if _document_float(doc, "reranker_score") is None
                or _document_float(doc, "reranker_score") >= threshold
            ]
    else:
        candidates = candidates[:k]

    for doc in candidates:
        doc.metadata = {
            key: value
            for key, value in (doc.metadata or {}).items()
            if key != "_rag_embedding"
        }
    return candidates


def _normalized_document_content(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _knowledge_base_origin(
    target: dict,
    metadata: dict,
    *,
    local_rank: int,
    rrf_score: float,
) -> dict:
    origin = {
        "knowledge_base_id": target["knowledge_base_id"],
        "knowledge_base_name": target["knowledge_base_name"],
        "source": str(metadata.get("source") or ""),
        "local_rank": local_rank,
        "rrf_score": rrf_score,
    }
    for key in ("chunk_id", "page", "page_number", "document_id"):
        if metadata.get(key) is not None:
            origin[key] = metadata[key]
    return origin


def _federated_candidate_sort_key(doc) -> tuple:
    metadata = getattr(doc, "metadata", {}) or {}
    chroma_score = _document_float(doc, "chroma_score")
    return (
        -float(metadata.get("rrf_score") or 0.0),
        -(chroma_score if chroma_score is not None else -1e9),
        str(metadata.get("knowledge_base_id") or ""),
        str(metadata.get("source") or ""),
        str(metadata.get("chunk_id") or ""),
    )


def _federated_source_diversity(
    docs,
    *,
    limit: int,
    max_per_source: int,
) -> list:
    selected = []
    overflow = []
    counts = {}
    for doc in docs:
        metadata = doc.metadata or {}
        key = (
            str(metadata.get("knowledge_base_id") or ""),
            str(metadata.get("source") or metadata.get("document_id") or ""),
        )
        if counts.get(key, 0) < max_per_source:
            selected.append(doc)
            counts[key] = counts.get(key, 0) + 1
        else:
            overflow.append(doc)
        if len(selected) >= limit:
            return selected
    return (selected + overflow)[:limit]


def _federated_mmr(docs, *, limit: int, mmr_lambda) -> list:
    try:
        weight = max(0.0, min(1.0, float(mmr_lambda)))
    except (TypeError, ValueError):
        weight = 0.7
    candidates = list(docs)
    selected = []
    while candidates and len(selected) < limit:
        scored = []
        for index, doc in enumerate(candidates):
            relevance = float((doc.metadata or {}).get("chroma_score") or 0.0)
            embedding = (doc.metadata or {}).get("_rag_embedding")
            redundancy = max(
                (
                    _cosine_similarity(
                        embedding,
                        (chosen.metadata or {}).get("_rag_embedding"),
                    )
                    for chosen in selected
                ),
                default=0.0,
            )
            score = (weight * relevance) - ((1.0 - weight) * redundancy)
            scored.append((score, -index, index, doc))
        _score, _position, best_index, best = max(
            scored,
            key=lambda item: (item[0], item[1]),
        )
        best.metadata = {**best.metadata, "mmr_score": round(_score, 6)}
        selected.append(best)
        candidates.pop(best_index)
    return selected


def _cosine_similarity(left, right) -> float:
    if left is None or right is None:
        return 0.0
    try:
        pairs = [(float(a), float(b)) for a, b in zip(left, right)]
    except (TypeError, ValueError):
        return 0.0
    if not pairs:
        return 0.0
    left_norm = math.sqrt(sum(a * a for a, _ in pairs))
    right_norm = math.sqrt(sum(b * b for _, b in pairs))
    if not left_norm or not right_norm:
        return 0.0
    return sum(a * b for a, b in pairs) / (left_norm * right_norm)


def _document_float(doc, key: str) -> Optional[float]:
    try:
        value = (doc.metadata or {}).get(key)
        return float(value) if value is not None else None
    except (AttributeError, TypeError, ValueError):
        return None


def _resolve_reranker(settings: dict):
    rag = settings["rag"]
    reranker_model = rag.get(
        "reranker_model",
        "local/BAAI/bge-reranker-v2-m3",
    )
    reranker_type = rag.get("reranker_type", "local")
    provider_id, provider_model = _split_reranker_model(reranker_model)
    if provider_id == "local" or (
        reranker_type == "local" and not provider_model
    ):
        return get_reranker(
            enabled=True,
            model_name=provider_model or reranker_model.removeprefix("local/"),
        )
    provider_id = provider_id or reranker_type
    provider = _find_reranker_provider(settings, provider_id)
    base_url = provider.get("base_url") if provider else None
    api_key = (
        rag.get("reranker_api_key")
        or rag.get("reranker_regolo_api_key")
        or (resolve_api_key(provider) if provider else "")
    )
    requires_api_key = bool(provider.get("requires_api_key", False)) if provider else False
    if (
        provider
        and provider.get("enabled", True)
        and base_url
        and (api_key or not requires_api_key)
    ):
        return get_reranker(
            enabled=True,
            model_name=provider_model,
            base_url=base_url,
            api_key=api_key or "openai-compatible-reranker",
            mode=provider.get("reranker_mode", "chat_completions"),
        )
    from utils.reranker import DummyReranker

    return DummyReranker()


def _merge_context_docs(context_docs, extra_context_docs: Optional[list] = None) -> list:
    merged = list(context_docs or [])
    seen = {
        (
            str(getattr(doc, "metadata", {}).get("source") or ""),
            str(getattr(doc, "metadata", {}).get("chunk_id") or ""),
            str(getattr(doc, "page_content", "") or "")[:120],
        )
        for doc in merged
    }
    for doc in extra_context_docs or []:
        key = (
            str(getattr(doc, "metadata", {}).get("source") or ""),
            str(getattr(doc, "metadata", {}).get("chunk_id") or ""),
            str(getattr(doc, "page_content", "") or "")[:120],
        )
        if key in seen:
            continue
        merged.append(doc)
        seen.add(key)
    return merged


def _serialize_context(
    context_docs,
    file_index_path: Optional[str] = None,
    knowledge_base_targets: Optional[list[dict]] = None,
    include_downloads: bool = True,
) -> List[dict]:
    target_indexes = {
        str(target.get("knowledge_base_id")): FileIndex(
            target.get("file_index_path") or file_index_path or Config.paths.file_index
        )
        for target in (knowledge_base_targets or [])
    }
    fallback_index = FileIndex(file_index_path or Config.paths.file_index)
    serialized = []
    for doc in (context_docs or []):
        entry = {
            "text": doc.page_content,
            "metadata": _public_document_metadata(doc.metadata),
        }
        if include_downloads:
            knowledge_base_id = str(
                (getattr(doc, "metadata", {}) or {}).get("knowledge_base_id")
                or ""
            )
            entry["download_url"] = _get_download_url(
                target_indexes.get(knowledge_base_id, fallback_index),
                doc,
            )
        serialized.append(entry)
    return serialized


def _serialize_sources(context_docs) -> List[dict]:
    return [_source_payload(doc) for doc in (context_docs or [])]


def _source_payload(doc) -> dict:
    metadata = dict(doc.metadata or {})
    source = str(metadata.get("source") or "")
    filename = _public_source_name(source) if source else "document"
    source_type = _public_metadata_scalar(metadata.get("source_type"))
    if not isinstance(source_type, str) or not source_type:
        source_type = _source_type_from_filename(filename)
    payload = {
        "filename": filename,
        "source_type": source_type,
        "snippet": _source_snippet(doc.page_content),
    }
    for key in (
        "knowledge_base_id",
        "knowledge_base_name",
    ):
        if metadata.get(key) is not None:
            payload[key] = _public_metadata_scalar(metadata[key])
    if metadata.get("knowledge_base_origins") is not None:
        payload["knowledge_base_origins"] = _public_knowledge_base_origins(
            metadata["knowledge_base_origins"]
        )
    for key in ("chunk_id", "page", "page_number"):
        if metadata.get(key) is not None:
            public_key = "page" if key == "page_number" else key
            scalar = _public_metadata_scalar(metadata[key])
            if scalar is not None:
                payload[public_key] = scalar
    if metadata.get("reranker_score") is not None:
        score = _public_metadata_scalar(metadata["reranker_score"])
        if score is not None:
            payload["score"] = score
    return payload


_PUBLIC_DOCUMENT_METADATA_KEYS = {
    "attachment_id",
    "chunk_id",
    "chunk_index",
    "document_id",
    "end_time",
    "filename",
    "heading",
    "knowledge_base_id",
    "knowledge_base_name",
    "local_rank",
    "page",
    "page_number",
    "reranker_score",
    "rrf_score",
    "score",
    "section",
    "source_id",
    "source_type",
    "start_time",
    "temporary_attachment",
}

_PUBLIC_ORIGIN_METADATA_KEYS = {
    "chunk_id",
    "document_id",
    "knowledge_base_id",
    "knowledge_base_name",
    "local_rank",
    "page",
    "page_number",
    "rrf_score",
}


def _public_document_metadata(value: object) -> dict:
    """Build public metadata from an allowlist, never from recursive copying.

    Connector metadata may contain credentials, internal URLs, prompts and
    local paths.  Only fields required by the query UI/API are exposed.
    """

    metadata = value if isinstance(value, dict) else {}
    public = {}
    source = metadata.get("source")
    if source:
        public["source"] = _public_source_reference(source)
    for key in _PUBLIC_DOCUMENT_METADATA_KEYS:
        if metadata.get(key) is not None:
            scalar = _public_metadata_scalar(metadata[key])
            if scalar is not None:
                public[key] = scalar
    if metadata.get("knowledge_base_origins") is not None:
        public["knowledge_base_origins"] = _public_knowledge_base_origins(
            metadata["knowledge_base_origins"]
        )
    return public


def _public_knowledge_base_origins(value: object) -> list[dict]:
    origins = []
    if not isinstance(value, (list, tuple)):
        return origins
    for raw_origin in value:
        if not isinstance(raw_origin, dict):
            continue
        origin = {}
        if raw_origin.get("source"):
            origin["source"] = _public_source_reference(raw_origin["source"])
        for key in _PUBLIC_ORIGIN_METADATA_KEYS:
            if raw_origin.get(key) is not None:
                scalar = _public_metadata_scalar(raw_origin[key])
                if scalar is not None:
                    origin[key] = scalar
        origins.append(origin)
    return origins


def _public_metadata_scalar(value: object):
    if isinstance(value, os.PathLike):
        return _public_source_reference(os.fspath(value))
    if isinstance(value, str):
        return value[:1024]
    if isinstance(value, (bool, int, float)) or value is None:
        return value
    return None


def _public_source_reference(value: object) -> str:
    """Reduce local paths and remote URLs to a non-sensitive public name."""

    return _public_source_name(os.fspath(value) if isinstance(value, os.PathLike) else str(value or ""))


def _public_source_name(value: str) -> str:
    normalized = str(value or "").strip().replace("\\", "/").rstrip("/")
    if not normalized:
        return "document"
    parsed = urlsplit(normalized)
    if parsed.scheme.lower() in {"http", "https", "file"}:
        normalized = unquote(parsed.path).rstrip("/")
    return normalized.rsplit("/", 1)[-1] or "document"


def _source_type_from_filename(filename: str) -> str:
    extension = os.path.splitext(filename.lower())[1].lstrip(".")
    if extension == "pdf":
        return "pdf"
    if extension == "md":
        return "markdown"
    if extension == "txt":
        return "text"
    if extension in {"mp3", "wav", "m4a", "webm", "ogg", "flac"}:
        return "audio"
    return "document"


def _source_snippet(text: str, limit: int = 240) -> str:
    cleaned = " ".join(str(text or "").split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: max(0, limit - 3)].rstrip() + "..."


def _split_reranker_model(value: str) -> tuple[str, str]:
    provider_id, separator, model_name = str(value or "").partition("/")
    return provider_id, model_name if separator else ""


def _find_reranker_provider(settings: dict, provider_id: str) -> Optional[dict]:
    for provider in load_builtin_reranker_providers():
        if provider.get("id") == provider_id:
            return provider
    for provider in settings.get("reranker_providers", []):
        if provider.get("id") == provider_id:
            return provider
    return None


def _get_download_url(file_index: FileIndex, doc) -> Optional[str]:
    source = doc.metadata.get("source") or ""
    document_id = doc.metadata.get("document_id") or ""
    source_id = doc.metadata.get("source_id") or ""
    
    if not source:
        return None
    
    filename = os.path.basename(source)
    
    for entry in file_index.list():
        if entry.get("status") != "indexed":
            continue
        
        entry_doc_id = entry.get("metadata", {}).get("document_id", "")
        entry_source_id_full = entry.get("metadata", {}).get("source_id", "")
        
        if source_id and entry_source_id_full and source_id == entry_source_id_full:
            return f"/admin/files/download/{entry.get('filename', filename)}"
        
        if document_id and entry_doc_id and document_id == entry_doc_id:
            return f"/admin/files/download/{entry.get('filename', filename)}"
    
    entry = file_index.get(filename)
    if entry and entry.get("status") == "indexed":
        return f"/admin/files/download/{filename}"
    
    return None



def _first_reranker_model() -> str:
    for prov in load_builtin_reranker_providers():
        return prov.get('default_model', 'Qwen3-Reranker-4B')
    return 'Qwen3-Reranker-4B'


def _first_reranker_base_url() -> str:
    for prov in load_builtin_reranker_providers():
        return prov.get('base_url', '')
    return ''


def _load_settings(settings_path: Optional[str] = None) -> dict:
    return get_settings(settings_path or Config.paths.settings_file)
