import os
import json
import asyncio
from typing import Optional, Generator, AsyncGenerator
from .base import BaseLLMProvider
from .error_utils import log_http_error
from .exceptions import ProviderError, RateLimitError, TimeoutError
from utils.model_defaults import load_builtin_provider_definitions


class RegoloProvider(BaseLLMProvider):
    """Regolo.ai provider (OpenAI-compatible) con supporto async nativo"""

    PROVIDER_ID = "regolo"
    BASE_URL = "https://api.regolo.ai/v1"

    def __init__(self):
        api_key = os.getenv("REGOLO_API_KEY") or os.getenv("MISTRAL_API_KEY")
        if not api_key:
            raise ValueError("REGOLO_API_KEY o MISTRAL_API_KEY non configurata")

        # Client sincrono (per retrocompatibilita')
        from openai import OpenAI
        self._client = OpenAI(
            api_key=api_key,
            base_url=self.BASE_URL
        )

        # Client ASINCRONO nativo
        try:
            from openai import AsyncOpenAI
            self._async_client = AsyncOpenAI(
                api_key=api_key,
                base_url=self.BASE_URL
            )
            self._has_async_client = True
        except ImportError:
            # Fallback: useremo run_in_executor
            self._async_client = None
            self._has_async_client = False
            import warnings
            warnings.warn(
                "openai.AsyncOpenAI non disponibile. "
                "Le chiamate async useranno run_in_executor.",
                RuntimeWarning
            )

    @property
    def provider_name(self) -> str:
        return "Regolo.ai"

    def _handle_error(self, error: Exception, model: str = "", stream: bool = False) -> None:
        log_http_error(self.provider_name, model, self.BASE_URL, error, stream)
        error_msg = str(error).lower()
        if "rate" in error_msg or "429" in error_msg:
            raise RateLimitError(str(error))
        elif "timeout" in error_msg or "504" in error_msg:
            raise TimeoutError(str(error))
        raise ProviderError(f"Regolo API error: {error}")
    
    def generate(self, 
                  system: str, 
                  user: str, 
                  model: Optional[str] = None,
                  temperature: float = 0.3) -> str:
        try:
            response = self._client.chat.completions.create(
                model=model or self._default_model(),
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user}
                ],
                temperature=temperature
            )

            # Debug: log raw JSON response
            if os.getenv("LLM_DEBUG_LOG", "false").lower() == "true":
                from ..logging_config import PROVIDER_LOGGER as log
                raw = {
                    "id": response.id,
                    "model": response.model,
                    "usage": response.usage.model_dump() if hasattr(response.usage, "model_dump") else vars(response.usage),
                    "choices": response.choices[0].model_dump() if hasattr(response.choices, "__iter__") else None,
                }
                log.info("=== REGOLO RAW RESPONSE ===")
                log.info(json.dumps(raw, indent=2, default=str))
                log.info("=============================")

            return response.choices[0].message.content or ""
             
        except Exception as e:
            self._handle_error(e, model or self._default_model(), stream=False)
    
    def is_model_available(self, model: str) -> bool:
        return model in self._available_models()
    
    def generate_stream(self, 
                       system: str, 
                       user: str, 
                       model: Optional[str] = None,
                       temperature: float = 0.3) -> Generator[str, None, None]:
        try:
            response = self._client.chat.completions.create(
                model=model or self._default_model(),
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user}
                ],
                temperature=temperature,
                stream=True
            )
            for chunk in response:
                if chunk.choices and chunk.choices[0].delta.content:
                    yield chunk.choices[0].delta.content
        except Exception as e:
            self._handle_error(e, model or self._default_model(), stream=True)
    
    async def generate_async(self, 
                            system: str, 
                            user: str, 
                            model: Optional[str] = None,
                            temperature: float = 0.3) -> str:
        """Generate response using native async client"""
        model = model or self._default_model()
        
        if self._has_async_client:
            # Usa client async nativo
            try:
                response = await self._async_client.chat.completions.create(
                    model=model,
                    messages=[
                        {"role": "system", "content": system},
                        {"role": "user", "content": user}
                    ],
                    temperature=temperature
                )
                return response.choices[0].message.content or ""
            except Exception as e:
                self._handle_error(e, model, stream=False)
        else:
            # Fallback: esegui in executor
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(
                None, 
                lambda: self.generate(system, user, model, temperature)
            )
    
    async def generate_stream_async(self, 
                                   system: str, 
                                   user: str, 
                                   model: Optional[str] = None,
                                   temperature: float = 0.3) -> AsyncGenerator[str, None]:
        """Stream response using native async client - VERA implementazione async"""
        model = model or self._default_model()
        
        if self._has_async_client:
            # Usa client async nativo - VERO STREAMING ASYNC
            try:
                response = await self._async_client.chat.completions.create(
                    model=model,
                    messages=[
                        {"role": "system", "content": system},
                        {"role": "user", "content": user}
                    ],
                    temperature=temperature,
                    stream=True
                )
                async for chunk in response:
                    if chunk.choices and chunk.choices[0].delta.content:
                        yield chunk.choices[0].delta.content
            except Exception as e:
                self._handle_error(e, model, stream=True)
        else:
            # Fallback: stream wrapper con queue per non bloccare event loop
            loop = asyncio.get_running_loop()
            queue = asyncio.Queue()
            
            def sync_stream():
                try:
                    gen = self.generate_stream(system, user, model, temperature)
                    for chunk in gen:
                        loop.call_soon_threadsafe(queue.put_nowait, chunk)
                except Exception as e:
                    loop.call_soon_threadsafe(queue.put_nowait, e)
                finally:
                    loop.call_soon_threadsafe(queue.put_nowait, None)
            
            future = loop.run_in_executor(None, sync_stream)
            
            while True:
                item = await queue.get()
                if item is None:
                    break
                if isinstance(item, Exception):
                    raise item
                yield item
            await future

    def _provider_definition(self) -> dict:
        return load_builtin_provider_definitions()[self.PROVIDER_ID]

    def _available_models(self) -> list[str]:
        return [str(model) for model in self._provider_definition().get("models", [])]

    def _default_model(self) -> str:
        definition = self._provider_definition()
        models = [str(model) for model in definition.get("models", [])]
        return str(definition.get("default_model") or (models[0] if models else ""))
