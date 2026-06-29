from io import BytesIO

import pytest
from werkzeug.datastructures import FileStorage

from app.utils.validators import ValidationError, validate_file


def _upload(payload: bytes, filename: str, content_type: str | None = None) -> FileStorage:
    return FileStorage(stream=BytesIO(payload), filename=filename, content_type=content_type)


def test_validate_file_accepts_pdf_magic_bytes_and_rewinds_stream():
    upload = _upload(b"%PDF-1.4\n%content", "contract.pdf", "application/pdf")

    assert validate_file(upload, allowed_extensions=["pdf"]) is upload
    assert upload.stream.tell() == 0


def test_validate_file_rejects_pdf_extension_with_text_payload():
    upload = _upload(b"not really a pdf", "contract.pdf", "application/pdf")

    with pytest.raises(ValidationError) as exc:
        validate_file(upload, allowed_extensions=["pdf"])

    assert exc.value.field == "file"
    assert "estensione .pdf" in exc.value.message


def test_validate_file_rejects_image_extension_with_wrong_magic_bytes():
    upload = _upload(b"not really a png", "scan.png", "image/png")

    with pytest.raises(ValidationError) as exc:
        validate_file(upload, allowed_extensions=["png"])

    assert exc.value.field == "file"
    assert "estensione .png" in exc.value.message


def test_validate_file_rejects_binary_payload_for_text_extension():
    upload = _upload(b"%PDF-1.4\nbinary", "notes.txt", "text/plain")

    with pytest.raises(ValidationError) as exc:
        validate_file(upload, allowed_extensions=["txt"])

    assert exc.value.field == "file"
    assert "file binario" in exc.value.message


def test_validate_file_accepts_markdown_text_payload():
    upload = _upload(b"# Notes\n\nPlain markdown content", "notes.md", "text/markdown")

    assert validate_file(upload, allowed_extensions=["md"]) is upload
    assert upload.stream.tell() == 0
