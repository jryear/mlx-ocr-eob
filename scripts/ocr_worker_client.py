from __future__ import annotations

import atexit
import json
import os
import subprocess
import threading
from pathlib import Path
from queue import Queue, Empty
from typing import Any, Dict, Optional


def _default_worker_script() -> Path:
    return Path(__file__).resolve().parent / "ocr_worker.py"


class PersistentOCRWorker:
    """Simple client wrapper around the OCR worker process."""

    def __init__(self, script_path: Optional[Path] = None) -> None:
        self.script_path = script_path or _default_worker_script()
        self._proc: Optional[subprocess.Popen[str]] = None
        self._stdout_queue: "Queue[str]" = Queue()
        self._lock = threading.Lock()
        self.worker_epoch: Optional[int] = None
        self._ensure_started()

    def _reader_thread(self, pipe: Any) -> None:
        while True:
            line = pipe.readline()
            if not line:
                break
            self._stdout_queue.put(line)

    def _ensure_started(self) -> None:
        if self._proc and self._proc.poll() is None:
            return
        env = os.environ.copy()
        self._proc = subprocess.Popen(
            [os.fspath(os.getenv("PYTHON_EXECUTABLE", os.sys.executable)), os.fspath(self.script_path)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
            env=env,
        )
        assert self._proc.stdin is not None
        assert self._proc.stdout is not None
        threading.Thread(target=self._reader_thread, args=(self._proc.stdout,), daemon=True).start()
        greeting = self._read_response(timeout=5.0)
        if greeting is None:
            raise RuntimeError("OCR worker failed to start")
        try:
            message = json.loads(greeting)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"OCR worker invalid greeting: {exc}") from exc
        if message.get("status") != "ready":
            raise RuntimeError(f"OCR worker failed: {message}")
        self.worker_epoch = message.get("worker_epoch")

    def _write(self, payload: Dict[str, Any]) -> None:
        assert self._proc is not None and self._proc.stdin is not None
        self._proc.stdin.write(json.dumps(payload) + "\n")
        self._proc.stdin.flush()

    def _read_response(self, timeout: float) -> Optional[str]:
        try:
            return self._stdout_queue.get(timeout=timeout)
        except Empty:
            return None

    def ocr(self, image_path: str, *, mode: str = "text", lang: str = "en-US", timeout: int = 10_000) -> Dict[str, Any]:
        with self._lock:
            self._ensure_started()
            self._write({"cmd": "ocr", "image_path": image_path, "mode": mode, "lang": lang, "timeout": timeout})
            response_line = self._read_response(timeout=timeout / 1000 + 5.0)
        if response_line is None:
            raise RuntimeError("OCR worker timed out")
        message = json.loads(response_line)
        status = message.get("status")
        if status != "ok":
            raise RuntimeError(f"OCR worker error: {message}")
        payload = message.get("payload") or {}
        payload["_worker_epoch"] = message.get("worker_epoch")
        return payload

    def shutdown(self) -> None:
        if not self._proc:
            return
        try:
            self._write({"cmd": "shutdown"})
        except Exception:
            pass
        self._proc = None


_worker_singleton: Optional[PersistentOCRWorker] = None


def get_worker() -> PersistentOCRWorker:
    global _worker_singleton
    if _worker_singleton is None:
        _worker_singleton = PersistentOCRWorker()
    return _worker_singleton


def _shutdown_worker() -> None:
    global _worker_singleton
    if _worker_singleton is not None:
        try:
            _worker_singleton.shutdown()
        finally:
            _worker_singleton = None


atexit.register(_shutdown_worker)
