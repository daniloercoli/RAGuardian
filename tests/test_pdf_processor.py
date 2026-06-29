from app.utils.document_identity import document_hash, source_hash


def test_document_hash_is_stable_for_file_content(tmp_path):
    pdf = tmp_path / "demo.pdf"
    pdf.write_bytes(b"same-content")

    first = document_hash(str(pdf))
    second = document_hash(str(pdf))

    assert first == second
    assert len(first) == 16


def test_source_hash_distinguishes_paths_for_same_content(tmp_path):
    first = tmp_path / "one.pdf"
    second = tmp_path / "two.pdf"
    first.write_bytes(b"same-content")
    second.write_bytes(b"same-content")

    assert document_hash(str(first)) == document_hash(str(second))
    assert source_hash(str(first)) != source_hash(str(second))
    assert len(source_hash(str(first))) == 12
