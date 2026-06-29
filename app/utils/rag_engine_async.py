import asyncio
import os
from typing import Optional, AsyncGenerator
from config import Config
from utils import RAG_LOGGER as log
from utils.chroma_manager import query_chroma_async
from utils.providers.provider_factory import ProviderFactory
from utils.retry import ErrorUtils
from utils.cache import RAGCache


_cache = RAGCache()


async def search_and_response_async(
    query: str, 
    model: Optional[str] = None, 
    stream: bool = False
) -> AsyncGenerator[str, None]:
    """Async version di search_and_response con client nativi async"""
    log.info(f"RAG Async Query: '{query}' (model={model}, stream={stream})")
    k = Config.rag.query_k
    model = model or Config.rag.default_model
    
    # Cache check (operazione sincrona veloce)
    if not stream and Config.rag.enable_cache:
        cached_results = _cache.get(query, k, model)
        if cached_results:
            log.debug("Cache hit (async)")
            async for item in generate_response_async(query, cached_results, model, stream=stream):
                yield item
            return
    
    # Query Chroma async
    context_docs = await query_chroma_async(query, k)
    
    # Cache set (operazione sincrona veloce)
    if not stream and Config.rag.enable_cache:
        _cache.set(query, context_docs, k, model)
    
    # Generate response async
    async for item in generate_response_async(query, context_docs, model, stream=stream):
        yield item


async def generate_response_async(
    query: str, 
    context_docs=None, 
    model: Optional[str] = None, 
    stream: bool = False
) -> AsyncGenerator[str, None]:
    """Async version di generate_response con client nativi async"""
    model = model or Config.rag.default_model
    
    if not context_docs:
        yield "Nessun documento caricato. Carica PDF con il form in alto."
        return
    
    context = "\n\n---\n\n".join(doc.page_content for doc in context_docs)
    sources = [os.path.basename(doc.metadata.get("source", "?")) for doc in context_docs]
    log.info(f"Context: {len(context_docs)} docs ({len(context)} char) — sources: {sources}")
    
    if Config.rag.use_internal_knowledge:
        system = """Sei un assistente esperto. Rispondi integrando le informazioni del contesto con la tua conoscenza interna.
- Rispondi in italiano
- Dai prioritaria importanza alle informazioni del contesto quando sono presenti
- Se il contesto è insufficiente, completa la risposta con la tua conoscenza interna
- Tieni presente che la conoscenza interna potrebbe contenere informazioni non aggiornate; preferisci sempre il contesto quando disponibile
- Mantieni le risposte concise ma complete"""
    else:
        system = """Sei un assistente esperto. Rispondi usando SOLO le informazioni del contesto.
- Rispondi in italiano
- Non inventare informazioni
- Ammetti se il contesto è insufficiente
- Mantieni le risposte concise ma complete"""

    prompt = f"""--- CONTESTO ---
{context}
---
--- DOMANDA ---
{query}"""

    provider = ProviderFactory.get_provider(model)
    log.info(f"Provider: {provider.provider_name} (model={model})")
    
    try:
        if stream:
            # Usa il metodo async nativo del provider
            async for chunk in ErrorUtils.retry_stream_with_backoff_async(
                provider.generate_stream_async,
                args=[system, prompt, model],
                max_retries=3
            ):
                yield chunk
        else:
            # Usa il metodo async nativo del provider
            response = await ErrorUtils.retry_with_backoff_async(
                provider.generate_async,
                args=[system, prompt, model],
                max_retries=3
            )
            yield response
    except Exception as e:
        log.error(f"Errore Provider {provider.provider_name}: {e}")
        yield f"Errore: {e}"
