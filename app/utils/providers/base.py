from abc import ABC, abstractmethod
import asyncio
from typing import Optional, Generator, AsyncGenerator

class BaseLLMProvider(ABC):
    """Abstract base class for LLM providers"""
    
    @property
    @abstractmethod
    def provider_name(self) -> str:
        """Return provider name"""
        pass
    
    @abstractmethod
    def generate(self, 
                 system: str, 
                 user: str, 
                 model: Optional[str] = None,
                 temperature: float = 0.3) -> str:
        """Generate response from context and query"""
        pass
    
    def generate_stream(self, 
                        system: str, 
                        user: str, 
                        model: Optional[str] = None,
                        temperature: float = 0.3) -> Generator[str, None, None]:
        """Generate response with streaming (default: non-streaming fallback)"""
        full_response = self.generate(system, user, model, temperature)
        yield full_response
    
    async def generate_async(self, 
                             system: str, 
                             user: str, 
                             model: Optional[str] = None,
                             temperature: float = 0.3) -> str:
        """Async version of generate"""
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
        """Async version of generate_stream"""
        response = await self.generate_async(system, user, model, temperature)
        for chunk in [response]:
            yield chunk
    
    @abstractmethod
    def is_model_available(self, model: str) -> bool:
        """Check if model is available"""
        pass
