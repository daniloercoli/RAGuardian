import hashlib
import os


def document_hash(file_path: str) -> str:
    digest = hashlib.sha256()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()[:16]


def source_hash(source: str) -> str:
    normalized = os.path.normpath(str(source)).replace(os.sep, "/")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:12]
