import json
import os
import subprocess
import sys
from pathlib import Path

import docker
import pytest

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


def test_code_interpreter_does_not_forward_host_environment_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgres://user:secret@example/db")
    monkeypatch.setenv("REDIS_URL", "redis://:secret@example/0")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "access-key")
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "/private/credentials.json")

    interpreter = CodeInterpreter({"upload_folder": str(tmp_path), "auto_build": False})

    assert interpreter._build_env() == {
        "PYTHONWARNINGS": "ignore",
        "MPLBACKEND": "Agg",
        "IMAGE_OUTPUT_DIR": "/output",
    }


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


@pytest.mark.skipif(
    os.getenv("RAG_DOCKER_INTEGRATION") != "1",
    reason="Set RAG_DOCKER_INTEGRATION=1 to run the real Docker sandbox test",
)
def test_real_code_interpreter_container_isolated_and_generates_image(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgres://audit-secret")
    monkeypatch.setenv("REDIS_URL", "redis://audit-secret")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "audit-secret")
    interpreter = CodeInterpreter(
        {"upload_folder": str(tmp_path), "auto_build": False, "timeout": 60}
    )
    code = (
        "import os, json\n"
        "import matplotlib.pyplot as plt\n"
        "print('ENV_KEYS=' + ','.join(sorted(os.environ)))\n"
        "with open('providers.json', encoding='utf-8') as handle:\n"
        "    data = json.load(handle)\n"
        "print('JSON_TYPE=' + type(data).__name__)\n"
        "plt.plot([1, 3, 2])\n"
    )

    result = interpreter.execute(
        code,
        [
            {
                "path": str(ROOT / "app/default_providers.json"),
                "runtime_name": "providers.json",
            }
        ],
    )

    assert result["success"] is True
    assert "JSON_TYPE=" in result["text"]
    assert "DATABASE_URL" not in result["text"]
    assert "REDIS_URL" not in result["text"]
    assert "AWS_ACCESS_KEY_ID" not in result["text"]
    assert result["images"]
