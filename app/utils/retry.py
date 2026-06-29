import time
import asyncio
from typing import Type, Tuple, Generator, Callable, Any, AsyncGenerator
from .logging_config import APP_LOGGER

class RetryConfig:
    max_retries: int = 3
    backoff_factor: float = 1.0
    exponential_base: float = 2.0
    timeout: int = 60

class ErrorUtils:
    @staticmethod
    def get_exception_type(error: Exception) -> str:
        error_msg = str(error).lower()
        if "rate" in error_msg or "429" in error_msg:
            return "rate_limit"
        elif "timeout" in error_msg or "504" in error_msg:
            return "timeout"
        elif "401" in error_msg or "unauthorized" in error_msg:
            return "auth"
        elif "403" in error_msg or "forbidden" in error_msg:
            return "permission"
        elif "not found" in error_msg or "404" in error_msg:
            return "not_found"
        return "unknown"
    
    @staticmethod
    def is_retryable(error: Exception) -> bool:
        error_type = ErrorUtils.get_exception_type(error)
        return error_type in ("rate_limit", "timeout", "unknown")
    
    @staticmethod
    def wait_with_backoff(attempt: int, base_factor: float = 1.0, base: float = 2.0) -> float:
        wait_time = base_factor * (base ** attempt)
        APP_LOGGER.info(f"Wait {wait_time:.1f}s before retry...")
        time.sleep(wait_time)
        return wait_time

    @staticmethod
    async def wait_with_backoff_async(attempt: int, base_factor: float = 1.0, base: float = 2.0) -> float:
        wait_time = base_factor * (base ** attempt)
        APP_LOGGER.info(f"Async wait {wait_time:.1f}s before retry...")
        await asyncio.sleep(wait_time)
        return wait_time

    @staticmethod
    def retry_with_backoff(func, args=None, kwargs=None, max_retries=3, exceptions=(Exception,)):
        """Retry callable with exponential backoff"""
        if args is None:
            args = ()
        if kwargs is None:
            kwargs = {}
        
        last_error = None
        for attempt in range(max_retries):
            try:
                return func(*args, **kwargs)
            except exceptions as e:
                last_error = e
                if attempt < max_retries - 1 and ErrorUtils.is_retryable(e):
                    ErrorUtils.wait_with_backoff(attempt)
                else:
                    break
        
        if last_error is not None:
            raise last_error
        raise RuntimeError(f"Retry failed for {func.__name__}: no attempts were made (max_retries=0)")

    @staticmethod
    def retry_stream_with_backoff(func: Callable[..., Generator[str, None, None]],
                                  args=None, 
                                  kwargs=None, 
                                  max_retries=3,
                                  exceptions=(Exception,)) -> Generator[str, None, None]:
        """Retry generator with exponential backoff for streaming"""
        if args is None:
            args = ()
        if kwargs is None:
            kwargs = {}
        
        last_error = None
        for attempt in range(max_retries):
            try:
                yield from func(*args, **kwargs)
                return
            except exceptions as e:
                last_error = e
                if attempt < max_retries - 1 and ErrorUtils.is_retryable(e):
                    ErrorUtils.wait_with_backoff(attempt)
                else:
                    break
        
        if last_error is not None:
            raise last_error
        raise RuntimeError(f"Retry failed for {func.__name__}: no attempts were made (max_retries=0)")
        
    @staticmethod
    async def retry_with_backoff_async(func: Callable[..., Any],
                                       args=None, 
                                       kwargs=None, 
                                       max_retries=3,
                                       exceptions=(Exception,)) -> Any:
        """Async retry with exponential backoff"""
        if args is None:
            args = ()
        if kwargs is None:
            kwargs = {}
        
        loop = asyncio.get_running_loop()
        
        last_error = None
        for attempt in range(max_retries):
            try:
                if asyncio.iscoroutinefunction(func):
                    return await func(*args, **kwargs)
                else:
                    return await loop.run_in_executor(None, lambda: func(*args, **kwargs))
            except exceptions as e:
                last_error = e
                if attempt < max_retries - 1 and ErrorUtils.is_retryable(e):
                    await ErrorUtils.wait_with_backoff_async(attempt)
                else:
                    break
        
        if last_error is not None:
            raise last_error
        raise RuntimeError(f"Retry failed for {func.__name__}: no attempts were made (max_retries=0)")
        
    @staticmethod
    async def retry_stream_with_backoff_async(
        func: Callable[..., Any], 
        args=None, 
        kwargs=None, 
        max_retries=3,
        exceptions=(Exception,)
    ) -> AsyncGenerator[str, None]:
        """Async retry generator with exponential backoff for streaming"""
        if args is None:
            args = ()
        if kwargs is None:
            kwargs = {}
        
        loop = asyncio.get_running_loop()
        last_error = None
        
        for attempt in range(max_retries):
            try:
                # Prova prima come async generator
                # Call the function and check if it returns an async generator or coroutine
                result = func(*args, **kwargs)
                
                # Check if it's a coroutine (needs await)
                if asyncio.iscoroutine(result):
                    gen = await result
                    async for item in gen:
                        yield item
                    return
                
                # Check if it's already an async generator
                elif hasattr(result, '__aiter__'):
                    async for item in result:
                        yield item
                    return
                
                # Check if it's a regular generator
                elif hasattr(result, '__iter__'):
                    # Usiamo queue pattern per non bloccare l'event loop
                    queue = asyncio.Queue()
                    
                    def sync_stream():
                        try:
                            for chunk in result:
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
                    return
                
                else:
                    # Single value, yield it
                    yield result
                    return
                
            except exceptions as e:
                last_error = e
                if attempt < max_retries - 1 and ErrorUtils.is_retryable(e):
                    await ErrorUtils.wait_with_backoff_async(attempt)
                else:
                    break
        
        if last_error is not None:
            raise last_error
        raise RuntimeError(f"Async retry stream failed for {func.__name__}: no attempts were made (max_retries=0)")
