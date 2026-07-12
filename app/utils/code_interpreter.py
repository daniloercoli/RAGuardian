"""
Docker-based code interpreter module. Executes user code inside a transient
Docker container with strict security restrictions.
"""
import json
import os
import shutil
import uuid
from pathlib import Path
from typing import Dict, List

import docker
import requests
from dotenv import dotenv_values

from utils.logging_config import APP_LOGGER as log
from utils.interpreter_sandbox import check_code_safety, sanitize_filename


class CodeInterpreter:
    """Sandboxed Python code execution via Docker container.

    Each call creates a transient (auto-removed) container running the
    ``code-interpreter:latest`` image. Data files are mounted read-only at
    ``/data`` and outputs land in a writable bind mount at ``/output``.
    """

    IMAGE_NAME = "code-interpreter:latest"
    WRAPPER = "/app/subprocess_wrapper.py"
    CONTAINER_CODE = "/data/run.py"
    CONTAINER_RESULT = "/output/result.json"

    def __init__(self, config: Dict[str, object] | None = None):
        config = config or {}
        env = dotenv_values(".env")

        self.timeout = int(
            config.get("timeout")
            or env.get("CODE_INTERPRETER_TIMEOUT", "120")
        )
        self.docker_memory = str(
            config.get("docker_memory")
            or env.get("CODE_INTERPRETER_DOCKER_MEMORY", "512m")
        )
        self.docker_cpu_quota = int(
            config.get("docker_cpu_quota")
            or env.get("CODE_INTERPRETER_DOCKER_CPU_QUOTA", "100000")
        )
        auto_build_value = (
            config["auto_build"]
            if "auto_build" in config
            else env.get("CODE_INTERPRETER_AUTO_BUILD", "1")
        )
        self.auto_build = _as_bool(auto_build_value, default=True)

        upload_folder = str(config.get("upload_folder", "app/uploads"))
        self.upload_folder = Path(upload_folder)
        self.upload_folder.mkdir(parents=True, exist_ok=True)

        base = self.upload_folder / "chat_files"
        self._code_dir = base / "code_runs"
        self._code_dir.mkdir(parents=True, exist_ok=True)
        self._pics_dir = base / "pics"
        self._pics_dir.mkdir(parents=True, exist_ok=True)

        self._client = None

    def _prepare_run_dirs(
        self,
        run_id: str,
        code: str,
        data_files: List[Dict[str, str]] | None = None,
    ) -> tuple[Path, Path]:
        """Create host directories for bind mounts.

        Returns (data_dir, output_dir).
        """
        data_dir = self._code_dir / run_id / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        output_dir = self._code_dir / run_id / "output"
        output_dir.mkdir(parents=True, exist_ok=True)

        if data_files:
            for df in data_files:
                src = Path(df["path"])
                if src.exists():
                    runtime_name = sanitize_filename(
                        str(df.get("runtime_name") or src.name)
                    )
                    if runtime_name:
                        shutil.copy2(str(src), str(data_dir / runtime_name))

        (data_dir / "run.py").write_text(code, encoding="utf-8")
        return data_dir, output_dir

    def _build_env(self) -> dict[str, str]:
        """Build the minimal environment required inside the container."""
        return {
            "PYTHONWARNINGS": "ignore",
            "MPLBACKEND": "Agg",
            "IMAGE_OUTPUT_DIR": "/output",
        }

    def execute(
        self, code: str, data_files: List[Dict[str, str]] | None = None
    ) -> Dict:
        """Execute Python code in a transient Docker container.

        Args:
            code: Python code string.
            data_files: Optional list of dicts with 'path' and 'name' keys.

        Returns:
            Dict with keys: success, text, images, error.
        """
        if not code:
            return {"success": False, "error": "Nessun codice fornito"}

        issues = check_code_safety(code)
        if issues:
            return {"success": False, "error": "; ".join(issues)}

        run_id = uuid.uuid4().hex[:12]
        data_dir, output_dir = self._prepare_run_dirs(run_id, code, data_files)
        # Ensure container user (1000) can write to the output bind mount
        os.chmod(str(output_dir), 0o777)

        container = None
        try:
            client = self._client or docker.from_env()
            self._client = client
            self._ensure_image(client)
            container = client.containers.run(
                self.IMAGE_NAME,
                command=[
                    "/usr/local/bin/python",
                    self.WRAPPER,
                    self.CONTAINER_CODE,
                    self.CONTAINER_RESULT,
                ],
                remove=True,
                network_disabled=True,
                read_only=True,
                tmpfs={"/tmp": "rw,noexec,nosuid,size=128m"},
                user="1000:1000",
                mem_limit=self.docker_memory,
                cpu_quota=self.docker_cpu_quota,
                volumes={
                    str(data_dir): {"bind": "/data", "mode": "ro"},
                    str(output_dir): {"bind": "/output", "mode": "rw"},
                },
                environment=self._build_env(),
                name=f"interpreter-{run_id}",
                detach=True,
                stdout=True,
                stderr=True,
            )
            try:
                wait_result = container.wait(timeout=self.timeout)
            except requests.exceptions.ReadTimeout:
                container.kill()
                return {"success": False, "error": "Execution timed out"}

            status_code = int(wait_result.get("StatusCode", 1))
            if status_code != 0:
                parsed = self._read_result(output_dir)
                if parsed is not None:
                    return self._normalise_result(parsed, run_id, output_dir)
                logs = container.logs(stdout=True, stderr=True).decode(errors="replace")
                log.error("Container error: %s", logs[:4096])
                return {
                    "success": False,
                    "error": logs.strip()[:8192] or "Execution failed",
                }
        except docker.errors.ImageNotFound:
            return {
                "success": False,
                "error": (
                    "Immagine Docker non trovata e auto-build non disponibile. "
                    "Esegui: docker build -f Dockerfile.code-interpreter "
                    "-t code-interpreter:latest . oppure abilita CODE_INTERPRETER_AUTO_BUILD=1."
                ),
            }
        except docker.errors.DockerException as exc:
            log.error("Docker unavailable for code interpreter: %s", exc)
            return {
                "success": False,
                "error": (
                    "Docker non disponibile per il code interpreter. "
                    "Verifica che il daemon sia attivo e che l'immagine "
                    "code-interpreter:latest sia stata creata."
                ),
            }
        except Exception as exc:
            log.error("Code interpreter error: %s", exc)
            return {"success": False, "error": str(exc)}
        finally:
            if container is not None:
                try:
                    container.remove(force=True)
                except docker.errors.DockerException:
                    pass

        # Parse result.json from output directory
        raw = self._read_result(output_dir)
        if raw is None:
            return {"success": False, "error": "Execution produced no result"}
        return self._normalise_result(raw, run_id, output_dir)

    def _ensure_image(self, client) -> None:
        if not self.auto_build:
            return
        try:
            client.images.get(self.IMAGE_NAME)
            return
        except docker.errors.ImageNotFound:
            pass

        project_root = Path(__file__).resolve().parents[2]
        dockerfile = project_root / "Dockerfile.code-interpreter"
        if not dockerfile.exists():
            raise RuntimeError(f"Dockerfile non trovato: {dockerfile}")

        log.info("Code interpreter image missing, building %s", self.IMAGE_NAME)
        try:
            client.images.build(
                path=str(project_root),
                dockerfile=str(dockerfile.relative_to(project_root)),
                tag=self.IMAGE_NAME,
                rm=True,
                pull=False,
            )
        except docker.errors.BuildError as exc:
            raise RuntimeError(f"Build automatico immagine Docker fallito: {exc}") from exc
        log.info("Code interpreter image built: %s", self.IMAGE_NAME)

    def _read_result(self, output_dir: Path) -> dict | None:
        host_result = output_dir / "result.json"
        if not host_result.exists():
            return None
        try:
            return json.loads(host_result.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, ValueError) as exc:
            return {"success": False, "error": f"Invalid result: {exc}"}

    def _normalise_result(self, raw: dict, run_id: str, output_dir: Path) -> Dict:
        text_out = raw.get("text", "")
        image_paths = raw.get("images") or []

        # Copy images to host pics directory
        url_images: list[str] = []
        seen: set[str] = set()
        for img_ref in image_paths:
            img_name = sanitize_filename(Path(img_ref).name)
            if img_name not in seen:
                src = output_dir / img_name
                if src.exists():
                    dst_name = f"{run_id}_{img_name}"
                    dst = self._pics_dir / dst_name
                    shutil.copy2(str(src), str(dst))
                    url_images.append(f"/code_pics/{dst_name}")
                    seen.add(img_name)

        # Scan for extra PNGs (e.g. auto-saved by matplotlib)
        for png_file in output_dir.glob("*.png"):
            img_name = sanitize_filename(png_file.name)
            if img_name and img_name not in seen:
                dst_name = f"{run_id}_{img_name}"
                dst = self._pics_dir / dst_name
                shutil.copy2(str(png_file), str(dst))
                url_images.append(f"/code_pics/{dst_name}")
                seen.add(img_name)

        return {
            "success": raw.get("success", False),
            "text": text_out,
            "error": raw.get("error", ""),
            "images": url_images,
        }


def _as_bool(value, *, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}
