import os
import time
from typing import Dict, Generator, List, Optional

from config import Config
from utils import RAG_LOGGER as log
from utils.cache import RAGCache
from utils.chroma_manager import query_chroma, query_chroma_with_rerank
from utils.conversation_memory import (
    fallback_summary,
    format_turns,
    get_conversation_store,
)
from utils.reranker import get_reranker
from utils.file_index import FileIndex
from utils.providers.provider_factory import ProviderFactory
from utils.providers.registry import ProviderRegistry
from utils.provider_config import resolve_api_key
from utils.retry import ErrorUtils
from utils.settings_store import get_settings
from .model_defaults import load_builtin_reranker_providers


_cache = RAGCache()


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

    conversation_context = _conversation_context(conversation_id)
    retrieval_query = _retrieval_query(query, conversation_id)
    context_docs = _get_context(retrieval_query, effective_k, selected_model, settings, collection_name=collection_name)
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
        )
    )
    _append_conversation_turn(
        conversation_id,
        query=query,
        answer=answer,
        provider=provider_id,
        model=selected_model,
        temperature=effective_temperature,
        settings=settings,
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
            include_downloads=not public,
        ),
        "sources": _serialize_sources(context_docs),
        "usage": None,
    }
    if conversation_id:
        result["conversation_id"] = conversation_id
    return result


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
) -> Generator[str, None, None]:
    settings = _load_settings(settings_path)
    rag = settings["rag"]
    effective_response_language = _normalize_response_language(response_language)
    provider_id, selected_model, _provider_config = ProviderFactory.resolve(model=model, provider=provider, settings=settings)
    effective_k = k or rag["query_k"]
    effective_temperature = temperature if temperature is not None else rag["temperature"]

    conversation_context = _conversation_context(conversation_id)
    retrieval_query = _retrieval_query(query, conversation_id)
    context_docs = _get_context(
        retrieval_query,
        effective_k,
        selected_model,
        settings,
        use_cache=False,
        collection_name=collection_name,
    )
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
    ):
        answer_parts.append(chunk)
        yield chunk

    _append_conversation_turn(
        conversation_id,
        query=query,
        answer="".join(answer_parts),
        provider=provider_id,
        model=selected_model,
        temperature=effective_temperature,
        settings=settings,
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

        conversation_context = _conversation_context(conversation_id)
        retrieval_query = _retrieval_query(query, conversation_id)
        context_docs = _get_context(
            retrieval_query,
            effective_k,
            selected_model,
            settings,
            use_cache=False,
            collection_name=collection_name,
        )
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
                include_downloads=not public,
            ),
            "sources": _serialize_sources(context_docs),
            "usage": None,
        }
        if conversation_id:
            done_event["conversation_id"] = conversation_id
        yield done_event
        _append_conversation_turn(
            conversation_id,
            query=query,
            answer="".join(answer_parts),
            provider=provider_id,
            model=selected_model,
            temperature=effective_temperature,
            settings=settings,
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
):
    settings = settings or _load_settings()
    rag = settings["rag"]
    provider_id, selected_model, _provider_config = ProviderFactory.resolve(
        model=model,
        provider=provider,
        settings=settings,
    )
    temperature = temperature if temperature is not None else rag["temperature"]

    if not context_docs:
        yield "Nessun documento caricato. Carica PDF dalla pagina admin File."
        return

    context = "\n\n---\n\n".join(doc.page_content for doc in context_docs)
    sources = [os.path.basename(doc.metadata.get("source", "?")) for doc in context_docs]
    log.info(f"Context: {len(context_docs)} docs ({len(context)} char) � sources: {sources}")
    language_instruction = _response_language_instruction(response_language)

    if rag["use_internal_knowledge"]:
        system = f"""Sei un assistente esperto. Rispondi integrando le informazioni del contesto con la tua conoscenza interna.
- {language_instruction}
- Dai prioritaria importanza alle informazioni del contesto quando sono presenti
- Usa il contesto conversazionale solo per capire riferimenti, preferenze e follow-up
- Se il contesto � insufficiente, completa la risposta con la tua conoscenza interna
- Tieni presente che la conoscenza interna potrebbe contenere informazioni non aggiornate; preferisci sempre il contesto quando disponibile
- Mantieni le risposte concise ma complete"""
    else:
        system = f"""Sei un assistente esperto. Rispondi usando SOLO le informazioni del contesto documentale e della conversazione.
- {language_instruction}
- Non inventare informazioni
- Usa il contesto conversazionale solo per capire riferimenti, preferenze e follow-up
- Ammetti se il contesto � insufficiente
- Mantieni le risposte concise ma complete"""

    conversation_block = conversation_context.strip() or "Nessun contesto conversazionale precedente."
    client_block = _client_context_block(client_context)

    prompt = f"""--- CONTESTO DOCUMENTALE ---
{context}
---
--- CONTESTO CONVERSAZIONE ---
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


def _conversation_context(conversation_id: Optional[str]) -> str:
    return get_conversation_store().render_for_prompt(conversation_id)


def _retrieval_query(query: str, conversation_id: Optional[str]) -> str:
    context = get_conversation_store().render_for_retrieval(conversation_id)
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
) -> None:
    if not conversation_id or not answer or answer.startswith("Errore:"):
        return

    store = get_conversation_store()
    summary_job = store.append_turn(conversation_id, user=query, assistant=answer)
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
):
    from utils.metrics import get_metrics

    metrics = get_metrics()
    retrieval_start = time.time()
    cached_results = None
    cache_query = f"{collection_name or 'documents'}\n{query}"
    if use_cache and settings["rag"]["enable_cache"]:
        cached_results = _cache.get(cache_query, k, model)

    cache_hit = bool(cached_results)
    is_cached_or_enabled = cached_results is not None or (use_cache and settings["rag"]["enable_cache"])

    if cached_results:
        elapsed = time.time() - retrieval_start
        metrics.observe_retrieval(duration=elapsed, docs_count=len(cached_results), cache_hit=True)
        metrics.set_context_docs_count(len(cached_results))
        return cached_results

    # Usa reranker se abilitato
    if settings["rag"].get("reranker_enabled", False):
        # Logica standard RAG con reranking:
        # 1. Recupera reranker_top_n documenti da ChromaDB
        # 2. Re-ranka per rilevanza
        # 3. Restituisci query_k (k) documenti finali
        top_n = settings["rag"].get("reranker_top_n", 20)
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
        _cache.set(cache_query, context_docs, k, model)
    return context_docs


def _serialize_context(
    context_docs,
    file_index_path: Optional[str] = None,
    include_downloads: bool = True,
) -> List[dict]:
    file_index = FileIndex(file_index_path or Config.paths.file_index)
    serialized = []
    for doc in (context_docs or []):
        entry = {
            "text": doc.page_content,
            "metadata": doc.metadata,
        }
        if include_downloads:
            entry["download_url"] = _get_download_url(file_index, doc)
        serialized.append(entry)
    return serialized


def _serialize_sources(context_docs) -> List[dict]:
    return [_source_payload(doc) for doc in (context_docs or [])]


def _source_payload(doc) -> dict:
    metadata = dict(doc.metadata or {})
    source = str(metadata.get("source") or "")
    filename = os.path.basename(source) if source else "document"
    source_type = metadata.get("source_type") or _source_type_from_filename(filename)
    payload = {
        "filename": filename,
        "source_type": source_type,
        "snippet": _source_snippet(doc.page_content),
    }
    for key in ("chunk_id", "page", "page_number"):
        if metadata.get(key) is not None:
            public_key = "page" if key == "page_number" else key
            payload[public_key] = metadata[key]
    if metadata.get("reranker_score") is not None:
        payload["score"] = metadata["reranker_score"]
    return payload


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
        
        entry_source_id = entry.get("metadata", {}).get("index_profile", {})
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
