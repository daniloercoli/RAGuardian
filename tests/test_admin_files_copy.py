from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_admin_files_explains_file_index_metadata():
    template = (ROOT / "app/templates/admin_files.html").read_text(encoding="utf-8")

    assert "Indexed Files" in template
    assert "Knowledge Base Status" in template
    assert "Ready: the knowledge base contains indexed documents" in template
    assert "This is not the RAG vector index" in template
    assert "Vectors and queryable chunks are in ChromaDB" in template
    assert "administrative metadata" in template
    assert "admin_delete_file" in template
    assert "Remove" in template
    assert "Download" in template
    assert "status-pill" in template
    assert "startRebuildButton" in template
    assert "manualRebuildButton" in template
    assert "Force Re-index All Files" in template
    assert "Manual Maintenance" in template
    assert "rebuildModal" in template
    assert "Rebuild Index" in template
    assert "source_type" in template


def test_admin_files_supports_multi_file_upload_interface():
    template = (ROOT / "app/templates/admin_files.html").read_text(encoding="utf-8")
    script = (ROOT / "app/static/admin_files.js").read_text(encoding="utf-8")

    assert 'type="file"' in template
    assert ".txt,.md,.csv" in template
    assert "ragFolderInput" in template
    assert "webkitdirectory" in template
    assert "uploadProgressPanel" in template
    assert "uploadErrorPanel" in template
    assert "cancelUploadButton" in template
    assert "admin_files.js" in template
    assert '".txt", ".md", ".csv"' in script
    assert "webkitRelativePath" in script
    assert 'formData.append("relative_path"' in script
    assert "/api/v1/audio" in script
    assert "/api/v1/files" in script
    assert "isAudioFile" in script
    assert "for (let index = 0; index < files.length; index += 1)" in script
    assert "renderErrors(errors)" in script


def test_admin_files_supports_index_rebuild_progress_modal():
    template = (ROOT / "app/templates/admin_files.html").read_text(encoding="utf-8")
    script = (ROOT / "app/static/admin_files.js").read_text(encoding="utf-8")

    assert "startRebuild" in script
    assert "window.confirm" in script
    assert "data-rebuild-trigger" in template
    assert "querySelectorAll(\"[data-rebuild-trigger]\")" in script
    assert "setRebuildButtonsDisabled" in script
    assert "pollRebuild" in script
    assert 'fetch(`/admin/files/rebuild/${encodeURIComponent(jobId)}`' in script
    assert "rebuildCurrentFile" in script
    assert "renderRebuildErrors" in script
