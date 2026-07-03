import os
from dataclasses import dataclass, field
try:
    from dotenv import load_dotenv
except ImportError:
    def load_dotenv(*args, **kwargs):
        return False

from utils.settings_store import DEFAULT_SETTINGS, get_settings
from utils.model_defaults import BUILTIN_PROVIDER_DEFINITIONS, ModelConfigurationError

load_dotenv()

@dataclass
class Paths:
    upload_folder: str = "app/uploads"
    chroma_persist_dir: str = "app/chroma_db"
    log_dir: str = "app/logs"
    data_dir: str = "app/data"
    settings_file: str = "app/data/settings.json"
    file_index: str = "app/data/files.json"
    max_upload_size_mb: int = 10  # Dimensione massima file upload in MB

@dataclass
class APIKeys:
    mistral_api_key: str = ""
    regolo_api_key: str = ""
    flask_secret_key: str = ""
    huggingface_token: str = ""

@dataclass
class ModelConfig:
    class VALID_RAG_MODELS:
        PROVIDERS = {
            provider_id: list(provider.get("models", []))
            for provider_id, provider in BUILTIN_PROVIDER_DEFINITIONS.items()
        }
        MISTRAL = list(PROVIDERS.get("mistral", []))
        REGOLO = list(PROVIDERS.get("regolo", []))
        @classmethod
        def all(cls):
            return [model for models in cls.PROVIDERS.values() for model in models]
        @classmethod
        def get_provider(cls, model: str) -> str:
            for provider_id, models in cls.PROVIDERS.items():
                if model in models:
                    return provider_id
            raise ValueError(
                f"Modello '{model}' non configurato. "
                f"Provider disponibili: {list(cls.PROVIDERS.keys())}"
            )

MISTRAL_MODELS = ModelConfig.VALID_RAG_MODELS.MISTRAL
REGOLO_MODELS = ModelConfig.VALID_RAG_MODELS.REGOLO

@dataclass
class RAGConfig:
    embedding_model: str = DEFAULT_SETTINGS["rag"]["embedding_model"]
    embedding_provider: str = DEFAULT_SETTINGS["rag"]["embedding_provider"]
    chunk_size: int = 1000
    chunk_overlap: int = 150
    query_k: int = 5
    temperature: float = 0.3
    default_model: str = ""
    enable_cache: bool = True
    cache_ttl: int = 3600
    use_internal_knowledge: bool = False
    reranker_enabled: bool = False
    reranker_model: str = DEFAULT_SETTINGS["rag"]["reranker_model"]
    reranker_top_n: int = 20
    reranker_source_diversity: bool = False
    reranker_threshold: float = 0.0

@dataclass
class VectorStoreConfig:
    backend: str = "chroma_persistent"

# Funzione di helper per caricare e validare variabili d'ambiente
from utils.validators import load_validated_env

# Module-level Config instance (not class)
_paths = Paths(
    upload_folder=os.getenv("UPLOAD_FOLDER", "app/uploads"),
    chroma_persist_dir=os.getenv("CHROMA_PERSIST_DIR", "app/chroma_db"),
    log_dir=os.getenv("LOG_DIR", "app/logs"),
    data_dir=os.getenv("RAG_DATA_DIR", "app/data"),
    settings_file=os.getenv("RAG_SETTINGS_FILE", "app/data/settings.json"),
    file_index=os.getenv("RAG_FILE_INDEX", "app/data/files.json"),
    max_upload_size_mb=load_validated_env(
        "MAX_UPLOAD_SIZE_MB",
        default=10,
        value_type=int,
        min_value=1,
        max_value=100
    )
)

_api_keys = APIKeys(
    mistral_api_key=os.getenv("MISTRAL_API_KEY", ""),
    regolo_api_key=os.getenv("REGOLO_API_KEY", ""),
    flask_secret_key=os.getenv("FLASK_SECRET_KEY", ""),
    huggingface_token=os.getenv("HUGGINGFACE_TOKEN", ""),
)

# Validazione configurazione RAG
_rag = RAGConfig(
    embedding_model=load_validated_env(
        "EMBEDDING_MODEL",
        default=DEFAULT_SETTINGS["rag"]["embedding_model"],
        value_type=str
    ),
    embedding_provider=load_validated_env(
        "EMBEDDING_PROVIDER",
        default=DEFAULT_SETTINGS["rag"]["embedding_provider"],
        value_type=str
    ),
    chunk_size=load_validated_env(
        "CHUNK_SIZE",
        default=1000,
        value_type=int,
        min_value=100,
        max_value=10000
    ),
    chunk_overlap=load_validated_env(
        "CHUNK_OVERLAP",
        default=150,
        value_type=int,
        min_value=0,
        max_value=500
    ),
    query_k=load_validated_env(
        "QUERY_K",
        default=5,
        value_type=int,
        min_value=1,
        max_value=50
    ),
    temperature=load_validated_env(
        "TEMPERATURE",
        default=0.3,
        value_type=float,
        min_value=0.0,
        max_value=1.0
    ),
    default_model=load_validated_env(
        "LLM_MODEL",
        default=DEFAULT_SETTINGS["rag"]["default_model"],
        value_type=str
    ),
    enable_cache=load_validated_env(
        "RAG_CACHE_ENABLED",
        default="true",
        value_type=bool
    ),
    cache_ttl=load_validated_env(
        "RAG_CACHE_TTL",
        default=3600,
        value_type=int,
        min_value=60,
        max_value=86400
    ),
    use_internal_knowledge=load_validated_env(
        "RAG_USE_INTERNAL_KNOWLEDGE",
        default="false",
        value_type=bool
    ),
)

_vector_store = VectorStoreConfig(
    backend=load_validated_env(
        "VECTOR_STORE_BACKEND",
        default="chroma_persistent",
        value_type=str
    )
)

# Backward compatibility as Config "class" with attributes
class _Config:
    paths = _paths
    api_keys = _api_keys
    rag = _rag
    vector_store = _vector_store
    @property
    def MISTRAL_API_KEY(self): return _api_keys.mistral_api_key
    @property
    def FLASK_SECRET_KEY(self): return _api_keys.flask_secret_key
    @property
    def UPLOAD_FOLDER(self): return _paths.upload_folder
    @property
    def CHROMA_PERSIST_DIR(self): return _paths.chroma_persist_dir
    @property
    def VECTOR_STORE_BACKEND(self): return _vector_store.backend
    @property
    def MODEL(self): return get_settings(_paths.settings_file)["rag"]["default_model"]
    @property
    def VALID_RAG_MODELS(self):
        from utils.providers.registry import ProviderRegistry
        try:
            return ProviderRegistry(get_settings(_paths.settings_file)).model_ids()
        except ModelConfigurationError:
            return []

# Export as both class-like and instance
Config = _Config()
