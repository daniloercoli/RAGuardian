from app.utils.text_processor import process_text_file


def test_process_text_file_chunks_markdown(tmp_path):
    markdown = tmp_path / "notes.md"
    markdown.write_text("# Title\n\nSome useful content.\n\n- One\n- Two\n", encoding="utf-8")

    documents = process_text_file(str(markdown), settings_path=str(tmp_path / "settings.json"))

    assert len(documents) == 1
    assert "Some useful content" in documents[0].page_content
    assert documents[0].metadata["source_type"] == "markdown"
    assert documents[0].metadata["document_id"]
    assert documents[0].metadata["source_id"]


def test_process_text_file_rejects_binary_content(tmp_path):
    text_file = tmp_path / "bad.txt"
    text_file.write_bytes(b"hello\x00world")

    try:
        process_text_file(str(text_file), settings_path=str(tmp_path / "settings.json"))
    except ValueError as exc:
        assert "binario" in str(exc)
    else:
        raise AssertionError("Expected binary-looking text file to be rejected")
