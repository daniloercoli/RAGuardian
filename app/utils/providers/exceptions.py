class ProviderError(Exception):
    """Base provider error"""
    pass

class AuthenticationError(ProviderError):
    """API key or authentication error"""
    pass

class RateLimitError(ProviderError):
    """Rate limit exceeded"""
    pass

class TimeoutError(ProviderError):
    """Request timeout"""
    pass

class ModelNotFoundError(ProviderError):
    """Model not available"""
    pass