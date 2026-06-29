"""
Middleware per validazione automatica delle richieste Flask
Fornisce decorator per validare input in modo dichiarativo
"""
from flask import request, jsonify
from functools import wraps
from typing import Optional, List, Callable, Any
from ..utils.validators import ValidationError, validate_query, validate_model, validate_file, validate_boolean, validate_integer, validate_float
from config import Config


def validate_json(schema: Optional[dict] = None, validator_func: Optional[Callable] = None):
    """
    Decorator per validare JSON request body
    
    Args:
        schema: Dizionario con regole di validazione (opzionale)
        validator_func: Funzione personalizzata per validazione (opzionale)
        
    Usage:
        @app.route("/ask", methods=["POST"])
        @validate_json(schema={"query": {"type": "string", "min_length": 3}})
        def ask():
            # request.validated_data contiene i dati validati
            pass
    """
    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            # Controlla Content-Type
            if not request.is_json:
                return jsonify({
                    "error": "Content-Type deve essere application/json",
                    "status": "validation_error"
                }), 415
            
            # Parsing JSON
            try:
                data = request.get_json()
                if data is None:
                    return jsonify({
                        "error": "Body vuoto",
                        "status": "validation_error"
                    }), 400
            except Exception as e:
                return jsonify({
                    "error": f"JSON non valido: {str(e)}",
                    "status": "validation_error"
                }), 400
            
            # Validazione con funzione personalizzata
            if validator_func:
                try:
                    validated_data = validator_func(data)
                    request.validated_data = validated_data
                except ValidationError as e:
                    return jsonify(e.to_dict()), 400
                except Exception as e:
                    return jsonify({
                        "error": str(e),
                        "status": "validation_error"
                    }), 400
                return f(*args, **kwargs)
            
            # Validazione con schema (semplicistica)
            if schema:
                validated_data = {}
                for field, rules in schema.items():
                    if field not in data and rules.get('required', False):
                        return jsonify({
                            "error": f"{field} e' obbligatorio",
                            "field": field,
                            "status": "validation_error"
                        }), 400
                    
                    value = data.get(field)
                    field_type = rules.get('type', str)
                    
                    try:
                        if field_type == 'string':
                            validated_data[field] = validate_string(
                                value=value,
                                field_name=field,
                                min_length=rules.get('min_length', 0),
                                max_length=rules.get('max_length', 10000),
                                pattern=rules.get('pattern'),
                                required=rules.get('required', False)
                            )
                        elif field_type == 'integer':
                            validated_data[field] = validate_integer(
                                value=value,
                                field_name=field,
                                min_value=rules.get('min_value'),
                                max_value=rules.get('max_value'),
                                required=rules.get('required', False)
                            )
                        elif field_type == 'float':
                            validated_data[field] = validate_float(
                                value=value,
                                field_name=field,
                                min_value=rules.get('min_value'),
                                max_value=rules.get('max_value'),
                                required=rules.get('required', False)
                            )
                        elif field_type == 'boolean':
                            validated_data[field] = validate_boolean(
                                value=value,
                                field_name=field,
                                required=rules.get('required', False)
                            )
                        elif field_type == 'enum':
                            validated_data[field] = validate_enum(
                                value=value,
                                field_name=field,
                                allowed_values=rules.get('allowed_values', []),
                                required=rules.get('required', False)
                            )
                        else:
                            validated_data[field] = value
                    except ValidationError as e:
                        return jsonify(e.to_dict()), 400
                
                request.validated_data = validated_data
                return f(*args, **kwargs)
            
            # Nessuna validazione specificata, usa dati grezzi
            request.validated_data = data
            return f(*args, **kwargs)
        
        return wrapper
    return decorator


def validate_file_upload(
    field_name: str = "file",
    allowed_extensions: Optional[List[str]] = None,
    max_size_mb: int = 10
):
    """
    Decorator per validare upload file
    
    Args:
        field_name: Nome del campo file nella request
        allowed_extensions: Lista di estensioni consentite
        max_size_mb: Dimensione massima in MB
        
    Usage:
        @app.route("/upload", methods=["POST"])
        @validate_file_upload(field_name="file", allowed_extensions=["pdf"], max_size_mb=10)
        def upload():
            # request.validated_file contiene il file validato
            file = request.validated_file
            # ...
    """
    if allowed_extensions is None:
        allowed_extensions = ["pdf"]
    
    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            # Controlla presence file
            if field_name not in request.files:
                return jsonify({
                    "error": f"{field_name} e' obbligatorio",
                    "field": field_name,
                    "status": "validation_error"
                }), 400
            
            file = request.files[field_name]
            
            # Validazione file
            try:
                from ..utils.validators import validate_file
                validated_file = validate_file(
                    file=file,
                    field_name=field_name,
                    allowed_extensions=allowed_extensions,
                    max_size_mb=max_size_mb
                )
                request.validated_file = validated_file
            except ValidationError as e:
                return jsonify(e.to_dict()), 400
            
            return f(*args, **kwargs)
        
        return wrapper
    return decorator


def validate_rag_request():
    """
    Decorator specifico per validare richieste RAG
    Valida: query, model, stream, temperature, k
    """
    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            # Controlla Content-Type
            if not request.is_json:
                return jsonify({
                    "error": "Content-Type deve essere application/json",
                    "status": "validation_error"
                }), 415
            
            # Parsing JSON
            try:
                data = request.get_json()
                if data is None:
                    return jsonify({
                        "error": "Body vuoto",
                        "status": "validation_error"
                    }), 400
            except Exception as e:
                return jsonify({
                    "error": f"JSON non valido: {str(e)}",
                    "status": "validation_error"
                }), 400
            
            # Validazione campi
            try:
                validated_data = {}
                
                # Query (obbligatorio)
                validated_data['query'] = validate_query(data.get('query'))
                
                # Model (opzionale)
                validated_data['model'] = validate_model(
                    data.get('model'),
                    Config.VALID_RAG_MODELS
                )
                
                # Stream (opzionale, default: False)
                validated_data['stream'] = validate_boolean(
                    data.get('stream', False)
                )
                
                # Temperature (opzionale)
                validated_data['temperature'] = validate_float(
                    data.get('temperature'),
                    field_name="temperature",
                    min_value=0.0,
                    max_value=1.0,
                    required=False
                )
                
                # K (opzionale)
                validated_data['k'] = validate_integer(
                    data.get('k'),
                    field_name="k",
                    min_value=1,
                    max_value=50,
                    required=False
                )
                
                request.validated_data = validated_data
                return f(*args, **kwargs)
                
            except ValidationError as e:
                return jsonify(e.to_dict()), 400
        
        return wrapper
    return decorator


# ============================================================================
# ESEMPI DI UTILIZZO
# ============================================================================

# Esempio 1: Decorator generico con schema
# @app.route("/api/ask", methods=["POST"])
# @validate_json(schema={
#     "query": {"type": "string", "min_length": 3, "max_length": 2000, "required": True},
#     "model": {"type": "string", "required": False},
#     "stream": {"type": "boolean", "required": False}
# })
# def ask():
#     data = request.validated_data
#     # ...

# Esempio 2: Decorator specifico RAG
# @app.route("/api/ask", methods=["POST"])
# @validate_rag_request()
# def ask():
#     data = request.validated_data
#     # data['query'], data['model'], data['stream'] sono gia' validati

# Esempio 3: Upload file
# @app.route("/api/upload", methods=["POST"])
# @validate_file_upload(field_name="file", allowed_extensions=["pdf"], max_size_mb=10)
# def upload():
#     file = request.validated_file
#     # file e' gia' validato
