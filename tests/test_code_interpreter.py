import json
import os
import subprocess
import sys
from pathlib import Path

import docker

from app.utils.code_interpreter import CodeInterpreter
from app.utils.interpreter_sandbox import check_code_safety

ROOT = Path(__file__).resolve().parents[1]


def test_subprocess_wrapper_reads_data_file_from_output_workdir(tmp_path):
    data_dir = tmp_path / "data"
    output_dir = tmp_path / "output"
    data_dir.mkdir()
    output_dir.mkdir()
    (data_dir / "demo.csv").write_text("a,b\n1,2\n", encoding="utf-8")
    (data_dir / "run.py").write_text(
        "with open('demo.csv', encoding='utf-8') as f:\n"
        "    print(f.read().strip())\n",
        encoding="utf-8",
    )

    env = dict(os.environ, IMAGE_OUTPUT_DIR=str(output_dir))
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "app/utils/subprocess_wrapper.py"),
            str(data_dir / "run.py"),
            str(output_dir / "result.json"),
        ],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads((output_dir / "result.json").read_text(encoding="utf-8"))
    assert payload["success"] is True
    assert "a,b\n1,2" in payload["text"]


def test_check_code_safety_blocks_dangerous_os_alias():
    issues = check_code_safety("from os import system\nsystem('echo nope')\n")

    assert any("os.system" in issue for issue in issues)


def test_code_interpreter_auto_builds_missing_image(tmp_path):
    calls = []

    class FakeImages:
        def get(self, image_name):
            raise docker.errors.ImageNotFound(image_name)

        def build(self, **kwargs):
            calls.append(kwargs)
            return object(), []

    class FakeClient:
        images = FakeImages()

    interpreter = CodeInterpreter({"upload_folder": str(tmp_path), "auto_build": True})

    interpreter._ensure_image(FakeClient())

    assert calls
    assert calls[0]["tag"] == CodeInterpreter.IMAGE_NAME
    assert calls[0]["dockerfile"] == "Dockerfile.code-interpreter"
