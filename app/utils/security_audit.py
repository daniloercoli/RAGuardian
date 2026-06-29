import time
import json
from datetime import datetime
from typing import Optional
from .logging_config import APP_LOGGER as log
import os


class SecurityAuditLogger:
    """Logger per eventi di sicurezza"""
    
    def __init__(self):
        from config import Config
        self._enabled = Config.api_keys.flask_secret_key is not None
        self._log_file = os.path.join(Config.paths.log_dir, "security_audit.log")
    
    def log(self, event_type: str, severity: str, details: dict):
        """Log security event"""
        event = {
            "timestamp": datetime.utcnow().isoformat(),
            "event_type": event_type,
            "severity": severity,
            "details": details,
            "ip": details.get("ip", "unknown"),
            "user_agent": details.get("user_agent", "unknown")
        }
        
        try:
            with open(self._log_file, "a") as f:
                f.write(json.dumps(event) + "\n")
        except Exception as e:
            log.warning(f"Failed to write security audit log: {e}")
        
        if severity in ("high", "critical"):
            log.warning(f"SECURITY {event_type}: {json.dumps(details)}")
    
    def log_blocked_request(self, client_ip: str, user_agent: str, reason: str, 
                           input_type: str, input_value: str):
        """Log blocked malicious request"""
        self.log(
            event_type="blocked_request",
            severity="high",
            details={
                "ip": client_ip,
                "user_agent": user_agent,
                "reason": reason,
                "input_type": input_type,
                "input_value": self._sanitize_for_log(input_value)
            }
        )
    
    def log_rate_limit(self, client_ip: str, user_agent: str, attempts: int):
        """Log rate limit exceeded"""
        self.log(
            event_type="rate_limit_exceeded",
            severity="medium",
            details={
                "ip": client_ip,
                "user_agent": user_agent,
                "attempts": attempts
            }
        )
    
    def log_invalid_upload(self, client_ip: str, user_agent: str, 
                          filename: str, reason: str):
        """Log invalid file upload attempt"""
        self.log(
            event_type="invalid_upload",
            severity="high",
            details={
                "ip": client_ip,
                "user_agent": user_agent,
                "filename": self._sanitize_for_log(filename),
                "reason": reason
            }
        )
    
    def log_config_error(self, error_type: str, config_key: str, details: str):
        """Log configuration errors"""
        self.log(
            event_type="config_error",
            severity="critical",
            details={
                "error_type": error_type,
                "config_key": config_key,
                "details": details
            }
        )
    
    def _sanitize_for_log(self, value: str) -> str:
        """Sanitize value for logging"""
        if not isinstance(value, str):
            return str(value)
        
        # Truncate long strings
        if len(value) > 100:
            value = value[:50] + "..." + value[-50:]
        
        # Remove newlines
        value = value.replace("\n", "\\n").replace("\r", "\\r")
        
        return value


_audit_logger = None


def get_audit_logger() -> SecurityAuditLogger:
    """Get singleton audit logger"""
    global _audit_logger
    if _audit_logger is None:
        _audit_logger = SecurityAuditLogger()
    return _audit_logger
