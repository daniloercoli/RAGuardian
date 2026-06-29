"""
Modulo per validazione input nel progetto RAG
Contiene funzioni di validazione base e specifiche per il dominio RAG
"""
import re
from typing import Optional, List, Any
from werkzeug.datastructures import FileStorage


# Import per audit log (opzionale, non rompe se non disponibile)
try:
    from .security_audit import get_audit_logger
    AUDIT_ENABLED = True
except (ImportError, ModuleNotFoundError):
    AUDIT_ENABLED = False
    get_audit_logger = None


class ValidationError(Exception):
    """Eccezione personalizzata per errori di validazione"""
    def __init__(self, message: str, field: Optional[str] = None, code: Optional[str] = None):
        self.message = message
        self.field = field
        self.code = code or "validation_error"
        super().__init__(self.message)
    
    def to_dict(self) -> dict:
        """Converte l'errore in dizionario per API response"""
        result = {"error": self.message, "status": self.code}
        if self.field:
            result["field"] = self.field
        return result


FILE_SIGNATURE_READ_BYTES = 4096
TEXT_FILE_EXTENSIONS = {"txt", "md", "csv"}


def _is_pdf(header: bytes) -> bool:
    return header.startswith(b"%PDF-")


def _is_png(header: bytes) -> bool:
    return header.startswith(b"\x89PNG\r\n\x1a\n")


def _is_jpeg(header: bytes) -> bool:
    return header.startswith(b"\xff\xd8\xff")


def _is_gif(header: bytes) -> bool:
    return header.startswith((b"GIF87a", b"GIF89a"))


def _is_webp(header: bytes) -> bool:
    return len(header) >= 12 and header[:4] == b"RIFF" and header[8:12] == b"WEBP"


def _is_bmp(header: bytes) -> bool:
    return header.startswith(b"BM")


def _is_tiff(header: bytes) -> bool:
    return header.startswith((b"II*\x00", b"MM\x00*"))


def _is_mp3(header: bytes) -> bool:
    return header.startswith(b"ID3") or (len(header) >= 2 and header[0] == 0xFF and (header[1] & 0xE0) == 0xE0)


def _is_wav(header: bytes) -> bool:
    return len(header) >= 12 and header[:4] == b"RIFF" and header[8:12] == b"WAVE"


def _is_m4a(header: bytes) -> bool:
    return len(header) >= 12 and header[4:8] == b"ftyp"


def _is_webm(header: bytes) -> bool:
    return header.startswith(b"\x1a\x45\xdf\xa3")


def _is_ogg(header: bytes) -> bool:
    return header.startswith(b"OggS")


def _is_flac(header: bytes) -> bool:
    return header.startswith(b"fLaC")


FILE_SIGNATURE_CHECKS = {
    "pdf": _is_pdf,
    "png": _is_png,
    "jpg": _is_jpeg,
    "jpeg": _is_jpeg,
    "webp": _is_webp,
    "gif": _is_gif,
    "bmp": _is_bmp,
    "tif": _is_tiff,
    "tiff": _is_tiff,
    "mp3": _is_mp3,
    "wav": _is_wav,
    "m4a": _is_m4a,
    "webm": _is_webm,
    "ogg": _is_ogg,
    "flac": _is_flac,
}


def _read_file_header(file: FileStorage) -> bytes:
    file.seek(0)
    header = file.read(FILE_SIGNATURE_READ_BYTES) or b""
    file.seek(0)
    if isinstance(header, str):
        return header.encode("utf-8", errors="ignore")
    return header


def _looks_like_binary_payload(header: bytes) -> bool:
    if not header:
        return False
    if any(check(header) for check in FILE_SIGNATURE_CHECKS.values()):
        return True
    if b"\x00" in header:
        return True
    control_count = sum(1 for byte in header if byte < 9 or 13 < byte < 32)
    return control_count / max(1, len(header)) > 0.05


def _log_invalid_upload(filename: str, reason: str) -> None:
    if not AUDIT_ENABLED:
        return
    try:
        get_audit_logger().log_invalid_upload(
            client_ip="unknown",
            user_agent="unknown",
            filename=filename,
            reason=reason,
        )
    except Exception:
        pass


def _validate_file_signature(file: FileStorage, extension: str, field_name: str) -> None:
    header = _read_file_header(file)
    extension = extension.lower()
    signature_check = FILE_SIGNATURE_CHECKS.get(extension)

    if signature_check and not signature_check(header):
        _log_invalid_upload(file.filename, f"Magic bytes mismatch for extension: {extension}")
        raise ValidationError(
            f"{field_name} contenuto non corrisponde all'estensione .{extension}",
            field_name,
        )

    if extension in TEXT_FILE_EXTENSIONS and _looks_like_binary_payload(header):
        _log_invalid_upload(file.filename, f"Binary content uploaded as text: {extension}")
        raise ValidationError(
            f"{field_name} sembra un file binario, non un documento testuale",
            field_name,
        )


# ============================================================================
# VALIDATORI BASE
# ============================================================================

def validate_string(
    value: Any,
    field_name: str,
    min_length: int = 0,
    max_length: int = 10000,
    pattern: Optional[str] = None,
    required: bool = True,
    strip: bool = True
) -> str:
    """
    Valida una stringa
    
    Args:
        value: Il valore da validare
        field_name: Nome del campo per messaggi di errore
        min_length: Lunghezza minima (0 = nessun limite)
        max_length: Lunghezza massima
        pattern: Regex pattern che il valore deve rispettare
        required: Se True, il campo e' obbligatorio
        strip: Se True, rimuove spazi prima/dopo
        
    Returns:
        Stringa validata
        
    Raises:
        ValidationError: Se la validazione fallisce
    """
    if required and (value is None or value == ""):
        if AUDIT_ENABLED:
            try:
                get_audit_logger().log_blocked_request(
                    client_ip="unknown",
                    user_agent="unknown",
                    reason=f"Required field empty: {field_name}",
                    input_type="validation",
                    input_value=str(value)
                )
            except:
                pass
        raise ValidationError(f"{field_name} e' obbligatorio", field_name)
    
    if value is None:
        return None
    
    if not isinstance(value, str):
        if AUDIT_ENABLED:
            try:
                get_audit_logger().log_blocked_request(
                    client_ip="unknown",
                    user_agent="unknown",
                    reason=f"Invalid type for string field: {field_name}",
                    input_type="validation",
                    input_value=str(value)
                )
            except:
                pass
        raise ValidationError(f"{field_name} deve essere una stringa", field_name)
    
    if strip:
        value = value.strip()
    
    if len(value) < min_length:
        raise ValidationError(
            f"{field_name} deve avere almeno {min_length} caratteri", 
            field_name
        )
    
    if len(value) > max_length:
        raise ValidationError(
            f"{field_name} deve avere al massimo {max_length} caratteri", 
            field_name
        )
    
    if pattern and not re.match(pattern, value):
        if AUDIT_ENABLED:
            try:
                get_audit_logger().log_blocked_request(
                    client_ip="unknown",
                    user_agent="unknown",
                    reason=f"Pattern mismatch: {field_name}",
                    input_type="validation",
                    input_value=value
                )
            except:
                pass
        raise ValidationError(f"{field_name} non ha un formato valido", field_name)
    
    return value


def validate_integer(
    value: Any,
    field_name: str,
    min_value: Optional[int] = None,
    max_value: Optional[int] = None,
    required: bool = True
) -> Optional[int]:
    """
    Valida un numero intero
    
    Args:
        value: Il valore da validare
        field_name: Nome del campo per messaggi di errore
        min_value: Valore minimo consentito (None = nessun limite)
        max_value: Valore massimo consentito (None = nessun limite)
        required: Se True, il campo e' obbligatorio
        
    Returns:
        Intero validato o None se non richiesto e value e' None
        
    Raises:
        ValidationError: Se la validazione fallisce
    """
    if required and value is None:
        raise ValidationError(f"{field_name} e' obbligatorio", field_name)
    
    if value is None and not required:
        return None
    
    try:
        int_value = int(value)
    except (TypeError, ValueError):
        raise ValidationError(f"{field_name} deve essere un numero intero", field_name)
    
    if min_value is not None and int_value < min_value:
        raise ValidationError(f"{field_name} deve essere >= {min_value}", field_name)
    
    if max_value is not None and int_value > max_value:
        raise ValidationError(f"{field_name} deve essere <= {max_value}", field_name)
    
    return int_value


def validate_float(
    value: Any,
    field_name: str,
    min_value: Optional[float] = None,
    max_value: Optional[float] = None,
    required: bool = True
) -> Optional[float]:
    """
    Valida un numero decimale
    
    Args:
        value: Il valore da validare
        field_name: Nome del campo per messaggi di errore
        min_value: Valore minimo consentito (None = nessun limite)
        max_value: Valore massimo consentito (None = nessun limite)
        required: Se True, il campo e' obbligatorio
        
    Returns:
        Float validato o None se non richiesto e value e' None
        
    Raises:
        ValidationError: Se la validazione fallisce
    """
    if required and value is None:
        raise ValidationError(f"{field_name} e' obbligatorio", field_name)
    
    if value is None and not required:
        return None
    
    try:
        float_value = float(value)
    except (TypeError, ValueError):
        raise ValidationError(f"{field_name} deve essere un numero decimale", field_name)
    
    if min_value is not None and float_value < min_value:
        raise ValidationError(f"{field_name} deve essere >= {min_value}", field_name)
    
    if max_value is not None and float_value > max_value:
        raise ValidationError(f"{field_name} deve essere <= {max_value}", field_name)
    
    return float_value


def validate_boolean(
    value: Any,
    field_name: str,
    required: bool = True
) -> bool:
    """
    Valida un booleano (accetta: True/False, "true"/"false", "yes"/"no", 1/0)
    
    Args:
        value: Il valore da validare
        field_name: Nome del campo per messaggi di errore
        required: Se True, il campo e' obbligatorio
        
    Returns:
        Booleano validato
        
    Raises:
        ValidationError: Se la validazione fallisce
    """
    if required and value is None:
        raise ValidationError(f"{field_name} e' obbligatorio", field_name)
    
    if isinstance(value, bool):
        return value
    
    if isinstance(value, int):
        return bool(value)
    
    if isinstance(value, str):
        truthy_values = ["true", "yes", "1", "y", "on", "enabled"]
        falsy_values = ["false", "no", "0", "n", "off", "disabled"]
        
        lower_value = value.lower().strip()
        if lower_value in truthy_values:
            return True
        elif lower_value in falsy_values:
            return False
    
    raise ValidationError(
        f"{field_name} deve essere un booleano valido (true/false, yes/no, 1/0)", 
        field_name
    )


def validate_enum(
    value: Any,
    field_name: str,
    allowed_values: List[str],
    required: bool = True,
    case_sensitive: bool = True
) -> Optional[str]:
    """
    Valida che un valore sia in una lista consentita
    
    Args:
        value: Il valore da validare
        field_name: Nome del campo per messaggi di errore
        allowed_values: Lista di valori consentiti
        required: Se True, il campo e' obbligatorio
        case_sensitive: Se False, il confronto e' case-insensitive
        
    Returns:
        Valore validato (stringa) o None se non richiesto e value e' None
        
    Raises:
        ValidationError: Se la validazione fallisce
    """
    if required and value is None:
        raise ValidationError(f"{field_name} e' obbligatorio", field_name)
    
    if value is None and not required:
        return None
    
    if not case_sensitive:
        str_value = str(value).lower()
        allowed_lower = [v.lower() for v in allowed_values]
        if str_value not in allowed_lower:
            raise ValidationError(
                f"{field_name} deve essere uno di: {', '.join(allowed_values)}", 
                field_name
            )
        # Restituisci il valore originale con casing corretto
        idx = allowed_lower.index(str_value)
        return allowed_values[idx]
    else:
        if str(value) not in allowed_values:
            raise ValidationError(
                f"{field_name} deve essere uno di: {', '.join(allowed_values)}", 
                field_name
            )
        return str(value)


def validate_list(
    value: Any,
    field_name: str,
    min_items: int = 0,
    max_items: Optional[int] = None,
    item_validator: Optional[callable] = None,
    required: bool = True
) -> list:
    """
    Valida una lista
    
    Args:
        value: Il valore da validare
        field_name: Nome del campo per messaggi di errore
        min_items: Numero minimo di elementi
        max_items: Numero massimo di elementi (None = nessun limite)
        item_validator: Funzione per validare ogni elemento (opzionale)
        required: Se True, il campo e' obbligatorio
        
    Returns:
        Lista validata
        
    Raises:
        ValidationError: Se la validazione fallisce
    """
    if required and value is None:
        raise ValidationError(f"{field_name} e' obbligatorio", field_name)
    
    if value is None and not required:
        return []
    
    if not isinstance(value, list):
        raise ValidationError(f"{field_name} deve essere una lista", field_name)
    
    if len(value) < min_items:
        raise ValidationError(
            f"{field_name} deve contenere almeno {min_items} elementi", 
            field_name
        )
    
    if max_items is not None and len(value) > max_items:
        raise ValidationError(
            f"{field_name} deve contenere al massimo {max_items} elementi", 
            field_name
        )
    
    if item_validator:
        validated_list = []
        for i, item in enumerate(value):
            try:
                validated_list.append(item_validator(item, f"{field_name}[{i}]"))
            except ValidationError as e:
                raise ValidationError(str(e), e.field)
        return validated_list
    
    return value


# ============================================================================
# VALIDATORI SPECIFICI PER RAG
# ============================================================================

def validate_query(query: Any) -> str:
    """
    Valida una query RAG

    - Deve essere una stringa
    - Minimo 3 caratteri
    - Massimo 2000 caratteri
    - Rifiuta solo payload binari/nul (nessun codice o SQL injection)
    """
    if not isinstance(query, str):
        raise ValidationError("query deve essere una stringa", "query")

    query = query.strip()

    if len(query) < 3:
        raise ValidationError("query deve avere almeno 3 caratteri", "query")

    if len(query) > 2000:
        raise ValidationError("query deve avere al massimo 2000 caratteri", "query")

    if "\x00" in query:
        raise ValidationError("query contiene caratteri non validi", "query")

    return query


def validate_conversation_id(value: Any, required: bool = False) -> Optional[str]:
    if value is None or value == "":
        if required:
            raise ValidationError("conversation_id e' obbligatorio", "conversation_id")
        return None

    if not isinstance(value, str):
        raise ValidationError("conversation_id deve essere una stringa", "conversation_id")

    value = value.strip()
    if required and not value:
        raise ValidationError("conversation_id e' obbligatorio", "conversation_id")

    if len(value) < 8 or len(value) > 80:
        raise ValidationError("conversation_id deve avere tra 8 e 80 caratteri", "conversation_id")

    if not re.match(r"^[A-Za-z0-9][A-Za-z0-9_-]*$", value):
        raise ValidationError("conversation_id non ha un formato valido", "conversation_id")

    return value


def validate_model(
    model: Any,
    allowed_models: List[str],
    required: bool = False
) -> Optional[str]:
    """
    Valida un modello LLM
    """
    return validate_enum(
        value=model,
        field_name="model",
        allowed_values=allowed_models,
        required=required,
        case_sensitive=True
    )


def validate_file(
    file: FileStorage,
    field_name: str = "file",
    allowed_extensions: Optional[List[str]] = None,
    max_size_mb: int = 10
) -> FileStorage:
    """
    Valida un file caricato
    
    Args:
        file: Oggetto FileStorage da Flask
        field_name: Nome del campo per messaggi di errore
        allowed_extensions: Lista di estensioni consentite (es: ["pdf"])
        max_size_mb: Dimensione massima in MB
        
    Returns:
        Il file validato (FileStorage)
        
    Raises:
        ValidationError: Se la validazione fallisce
    """
    if allowed_extensions is None:
        allowed_extensions = ["pdf"]
    
    # Controlla presence
    if not file or not hasattr(file, 'filename') or not file.filename:
        raise ValidationError(f"{field_name} e' obbligatorio", field_name)
    
    # Controlla filename
    if not file.filename:
        raise ValidationError(f"{field_name} deve avere un nome", field_name)
    
    # Controlla estensione
    if '.' not in file.filename:
        raise ValidationError(f"{field_name} deve avere un'estensione", field_name)
    
    extension = file.filename.rsplit('.', 1)[1].lower()
    if extension not in [ext.lower() for ext in allowed_extensions]:
        _log_invalid_upload(file.filename, f"Invalid extension: {extension}")
        raise ValidationError(
            f"{field_name} deve essere un file {', '.join(allowed_extensions)}", 
            field_name
        )
    
    # Controlla dimensione
    file.seek(0, 2)  # Va alla fine
    file_size = file.tell()
    file.seek(0)  # Torna all'inizio
    max_size_bytes = max_size_mb * 1024 * 1024
    
    if file_size > max_size_bytes:
        size_mb = file_size / 1024 / 1024
        _log_invalid_upload(file.filename, f"File too large: {size_mb:.1f}MB > {max_size_mb}MB")
        raise ValidationError(
            f"{field_name} deve essere <= {max_size_mb}MB (ricevuti {size_mb:.1f}MB)", 
            field_name
        )
    
    # Controlla MIME type (se disponibile)
    if hasattr(file, 'content_type') and file.content_type:
        allowed_mimes = {
            "pdf": ["application/pdf", "application/x-pdf"],
            "txt": ["text/plain", "application/octet-stream"],
            "md": ["text/markdown", "text/plain", "text/x-markdown", "application/octet-stream"],
            "docx": ["application/vnd.openxmlformats-officedocument.wordprocessingml.document"],
            "png": ["image/png"],
            "jpg": ["image/jpeg"],
            "jpeg": ["image/jpeg"],
            "webp": ["image/webp"],
            "gif": ["image/gif"],
            "bmp": ["image/bmp", "image/x-ms-bmp"],
            "tif": ["image/tiff"],
            "tiff": ["image/tiff"],
            "mp3": ["audio/mpeg", "audio/mp3", "application/octet-stream"],
            "wav": ["audio/wav", "audio/x-wav", "audio/wave", "application/octet-stream"],
            "m4a": ["audio/mp4", "audio/x-m4a", "video/mp4", "application/octet-stream"],
            "webm": ["audio/webm", "video/webm", "application/octet-stream"],
            "ogg": ["audio/ogg", "application/ogg", "application/octet-stream"],
            "flac": ["audio/flac", "audio/x-flac", "application/octet-stream"],
        }
        
        if extension.lower() in allowed_mimes:
            if file.content_type not in allowed_mimes[extension.lower()]:
                raise ValidationError(
                    f"{field_name} tipo MIME non valido: {file.content_type}. "
                    f"Atteso uno di: {', '.join(allowed_mimes[extension.lower()])}", 
                    field_name
                )
    
    _validate_file_signature(file, extension, field_name)

    return file


def validate_temperature(temperature: Any, required: bool = False) -> Optional[float]:
    """
    Valida la temperatura (0.0 - 1.0)
    """
    return validate_float(
        value=temperature,
        field_name="temperature",
        min_value=0.0,
        max_value=1.0,
        required=required
    )


def validate_k(k: Any, max_k: int = 50, required: bool = False) -> Optional[int]:
    """
    Valida il parametro k (numero di risultati)
    """
    return validate_integer(
        value=k,
        field_name="k",
        min_value=1,
        max_value=max_k,
        required=required
    )


# ============================================================================
# VALIDATORI PER CONFIGURAZIONE
# ============================================================================

def validate_config_value(
    value: Any,
    field_name: str,
    value_type: type,
    min_value: Optional[int] = None,
    max_value: Optional[int] = None,
    allowed_values: Optional[List[Any]] = None
) -> Any:
    """
    Valida un valore di configurazione
    
    Args:
        value: Il valore da validare
        field_name: Nome della variabile di configurazione
        value_type: Tipo atteso (str, int, float, bool)
        min_value: Valore minimo (per numeri)
        max_value: Valore massimo (per numeri)
        allowed_values: Valori consentiti (opzionale)
        
    Returns:
        Valore validato
        
    Raises:
        ValidationError: Se la validazione fallisce
    """
    if value is None:
        return None
    
    # Controlla tipo
    if not isinstance(value, value_type):
        try:
            # Prova a convertire
            if value_type == int:
                value = int(value)
            elif value_type == float:
                value = float(value)
            elif value_type == bool:
                value = validate_boolean(value, field_name)
            else:
                raise ValidationError(
                    f"{field_name} deve essere di tipo {value_type.__name__}", 
                    field_name
                )
        except (ValueError, TypeError, ValidationError):
            if AUDIT_ENABLED:
                try:
                    get_audit_logger().log_config_error(
                        error_type="invalid_value",
                        config_key=field_name,
                        details=f"Expected {value_type.__name__}, got {type(value).__name__}"
                    )
                except:
                    pass
            raise ValidationError(
                f"{field_name} deve essere di tipo {value_type.__name__}", 
                field_name
            )
    
    # Controlla range per numeri
    if isinstance(value, (int, float)):
        if min_value is not None and value < min_value:
            if AUDIT_ENABLED:
                try:
                    get_audit_logger().log_config_error(
                        error_type="out_of_range",
                        config_key=field_name,
                        details=f"Value {value} < {min_value}"
                    )
                except:
                    pass
            raise ValidationError(f"{field_name} deve essere >= {min_value}", field_name)
        if max_value is not None and value > max_value:
            if AUDIT_ENABLED:
                try:
                    get_audit_logger().log_config_error(
                        error_type="out_of_range",
                        config_key=field_name,
                        details=f"Value {value} > {max_value}"
                    )
                except:
                    pass
            raise ValidationError(f"{field_name} deve essere <= {max_value}", field_name)
    
    # Controlla valori consentiti
    if allowed_values and value not in allowed_values:
        if AUDIT_ENABLED:
            try:
                get_audit_logger().log_config_error(
                    error_type="invalid_choice",
                    config_key=field_name,
                    details=f"Value {value} not in allowed values"
                )
            except:
                pass
        raise ValidationError(
            f"{field_name} deve essere uno di: {', '.join(str(v) for v in allowed_values)}", 
            field_name
        )
    
    return value


def load_validated_env(
    var_name: str,
    default: Any = None,
    value_type: type = str,
    min_value: Optional[int] = None,
    max_value: Optional[int] = None,
    allowed_values: Optional[List[Any]] = None
) -> Any:
    """
    Carica una variabile d'ambiente con validazione
    
    Args:
        var_name: Nome della variabile d'ambiente
        default: Valore predefinito se non impostata
        value_type: Tipo atteso
        min_value: Valore minimo (per numeri)
        max_value: Valore massimo (per numeri)
        allowed_values: Valori consentiti
        
    Returns:
        Valore validato
        
    Raises:
        ValueError: Se la validazione fallisce
    """
    import os
    value = os.getenv(var_name, default)
    
    if value is None:
        return None
    
    try:
        return validate_config_value(
            value=value,
            field_name=var_name,
            value_type=value_type,
            min_value=min_value,
            max_value=max_value,
            allowed_values=allowed_values
        )
    except ValidationError as e:
        raise ValueError(f"Configurazione non valida per {var_name}: {e.message}")
