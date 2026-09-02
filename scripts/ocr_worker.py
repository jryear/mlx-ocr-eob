#!/usr/bin/env python3
"""Persistent OCR worker that proxies calls to vision-ocr-cli."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict


def _load_env_path() -> Path:
    default_path = Path("vision-ocr-cli/.build/release/vision-ocr-cli")
    return Path(os.getenv("VISION_OCR_CLI", default_path)).resolve()


VISION_OCR_CLI = _load_env_path()


def _run_vision(image_path: str, *, mode: str, lang: str, timeout: int) -> Dict[str, Any]:
    if not VISION_OCR_CLI.exists():
        raise RuntimeError(f"vision-ocr-cli not found at {VISION_OCR_CLI}")
    with tempfile.NamedTemporaryFile(prefix="ocr_worker_", suffix=".jsonl", delete=False) as tmp:
        ndjson_path = Path(tmp.name)
    cmd = [
        str(VISION_OCR_CLI),
        image_path,
        "--mode",
        mode,
        "--lang",
        lang,
        "--ndjson",
        str(ndjson_path),
        "--timeout",
        str(timeout),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            return {
                "status": "error",
                "error": f"vision_cli_error:{result.returncode}",
                "stderr": result.stderr.strip(),
            }
        try:
            content = ndjson_path.read_text(encoding="utf-8").strip()
        finally:
            ndjson_path.unlink(missing_ok=True)
        if not content:
            return {"status": "error", "error": "vision_empty_output"}
        lines = content.splitlines()
        try:
            payload = json.loads(lines[0])
        except Exception as exc:
            return {"status": "error", "error": "vision_output_parse", "detail": str(exc)}
        return {"status": "ok", "payload": payload}
    except Exception as exc:
        ndjson_path.unlink(missing_ok=True)
        return {"status": "error", "error": "vision_cli_exception", "detail": str(exc)}


def main() -> None:
    worker_epoch = int(time.time())
    stdout = sys.stdout
    stdin = sys.stdin
    stdout.write(json.dumps({"status": "ready", "worker_epoch": worker_epoch}) + "\n")
    stdout.flush()
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            stdout.write(json.dumps({"status": "error", "error": "invalid_json"}) + "\n")
            stdout.flush()
            continue
        cmd = message.get("cmd")
        if cmd == "ocr":
            image_path = message.get("image_path")
            mode = message.get("mode", "text")
            lang = message.get("lang", "en-US")
            timeout = int(message.get("timeout", 10_000))
            if not image_path:
                stdout.write(json.dumps({"status": "error", "error": "missing_image_path"}) + "\n")
                stdout.flush()
                continue
            response = _run_vision(image_path, mode=mode, lang=lang, timeout=timeout)
            response["worker_epoch"] = worker_epoch
            stdout.write(json.dumps(response) + "\n")
            stdout.flush()
        elif cmd == "ping":
            stdout.write(json.dumps({"status": "ok", "worker_epoch": worker_epoch}) + "\n")
            stdout.flush()
        elif cmd == "shutdown":
            stdout.write(json.dumps({"status": "ok", "worker_epoch": worker_epoch}) + "\n")
            stdout.flush()
            break
        else:
            stdout.write(json.dumps({"status": "error", "error": "unknown_cmd"}) + "\n")
            stdout.flush()


if __name__ == "__main__":
    main()
