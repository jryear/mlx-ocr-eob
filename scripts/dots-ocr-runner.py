#!/usr/bin/env python3
"""Deterministic VLM stdio runner for NativeOCR.

Reads one JSON request on stdin (vlm.request.v1), emits exactly one JSON
response (vlm.response.v1) followed by a newline. Designed to be invoked per-ROI
by the Swift CLI.

Determinism & provenance:
- Greedy decode (temperature=0, top_p=1) when supported
- Records prompt_version, prompt_sha256, image_sha256, roi_image_size, idempotency key
- NFC normalizes all emitted text

Idempotency & retries:
- One retry with small backoff on failure/timeout
- Emits contract-valid empty response on persistent failure and writes a sidecar error

Validation & safety:
- Optional JSON Schema validation (set VLM_VALIDATE=1)
- Caps text size (VLM_MAX_TEXT, default 32768) and strips control chars
"""
from __future__ import annotations

import json
import os
import sys
import time
import hashlib
import unicodedata
import traceback
import re
from pathlib import Path
from typing import Any, Dict

try:
    from PIL import Image  # type: ignore
except Exception:  # pragma: no cover
    Image = None  # type: ignore

# Optional schema validation
_VALIDATE = os.environ.get("VLM_VALIDATE", "0").strip() in {"1", "true", "TRUE"}
_SCHEMA_PATH = os.environ.get("VLM_RESPONSE_SCHEMA", "schemas/vlm/response.v1.json")
try:
    if _VALIDATE:
        import jsonschema  # type: ignore
        with open(_SCHEMA_PATH, "r", encoding="utf-8") as fh:
            _RESP_SCHEMA = json.load(fh)
    else:
        _RESP_SCHEMA = None
except Exception:  # pragma: no cover
    _RESP_SCHEMA = None
    _VALIDATE = False

# Greedy/limits
MODEL_ID = os.environ.get("VLM_MODEL", "mlx-community/Qwen2-VL-7B-bf16")
MAX_TEXT = int(os.environ.get("VLM_MAX_TEXT", "32768"))
TIMEOUT_S = float(os.environ.get("VLM_TIMEOUT_S", "30"))
MAX_TOKENS = int(os.environ.get("VLM_MAX_TOKENS", os.environ.get("VLM_MAX_NEW_TOKENS", "2048")))
TEMPERATURE = float(os.environ.get("VLM_TEMPERATURE", "0"))
TOP_P = float(os.environ.get("VLM_TOP_P", "1"))
FORCE_GREEDY = os.environ.get("VLM_FORCE_GREEDY", "0").strip() in {"1","true","TRUE"}
MAX_PIXELS = int(os.environ.get("VLM_MAX_PIXELS", "4000000"))

PROMPT_VERSION = "table-json-v1"

def nfc(text: str) -> str:
    return unicodedata.normalize("NFC", text)

def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()

def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()

def slugify(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", s)[:200]

def control_sanitize(s: str) -> str:
    # Allow tabs/newlines; drop other C0 controls
    return re.sub(r"[\x00-\x08\x0B\x0C\x0E-\x1F]", "", s)

def build_prompt(hints: Dict[str, Any]) -> str:
    cols = hints.get("columns_est")
    return nfc(
        (
            "You are reading a cropped table ROI from a dental EOB.\n"
            + (f"This table likely has about {cols} columns.\n" if cols else "")
            + "Return ONLY a JSON object with keys: rows (int), cols (int), "
              "cells (array of {r:int,c:int,text:string}).\n"
              "Do not include explanations, prose, or code fences.\n"
              "Output MUST be a single JSON object in compact form (no extra whitespace).\n"
              "If no table, output {\"rows\":0,\"cols\":0,\"cells\":[]}."
        )
    )

def _extract_json_object(text: str) -> str | None:
    # Try fenced code first
    m = re.search(r"```json\s*(\{[\s\S]*?\})\s*```", text, re.IGNORECASE)
    if m:
        return m.group(1)
    # Scan for first balanced JSON object
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        else:
            if ch == '"':
                in_str = True
            elif ch == '{':
                depth += 1
            elif ch == '}':
                depth -= 1
                if depth == 0:
                    return text[start:i+1]
    return None

def try_parse_table(text: str, notes: Dict[str, Any] | None = None) -> Dict[str, Any]:
    # 1) Strict JSON whole-output
    try:
        obj = json.loads(text)
        if isinstance(obj, dict) and {"rows","cols","cells"} <= set(obj.keys()):
            cells = []
            for c in obj.get("cells", []):
                r = int(c.get("r", 0)); col = int(c.get("c", 0))
                t = nfc(control_sanitize(str(c.get("text", ""))))
                cells.append({"r": r, "c": col, "text": t})
            return {"rows": int(obj.get("rows", 0)), "cols": int(obj.get("cols", 0)), "cells": cells}
    except Exception:
        pass
    # 1b) Extract JSON block from prose if present
    try:
        block = _extract_json_object(text)
        if block:
            obj = json.loads(block)
            if isinstance(obj, dict) and {"rows","cols","cells"} <= set(obj.keys()):
                cells = []
                for c in obj.get("cells", []):
                    r = int(c.get("r", 0)); col = int(c.get("c", 0))
                    t = nfc(control_sanitize(str(c.get("text", ""))))
                    cells.append({"r": r, "c": col, "text": t})
                return {"rows": int(obj.get("rows", 0)), "cols": int(obj.get("cols", 0)), "cells": cells}
    except Exception:
        pass
    # 1c) Truncation repair: attempt to balance braces/strings and parse
    try:
        start = text.find('{')
        if start != -1:
            frag = text[start:]
            # Balance strings/brackets
            depth_stack = []
            in_str = False
            esc = False
            for ch in frag:
                if in_str:
                    if esc:
                        esc = False
                    elif ch == '\\':
                        esc = True
                    elif ch == '"':
                        in_str = False
                else:
                    if ch == '"':
                        in_str = True
                    elif ch == '{':
                        depth_stack.append('}')
                    elif ch == '[':
                        depth_stack.append(']')
                    elif ch == '}' or ch == ']':
                        if depth_stack:
                            depth_stack.pop()
            repaired = frag
            added = 0
            if in_str:
                repaired += '"'
                added += 1
            if depth_stack:
                closers = ''.join(reversed(depth_stack))
                repaired += closers
                added += len(closers)
            try:
                obj = json.loads(repaired)
                if isinstance(obj, dict) and {"rows","cols","cells"} <= set(obj.keys()):
                    if notes is not None and added > 0:
                        notes["parse_repair"] = {"truncation_repair": True, "added_chars": added}
                    cells = []
                    for c in obj.get("cells", []):
                        r = int(c.get("r", 0)); col = int(c.get("c", 0))
                        t = nfc(control_sanitize(str(c.get("text", ""))))
                        cells.append({"r": r, "c": col, "text": t})
                    return {"rows": int(obj.get("rows", 0)), "cols": int(obj.get("cols", 0)), "cells": cells}
            except Exception:
                pass
    except Exception:
        pass

    # 2) TSV/pipe fallback
    cells = []
    r = 0
    for line in text.splitlines():
        parts = [p.strip() for p in line.replace("|", "\t").split("\t") if p.strip()]
        for c, val in enumerate(parts):
            cells.append({"r": r, "c": c, "text": nfc(control_sanitize(val))})
        if parts:
            r += 1
    rows = max((c["r"] for c in cells), default=-1) + 1
    cols = max((c["c"] for c in cells), default=-1) + 1
    return {"rows": rows, "cols": cols, "cells": cells}

def validate_or_downgrade(resp: Dict[str, Any], error_note: str | None = None) -> Dict[str, Any]:
    if not _VALIDATE or _RESP_SCHEMA is None:
        return resp
    try:
        jsonschema.validate(resp, _RESP_SCHEMA)  # type: ignore
        return resp
    except Exception as exc:  # pragma: no cover
        # Downgrade to empty response
        downgraded = {"tables": [], "text_blocks": [], "model_meta": resp.get("model_meta", {})}
        _write_sidecar_error(error_note or f"schema_validation: {exc}")
        return downgraded

def _write_sidecar_error(message: str) -> None:
    try:
        Path("runs/vlm").mkdir(parents=True, exist_ok=True)
        with open("runs/vlm/runner_error.log", "a", encoding="utf-8") as fh:
            fh.write(nfc(message) + "\n")
    except Exception:
        pass

def _canon_without_digest(resp: Dict[str, Any]) -> str:
    meta = dict(resp.get("model_meta", {}))
    meta.pop("response_sha256", None)
    meta.pop("response_sha256_scope", None)
    tmp = dict(resp)
    tmp["model_meta"] = meta
    return json.dumps(tmp, ensure_ascii=False, separators=(",", ":"), sort_keys=True)

def _emit(resp: Dict[str, Any]) -> None:
    resp.setdefault("model_meta", {})
    canon_for_digest = _canon_without_digest(resp)
    digest = sha256_bytes(canon_for_digest.encode("utf-8"))
    resp["model_meta"]["response_sha256"] = digest
    resp["model_meta"]["response_sha256_scope"] = "canonical JSON with this field omitted"
    final = json.dumps(resp, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    sys.stdout.write(final + "\n")
    sys.stdout.flush()

def _generate_with_timeout(formatted_prompt: Any, images: Any, used_kwargs: Dict[str, Any]) -> (str, Dict[str, Any], bool):
    # Import locally to keep module optional
    from mlx_vlm import generate  # type: ignore
    # No hard timeout hook in lib; enforce with wall clock and break early if possible.
    # Best-effort: rely on small MAX_TOKENS + deterministic settings to bound latency.
    start = time.time()
    accepted = True
    out = None
    # Attempt 1: image kw
    try:
        out = generate(MODEL, PROCESSOR, formatted_prompt, image=images, verbose=False, **used_kwargs)
    except TypeError:
        try:
            out = generate(MODEL, PROCESSOR, formatted_prompt, image=images, verbose=False)
            accepted = False
        except Exception:
            out = None
    # Attempt 2: images kw
    if out is None:
        try:
            out = generate(MODEL, PROCESSOR, formatted_prompt, images=images, verbose=False, **used_kwargs)
            accepted = True
        except Exception:
            try:
                out = generate(MODEL, PROCESSOR, formatted_prompt, images=images, verbose=False)
                accepted = False
            except Exception:
                out = None
    # Attempt 3: prompt string instead of messages structure
    if out is None:
        try:
            prompt_str = formatted_prompt if isinstance(formatted_prompt, str) else json.dumps(formatted_prompt, ensure_ascii=False)
            out = generate(MODEL, PROCESSOR, prompt_str, image=images, verbose=False)
            accepted = False
        except Exception:
            out = None
    # Soft timeout enforcement
    if time.time() - start > TIMEOUT_S:
        raise TimeoutError(f"generation exceeded {TIMEOUT_S}s")
    # Handle GenerationResult object (mlx-vlm 0.3.4+) vs raw string
    if out is not None and hasattr(out, 'text'):
        out = out.text  # type: ignore
    if out is None or (isinstance(out, str) and out == ""):
        raise RuntimeError("mlx_vlm.generate returned empty/None output")
    return str(out), used_kwargs if accepted else {}, accepted

def _make_decode_kwargs() -> Dict[str, Any]:
    kwargs: Dict[str, Any] = {}
    if FORCE_GREEDY:
        kwargs.update(dict(temperature=0.0, top_p=1.0, do_sample=False))
    else:
        kwargs.update(dict(temperature=TEMPERATURE, top_p=TOP_P))
    if MAX_TOKENS > 0:
        kwargs.update(dict(max_tokens=MAX_TOKENS, max_new_tokens=MAX_TOKENS))
    return kwargs

def main() -> None:
    t0 = time.time()
    raw = sys.stdin.read()
    if not raw.strip():
        # Contract-safe empty response
        _emit({"schema": "vlm.response.v1", "tables": [], "text_blocks": [], "model_meta": {"engine": "mlx-vlm", "model": MODEL_ID, "prompt_version": PROMPT_VERSION, "sec": round(time.time()-t0, 3)}})
        return

    # Load request
    req = json.loads(raw)
    img_path = req.get("image_path")
    hints = req.get("hints", {}) or {}
    doc_sha = req.get("doc_sha256", "")
    pid = req.get("page_index", -1)
    rid = str(req.get("roi_id", ""))
    idem_key = f"{doc_sha}:{pid}:{rid}"
    idem_slug = slugify(idem_key)

    prompt = build_prompt(hints)
    prompt_sha = sha256_bytes(prompt.encode("utf-8"))
    img_sha = sha256_file(img_path) if img_path and Path(img_path).exists() else ""

    # Try to import model stack lazily to allow empty-response fallback when unavailable
    out_text = ""
    model_meta = {
        "engine": "mlx-vlm",
        "model": MODEL_ID,
        "prompt_version": PROMPT_VERSION,
        "prompt_sha256": prompt_sha,
        "image_sha256": img_sha,
        "roi_image_size": None,
        "idempotency_key": idem_key,
        "sec": None,
        "requested_decode": {
            "temperature": 0.0 if FORCE_GREEDY else TEMPERATURE,
            "top_p": 1.0 if FORCE_GREEDY else TOP_P,
            "do_sample": False if FORCE_GREEDY else None,
            "max_new_tokens": MAX_TOKENS if MAX_TOKENS > 0 else None,
        },
        "timeout_s": TIMEOUT_S,
    }

    # Attempt generation, one retry on failure
    last_err = None
    attempts = 0
    retry_reason = None
    for attempt in (1, 2):
        try:
            t1 = time.time()
            try:
                from mlx_vlm import load  # type: ignore
                from mlx_vlm.prompt_utils import apply_chat_template  # type: ignore
                from mlx_vlm.utils import load_config  # type: ignore
            except Exception as e:  # pragma: no cover
                raise RuntimeError(f"mlx_vlm import error: {e}")

            # Load model and processor (per-invocation; upstream CLI invokes runner per ROI)
            global MODEL, PROCESSOR, CFG  # keep in module for potential reuse
            try:
                MODEL  # type: ignore
                PROCESSOR  # type: ignore
                CFG  # type: ignore
            except NameError:  # first time in this process
                MODEL, PROCESSOR = load(MODEL_ID)  # type: ignore
                # Capture chat template/config fingerprint
                from mlx_vlm.utils import load_config as _lc  # type: ignore
                try:
                    CFG = _lc(MODEL_ID)  # type: ignore
                    model_meta["chat_template_sha256"] = sha256_bytes(json.dumps(CFG, sort_keys=True, default=str).encode("utf-8"))
                except Exception:
                    CFG = None  # type: ignore
                    model_meta["chat_template_sha256"] = "unknown"

            if Image is None or not img_path or not Path(img_path).exists():
                raise RuntimeError("PIL unavailable or image missing")
            im = Image.open(img_path).convert("RGB")
            w, h = im.size
            if w * h > MAX_PIXELS:
                scale = (MAX_PIXELS / float(w * h)) ** 0.5
                nw, nh = max(1, int(w * scale)), max(1, int(h * scale))
                im = im.resize((nw, nh))
                model_meta["resize"] = {"from": [w, h], "to": [nw, nh]}
            model_meta["roi_image_size"] = list(im.size)

            formatted = apply_chat_template(PROCESSOR, CFG, prompt, num_images=1)  # type: ignore
            # Generate with deterministic params and soft timeout enforcement
            used_kwargs = _make_decode_kwargs()
            out_text, actual_used, accepted = _generate_with_timeout(formatted, [im], used_kwargs)
            out_text = nfc(control_sanitize(out_text))[:MAX_TEXT]
            model_meta["sec"] = round(time.time() - t1, 3)
            model_meta["actual_decode"] = actual_used
            model_meta["decode_kwargs_accepted"] = bool(accepted)
            attempts = attempt
            last_err = None
            break
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
            retry_reason = last_err
            # Backoff before retry
            time.sleep(0.5)

    if last_err:
        # Persistent failure → sidecar error and empty response
        try:
            Path("runs/vlm").mkdir(parents=True, exist_ok=True)
            with open(f"runs/vlm/{idem_slug}.error", "w", encoding="utf-8") as fh:
                fh.write(nfc(last_err) + "\n" + traceback.format_exc() + "\n")
        except Exception:
            pass
        model_meta["attempts"] = attempts or 2
        if retry_reason:
            model_meta["retry_reason"] = retry_reason
        resp = {"schema": "vlm.response.v1", "tables": [], "text_blocks": [], "model_meta": model_meta}
        _emit(validate_or_downgrade(resp, last_err))
        return

    # Parse table JSON first, then fallback
    parse_notes: Dict[str, Any] = {}
    tab = try_parse_table(out_text, parse_notes)
    def rectangularize(t: Dict[str, Any]) -> Dict[str, Any]:
        R, C = int(t.get("rows", 0)), int(t.get("cols", 0))
        if R <= 0 or C <= 0:
            t["rows"], t["cols"], t["cells"] = 0, 0, []
            return t
        grid = [[""] * C for _ in range(R)]
        for c in t.get("cells", []):
            try:
                r = int(c.get("r", 0)); col = int(c.get("c", 0))
                if 0 <= r < R and 0 <= col < C:
                    grid[r][col] = str(c.get("text", ""))
            except Exception:
                continue
        t["cells"] = [{"r": r, "c": c, "text": grid[r][c]} for r in range(R) for c in range(C)]
        return t

    tab = rectangularize(tab)
    tables = [tab] if tab.get("rows", 0) > 0 and tab.get("cols", 0) > 0 else []
    model_meta["attempts"] = attempts or 1
    if parse_notes.get("parse_repair"):
        model_meta["parse_repair"] = parse_notes["parse_repair"]
    resp = {"schema": "vlm.response.v1", "tables": tables, "text_blocks": [{"text": out_text}], "model_meta": model_meta}
    _emit(validate_or_downgrade(resp))

if __name__ == "__main__":
    main()
