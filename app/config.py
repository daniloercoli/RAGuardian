import os
from dataclasses import dataclass
try:
    from dotenv import load_dotenv
except ImportError:
    def load_dotenv(*args, **kwargs):
        return False

from utils.settings_store import get_settings
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
