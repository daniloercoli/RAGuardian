import functools
import time
from .logging_config import setup_logger

def log_execution(logger=None):
    """Decoratore per loggare esecuzione funzione"""
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            log = logger or setup_logger(func.__module__)
            
            log.info(f"START {func.__name__}")
            start = time.time()
            
            try:
                result = func(*args, **kwargs)
                elapsed = (time.time() - start) * 1000
                log.info(f"OK {func.__name__} ({elapsed:.1f}ms)")
                return result
            except Exception as e:
                elapsed = (time.time() - start) * 1000
                log.error(f"FAIL {func.__name__} ({elapsed:.1f}ms): {e}")
                raise
        return wrapper
    return decorator

def log_input_output(logger=None):
    """Decoratore che logga input e output"""
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            log = logger or setup_logger(func.__module__)
            
            args_str = ", ".join(repr(a)[:100] for a in args[:3])
            log.info(f">> {func.__name__}({args_str})")
            
            result = func(*args, **kwargs)
            
            if isinstance(result, str):
                log.info(f"<< {func.__name__}: {result[:100]}...")
            else:
                log.info(f"<< {func.__name__}: {type(result).__name__}")
            
            return result
        return wrapper
    return decorator

def retry(max_retries=3, backoff_factor=1.0, exceptions=(Exception,)):
    """Decoratore per retry con backoff"""
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            last_error = None
            for attempt in range(max_retries):
                try:
                    return func(*args, **kwargs)
                except exceptions as e:
                    last_error = e
                    if attempt < max_retries - 1:
                        wait = backoff_factor * (2 ** attempt)
                        from .logging_config import APP_LOGGER
                        APP_LOGGER.warning(f"Retry {func.__name__} dopo {wait}s (tentativo {attempt+1}/{max_retries})")
                        time.sleep(wait)
            raise last_error
        return wrapper
    return decorator
