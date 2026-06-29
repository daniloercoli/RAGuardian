from concurrent.futures import ThreadPoolExecutor

from app.utils.file_index import FileIndex


def test_file_index_get_and_remove_entry(tmp_path):
    index = FileIndex(str(tmp_path / "files.json"))
    index.record("demo.pdf", "app/uploads/demo.pdf", 2, status="indexed")

    assert index.get("demo.pdf")["chunks"] == 2

    removed = index.remove("demo.pdf")

    assert removed["filename"] == "demo.pdf"
    assert index.get("demo.pdf") is None
    assert index.list() == []


def test_file_index_record_keeps_updates_across_instances(tmp_path):
    index_path = tmp_path / "files.json"

    def record_batch(worker_id: int):
        for item_id in range(20):
            filename = f"{worker_id}-{item_id}.txt"
            FileIndex(str(index_path)).record(filename, f"app/uploads/{filename}", 1)

    with ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(record_batch, range(4)))

    entries = FileIndex(str(index_path)).list()
    filenames = {entry["filename"] for entry in entries}

    assert len(entries) == 80
    assert len(filenames) == 80
