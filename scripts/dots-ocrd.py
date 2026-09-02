#!/usr/bin/env python3
"""Persistent VLM runner (STDIO) for NativeOCR.

Protocol (JSON Lines over STDIO):
 - hello:  {"type":"hello","runner_mode":"persistent","model_loaded_ms":int,"model_fingerprint":{...}}
 - infer:  {"type":"infer","id":str,"image_b64":str,"roi_meta":{...},"decode":{...},"schema_hint":{...},"payer_hint":str,"compact":bool}
 - result: {"type":"result","id":str,"rows":int,"cols":int,"cells":list,"model_meta":{...},"status":"ok|error"}
 - goodbye:{"type":"goodbye","stats":{...}}

Determinism: greedy decode; NFC normalization; control char strip; compact JSON.
"""
from __future__ import annotations

import base64
import json
import os
import sys
import time
import hashlib
import unicodedata as ud
import re
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, Tuple, Iterable

try:
    from PIL import Image
except Exception:
    Image = None  # type: ignore

# Runner pins via env
MODEL_ID = (os.getenv("VLM_MODEL") or "mlx-community/Qwen2-VL-7B-bf16").strip()
try:
    MAX_TOKENS = int(os.getenv("VLM_MAX_NEW_TOKENS"))
except (TypeError, ValueError):
    try:
        MAX_TOKENS = int(os.getenv("VLM_MAX_TOKENS"))
    except (TypeError, ValueError):
        MAX_TOKENS = 1536
TIMEOUT_S = float(os.getenv("VLM_TIMEOUT_S", "120"))
FORCE_GREEDY = os.getenv("VLM_FORCE_GREEDY", "1").strip() in {"1","true","TRUE"}
VLM_MAX_PIXELS = int(float(os.getenv("VLM_MAX_PIXELS", "2000000")))
ERROR_DIR = Path(os.getenv("VLM_ERROR_DIR", "runs/vlm/errors"))
STRICT_JSON = os.getenv("VLM_STRICT_JSON", "1").strip() not in {"0", "false", "FALSE"}
COERCE_JSON = os.getenv("VLM_COERCE_JSON", "0").strip() in {"1", "true", "TRUE"}
ESCALATE_MODEL = (os.getenv("VLM_ESCALATE_MODEL") or MODEL_ID).strip()
try:
    ESCALATE_MAX_TOKENS = int(os.getenv("VLM_ESCALATE_MAX_NEW_TOKENS"))
except (TypeError, ValueError):
    ESCALATE_MAX_TOKENS = max(MAX_TOKENS, 2048)
ESCALATE_INVALID_THRESHOLD = float(os.getenv("VLM_ESCALATE_INVALID_THRESHOLD", "0.5"))
ESCALATE_PARSE_THRESHOLD = float(os.getenv("VLM_ESCALATE_PARSE_THRESHOLD", "0.1"))
ESCALATE_MIN_SAMPLES = int(os.getenv("VLM_ESCALATE_MIN_SAMPLES", "4"))
JSON_FIRST_OBJ = re.compile(r"\{", re.S)
JSON_FIRST_ARR = re.compile(r"\[", re.S)
MONEY_RX = re.compile(r"(?:^|[^\d])\d{1,3}(?:[,\s]\d{3})*(?:\.\d{2})?(?:$|[^\d])")
PROC_RX = re.compile(r"\bD\d{4}\b|\b\d{5}\b")
DATE_RX = re.compile(r"\b(?:\d{1,2}[\/\-]\d{1,2}[\/\-]\d{2,4})\b")

MODEL_CACHE: Dict[str, Tuple[Any, Any, Dict[str,str], Dict[str, Any]]] = {}
MODEL: Any = None
PROCESSOR: Any = None
CFG: Dict[str, Any] = {}
MODEL_FP: Dict[str, str] = {}
CURRENT_MODEL_ID: str = MODEL_ID
session_stats: Dict[str, Dict[str, Any]] = defaultdict(lambda: {"total": 0, "invalid": 0, "parse_error": 0, "escalated": False})
session_overrides: Dict[str, Dict[str, Any]] = {}

def _safe_slug(value: str | None) -> str:
    import re
    return re.sub(r"[^A-Za-z0-9._-]+", "_", (value or "").strip()) or "unknown"

def _log_raw_output(session_id: str | None, roi_id: str | None, stage: str, reason: str, raw_text: str, details: Dict[str, Any] | None = None) -> None:
    try:
        ERROR_DIR.mkdir(parents=True, exist_ok=True)
        base = f"{_safe_slug(session_id)}_{_safe_slug(roi_id)}_{stage}"
        raw_path = ERROR_DIR / f"{base}.raw.txt"
        meta_path = ERROR_DIR / f"{base}.reason.json"
        raw_path.write_text(raw_text or "", encoding="utf-8")
        meta = {
            "session_id": session_id,
            "roi_id": roi_id,
            "stage": stage,
            "reason": reason
        }
        if details:
            meta.update(details)
        meta_path.write_text(canon(meta), encoding="utf-8")
    except Exception:
        pass


def ensure_model(model_id: str) -> Tuple[Any, Any, Dict[str, Any], Dict[str, str]]:
    global MODEL, PROCESSOR, CFG, MODEL_FP, CURRENT_MODEL_ID
    if model_id not in MODEL_CACHE:
        MODEL_CACHE[model_id] = load_model_bundle(model_id)
    model, processor, fp, cfg = MODEL_CACHE[model_id]
    MODEL = model
    PROCESSOR = processor
    MODEL_FP = fp
    CFG = cfg
    CURRENT_MODEL_ID = model_id
    return MODEL, PROCESSOR, CFG, MODEL_FP


def maybe_escalate(session_id: str) -> None:
    stats = session_stats[session_id]
    if stats.get("escalated"):
        return
    total = stats.get("total", 0)
    if total < ESCALATE_MIN_SAMPLES:
        return
    invalid_ratio = stats.get("invalid", 0) / total if total else 0.0
    parse_ratio = stats.get("parse_error", 0) / total if total else 0.0
    if invalid_ratio > ESCALATE_INVALID_THRESHOLD and parse_ratio < ESCALATE_PARSE_THRESHOLD:
        prev_model = CURRENT_MODEL_ID
        ensure_model(ESCALATE_MODEL)
        stats["escalated"] = True
        session_overrides[session_id] = {"max_tokens": max(MAX_TOKENS, ESCALATE_MAX_TOKENS)}
        detail = {
            "session_id": session_id,
            "total": total,
            "invalid": stats.get("invalid", 0),
            "parse_error": stats.get("parse_error", 0),
            "model_before": prev_model,
            "model_after": CURRENT_MODEL_ID,
            "max_tokens": session_overrides[session_id]["max_tokens"]
        }
        _log_raw_output(session_id, None, "escalate", "model_upgrade", "", detail)

def nfc(s: str) -> str:
    return ud.normalize("NFC", s or "")

def strip_ctrl(s: str) -> str:
    return "".join(ch for ch in s if (ud.category(ch)[0] != "C") or ch in "\n\t")

def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def compute_prompt_sha(prompt: str) -> str:
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()

def canon(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), sort_keys=True)

def load_model_bundle(model_id: str) -> Tuple[Any, Any, Dict[str,str], Dict[str, Any]]:
    t0 = time.time()
    from mlx_vlm import load
    from mlx_vlm.utils import load_config
    model, processor = load(model_id)
    try:
        cfg = load_config(model_id) or {}
    except Exception:
        cfg = {}
    fp = {
        "name": model_id,
        "revision_sha": "unknown",
        "quant": "int4" if "4bit" in model_id or "int4" in model_id else "bf16",
        "tokenizer_sha": "unknown",
        "chat_template_sha": sha256_bytes(json.dumps(cfg, sort_keys=True, default=str).encode("utf-8")) if cfg else "unknown",
        "model_loaded_ms": str(int((time.time()-t0)*1000))
    }
    return model, processor, fp, cfg

def generate_text(MODEL, PROCESSOR, CFG, img: Image.Image, prompt: str, max_tokens: int) -> str:
    from mlx_vlm import generate
    from mlx_vlm.prompt_utils import apply_chat_template
    try:
        formatted = apply_chat_template(PROCESSOR, CFG, prompt, num_images=1)
    except Exception:
        # Fallback if CFG absent or incompatible
        formatted = apply_chat_template(PROCESSOR, None, prompt, num_images=1)
    start = time.time()
    kwargs = {}
    if FORCE_GREEDY:
        kwargs.update(dict(temperature=0.0, top_p=1.0, do_sample=False))
    if max_tokens:
        kwargs.update(dict(max_tokens=max_tokens, max_new_tokens=max_tokens))
    out = None
    try:
        out = generate(MODEL, PROCESSOR, formatted, image=[img], verbose=False, **kwargs)
    except TypeError:
        out = generate(MODEL, PROCESSOR, formatted, image=[img], verbose=False)
    if time.time() - start > TIMEOUT_S:
        raise TimeoutError("decode timeout")
    # Normalize various return types
    if hasattr(out, "text"):
        out = out.text
    if not isinstance(out, str):
        out = str(out)
    return nfc(strip_ctrl(out))

def model_fingerprint_metadata() -> Dict[str, str]:
    return {
        "name": CURRENT_MODEL_ID,
        "revision_sha": MODEL_FP.get("revision_sha", "unknown"),
        "quant": MODEL_FP.get("quant", "unknown")
    }


SYSTEM_INSTRUCTION = "You are a JSON machine. Output ONLY valid JSON with no comments or prose."

def _header_ctx(header_lock: Dict[str, Any] | None) -> str:
    if not isinstance(header_lock, dict):
        return ""
    cols = header_lock.get("canonical_order") or header_lock.get("headers") or []
    if not cols:
        return ""
    cols_txt = ", ".join(str(c) for c in cols)
    return f"Table columns (ordered): [{cols_txt}].\n"


def make_data_prompt(header_lock: Dict[str, Any] | None, payer: str | None, compact: bool) -> str:
    hdr = _header_ctx(header_lock)
    payer_txt = f"Payer: {payer}.\n" if payer else ""
    compact_txt = "Use compact JSON without extra whitespace.\n" if compact else ""
    return nfc(
        (
            f"{SYSTEM_INSTRUCTION}\n"
            f"{payer_txt}"
            f"{hdr}"
            "Return ONLY the table aligned to these columns.\n"
            "Output EXACTLY: {\"rows\":int,\"cols\":int,\"cells\":[{\"r\":int,\"c\":int,\"text\":string}, ...]}\n"
            "Rules:\n"
            "- rows * cols MUST equal len(cells).\n"
            "- Column order is fixed by the header list; do not add, remove, or reorder columns.\n"
            "- Write the literal cell value only. Do NOT prepend labels like \"Date:\" or \"Code:\".\n"
            "- If a cell has no value, emit \"\" (empty string). Never invert values or codes.\n"
            "- Use only text read from the image. Do NOT copy column names or prompt text into the cells unless the image literally contains that text in the row.\n"
            "- The data rows appear below the header row in the image; extract them in that order.\n"
            "- If no table exists, return exactly {\"rows\":0,\"cols\":0,\"cells\":[]} and nothing else.\n"
            f"{compact_txt}"
            "Return the JSON object ONLY."
        )
    )


def prompt_for(columns: list[str] | None, payer: str | None, compact: bool) -> str:
    cols = ",".join(columns or [])
    return nfc(
        (
            SYSTEM_INSTRUCTION + "\n" +
            (f"Payer: {payer}.\n" if payer else "") +
            (f"Columns: [{cols}].\n" if columns else "") +
            "Return ONLY a single JSON object with keys rows(int), cols(int), cells(array of {r:int,c:int,text:string}).\n"
            "Do not write prose, explanations, additional keys, or code fences.\n"
            "If no table is present, respond exactly with {\"rows\":0,\"cols\":0,\"cells\":[]}.\n"
            + ("Use compact JSON without extra whitespace.\n" if compact else "")
        )
    )

def prompt_for_headers(columns: list[str] | None, payer: str | None, compact: bool) -> str:
    cols = ",".join(columns or [])
    return nfc(
        (
            SYSTEM_INSTRUCTION + "\n" +
            (f"Payer: {payer}.\n" if payer else "") +
            (f"Canonical columns (ordered): [{cols}].\n" if columns else "") +
            "Return ONLY a JSON array of header strings in reading order.\n"
            "Do not include prose, keys, or data values. If unsure, respond [].\n"
            + ("Use compact JSON without extra whitespace.\n" if compact else "")
        )
    )

def _norm(s: str) -> str:
    import re
    s = nfc(s).upper()
    s = re.sub(r"[^A-Z0-9 ]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s

def _lev(a: str, b: str) -> int:
    la, lb = len(a), len(b)
    if la == 0: return lb
    if lb == 0: return la
    dp = list(range(lb+1))
    for i, ca in enumerate(a, 1):
        prev = dp[0]
        dp[0] = i
        for j, cb in enumerate(b, 1):
            cur = dp[j]
            cost = 0 if ca == cb else 1
            dp[j] = min(dp[j] + 1, dp[j-1] + 1, prev + cost)
            prev = cur
    return dp[lb]

def _jaccard_tokens(a: str, b: str) -> float:
    ta = set(_norm(a).split())
    tb = set(_norm(b).split())
    if not ta and not tb:
        return 1.0
    inter = len(ta & tb)
    union = len(ta | tb)
    return inter / union if union else 0.0

def _map_headers(detected: list[str], canonical: list[str]) -> tuple[list[dict], list[str]]:
    n = max(len(detected), len(canonical))
    mapping: list[dict] = []
    unmapped: list[str] = []
    used_det: set[int] = set()
    for j, can in enumerate(canonical):
        best = None
        best_cost = 1e9
        best_terms = None
        for i, det in enumerate(detected):
            if i in used_det:
                continue
            levn = _lev(_norm(det), _norm(can)) / max(1, max(len(det), len(can)))
            jacc = _jaccard_tokens(det, can)
            pos = abs(i - j) / max(1, n - 1)
            syn = 0.0
            cost = 0.60 * levn + 0.40 * (1.0 - jacc) + 0.25 * pos - syn
            if cost < best_cost or (abs(cost - best_cost) < 1e-9 and (pos < (best_terms[2] if best_terms else 1e9))):
                best_cost = cost
                best = i
                best_terms = (levn, jacc, pos, syn)
        if best is not None:
            used_det.add(best)
            if best_cost < 0.50:
                mapping.append({"det_idx": best, "canonical": can, "cost": round(best_cost, 4), "cost_terms": {"lev": round(best_terms[0],4), "jacc": round(best_terms[1],4), "pos": round(best_terms[2],4), "syn": round(best_terms[3],4)}})
            else:
                unmapped.append(can)
        else:
            unmapped.append(can)
    mapping.sort(key=lambda m: (m["cost"], m["det_idx"], m["canonical"]))
    return mapping, unmapped

def _balanced_json_slice(s: str) -> str | None:
    i_obj = JSON_FIRST_OBJ.search(s)
    i_arr = JSON_FIRST_ARR.search(s)
    if not (i_obj or i_arr):
        return None
    i0 = min([m.start() for m in (i_obj, i_arr) if m] or [0])
    stack: list[str] = []
    for i, ch in enumerate(s[i0:], start=i0):
        if ch in "{[":
            stack.append("}" if ch == "{" else "]")
        elif ch in "}]":
            if not stack or ch != stack[-1]:
                return None
            stack.pop()
            if not stack:
                return s[i0:i + 1]
    return None

def extract_json(raw: str) -> Tuple[Any | None, str | None]:
    """Strict balanced JSON extraction. Returns (obj, slice)."""
    sl = _balanced_json_slice(raw)
    if not sl:
        return None, None
    try:
        obj = json.loads(sl)
        if not STRICT_JSON and not isinstance(obj, (dict, list)):
            raise ValueError("non_json_root")
        return obj, sl
    except Exception:
        return None, sl

def normalize_table(obj: Dict[str, Any]) -> Tuple[int, int, list[Dict[str, Any]]]:
    if not isinstance(obj, dict):
        raise ValueError("not_object")
    try:
        rows = int(obj.get("rows", 0))
        cols = int(obj.get("cols", 0))
    except Exception:
        raise ValueError("shape")
    cells = obj.get("cells")
    if not isinstance(cells, list):
        raise ValueError("cells_not_list")
    if rows <= 0 or cols <= 0:
        raise ValueError("shape")
    if rows * cols != len(cells):
        raise ValueError("rect")
    normalized: list[Dict[str, Any]] = [{"r": r, "c": c, "text": ""} for r in range(rows) for c in range(cols)]
    for cell in cells:
        if not isinstance(cell, dict):
            continue
        try:
            r = int(cell.get("r", -1))
            c = int(cell.get("c", -1))
        except Exception:
            continue
        if 0 <= r < rows and 0 <= c < cols:
            idx = r * cols + c
            txt = nfc(strip_ctrl(str(cell.get("text", ""))))
            entry = normalized[idx]
            entry["text"] = txt
            if "confidence" in cell and cell["confidence"] is not None:
                entry["confidence"] = cell["confidence"]
    return rows, cols, normalized


def _signal_counts(cells: list[Dict[str, Any]]) -> Tuple[int, int, int, int]:
    nonblank = money = proc = dates = 0
    for cell in cells:
        if not isinstance(cell, dict):
            continue
        txt = str(cell.get("text", ""))
        if txt.strip():
            nonblank += 1
        if MONEY_RX.search(txt):
            money += 1
        if PROC_RX.search(txt):
            proc += 1
        if DATE_RX.search(txt):
            dates += 1
    return nonblank, money, proc, dates


def valid_table(rows: int, cols: int, cells: list[Dict[str, Any]]) -> Tuple[bool, str]:
    if rows <= 0 or cols <= 0 or rows * cols != len(cells):
        return False, "rect"
    nb, money_hits, proc_hits, date_hits = _signal_counts(cells)
    if (money_hits + proc_hits) >= 2 and nb >= max(3, rows):
        return True, "tierA"
    if nb >= max(6, (rows * cols) // 6 or 1) and (date_hits >= 2 or proc_hits >= 1):
        return True, "tierB"
    return False, "low_signal"

def propose_numeric_dense_rois(tokens: Iterable[Dict[str, Any]], W: float, H: float, k: int = 3) -> list[Tuple[float, float, float, float]]:
    """
    tokens: iterable of {x0,y0,x1,y1,text}
    returns up to k ROI boxes [(x0,y0,x1,y1), ...] with highest numeric density.
    """
    buckets: list[Tuple[Tuple[float, float, float, float], float]] = []
    for t in tokens:
        txt = str(t.get("text", ""))
        digits = sum(ch.isdigit() for ch in txt)
        if digits == 0:
            continue
        x0, y0, x1, y1 = t.get("x0", 0.0), t.get("y0", 0.0), t.get("x1", 0.0), t.get("y1", 0.0)
        area = max(1.0, float(x1 - x0) * float(y1 - y0))
        score = digits / area
        buckets.append(((float(x0), float(y0), float(x1), float(y1)), score))
    buckets.sort(key=lambda b: (round(b[0][1] / (H / max(1, k * 5))), -b[1]))
    rois: list[Tuple[float, float, float, float]] = []
    seen_bands = set()
    band_height = H / 20.0 if H else 1.0
    for box, _ in buckets:
        band = round(box[1] / max(1e-6, band_height))
        if band in seen_bands:
            continue
        rois.append(box)
        seen_bands.add(band)
        if len(rois) >= k:
            break
    return rois

def decode_image(b64: str) -> Image.Image:
    raw = base64.b64decode(b64)
    from io import BytesIO
    im = Image.open(BytesIO(raw)).convert("RGB")
    w,h = im.size
    if w*h > VLM_MAX_PIXELS:
        import math
        scale = math.sqrt(VLM_MAX_PIXELS/(w*h))
        nw, nh = max(1,int(w*scale)), max(1,int(h*scale))
        im = im.resize((nw, nh))
    return im

def main() -> None:
    global MODEL, PROCESSOR, CFG, MODEL_FP
    t0 = time.time()
    try:
        ensure_model(MODEL_ID)
    except Exception:
        MODEL = None
        PROCESSOR = None
        CFG = {}
        MODEL_FP = {
            "name": MODEL_ID,
            "revision_sha": "unknown",
            "quant": "unknown",
            "tokenizer_sha": "unknown",
            "chat_template_sha": "unknown",
            "model_loaded_ms": str(int((time.time() - t0) * 1000))
        }
    hello = {
        "type": "hello",
        "runner_mode": "persistent",
        "model_loaded_ms": int((time.time() - t0) * 1000),
        "model_fingerprint": MODEL_FP
    }
    sys.stdout.write(canon(hello)+"\n"); sys.stdout.flush()

    processed = 0
    total_sec = 0.0
    for line in sys.stdin:
        line=line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except Exception:
            continue
        if msg.get("type") == "infer.headers.v1":
            rid = msg.get("roi_id") or f"roi-{processed:06x}"
            b64 = msg.get("image_b64")
            img_path = msg.get("image_path")
            decode = msg.get("decode", {})
            session_id = msg.get("session_id") or "sess-00000000-x"
            try:
                max_new_tokens = int(decode.get("max_new_tokens", MAX_TOKENS))
            except (TypeError, ValueError):
                max_new_tokens = MAX_TOKENS
            override = session_overrides.get(session_id)
            if override and override.get("max_tokens"):
                max_new_tokens = max(max_new_tokens, override["max_tokens"])
            compact = bool(msg.get("compact", True))
            payer = msg.get("payer_hint")
            schema_hint = msg.get("schema_hint") or {}
            canonical_cols = schema_hint.get("canonical_columns") or []
            budget = msg.get("budget") or {}
            request_sha = ""
            prompt_sha = ""
            try:
                # decode image sha
                image_sha256 = None
                if b64:
                    raw = base64.b64decode(b64)
                    image_sha256 = sha256_bytes(raw)
                    im = decode_image(b64)
                elif img_path:
                    p = Path(img_path)
                    raw = p.read_bytes()
                    image_sha256 = sha256_bytes(raw)
                    im = Image.open(img_path).convert("RGB")
                else:
                    raise RuntimeError("missing image content")
                # generate headers-only
                prompt = prompt_for_headers(canonical_cols, payer, compact)
                prompt_sha = compute_prompt_sha(prompt)
                t1 = time.time()
                text = generate_text(MODEL, PROCESSOR, CFG, im, prompt, min(max_new_tokens, 512))
                sec = time.time() - t1
                request_meta = {
                    "session_id": session_id,
                    "roi_id": rid,
                    "stage": "headers",
                    "prompt_sha256": prompt_sha,
                    "image_sha256": image_sha256,
                    "model": CURRENT_MODEL_ID,
                    "max_new_tokens": max_new_tokens,
                }
                request_sha = sha256_bytes(canon(request_meta).encode("utf-8"))
                obj, raw_slice = extract_json(text)
                arr: list[str] = []
                if obj is None:
                    slice_source = raw_slice or text or ""
                    details = {
                        "slice": slice_source[:256],
                        "slice_sha256": sha256_bytes(slice_source.encode("utf-8")),
                        "request_sha256": request_sha,
                        "prompt_sha256": prompt_sha,
                    }
                    _log_raw_output(session_id, rid, "headers", "parse_error", text or "", details)
                elif isinstance(obj, list):
                    arr = obj
                else:
                    slice_source = raw_slice or text or ""
                    details = {
                        "slice": slice_source[:256],
                        "slice_sha256": sha256_bytes(slice_source.encode("utf-8")),
                        "request_sha256": request_sha,
                        "prompt_sha256": prompt_sha,
                    }
                    _log_raw_output(session_id, rid, "headers", "unexpected_headers_payload", text or "", details)
                detected_headers = []
                for idx, h in enumerate(arr):
                    s = nfc(strip_ctrl(str(h or "")))
                    detected_headers.append({"text": s, "text_norm": s, "bbox": [0,0,0,0], "order_idx": idx})
                mapping, unmapped = _map_headers([h["text"] for h in detected_headers], canonical_cols)
                lock_obj = {
                    "canonical_order": canonical_cols,
                    "mapping": [{"canonical": m["canonical"], "det_idx": m["det_idx"]} for m in mapping],
                    "unmapped": unmapped
                }
                lock_src = {"image_sha256": image_sha256, **lock_obj}
                lock_sha = sha256_bytes(canon(lock_src).encode("utf-8"))
                lock_obj["lock_sha256"] = lock_sha
                bc = budget.get("class") or "typical"
                est_cells = budget.get("est_cells")
                est_source = budget.get("est_source") or "vision_lines"
                meta = {
                    "response_sha256": sha256_bytes((text or "").encode("utf-8")),
                    "parse_repair": "none",
                    "decode_kwargs_accepted": {"max_new_tokens": max_new_tokens},
                    "budget_class": bc,
                    "est_cells": est_cells or 0,
                    "est_source": est_source,
                    "runner_mode": "persistent",
                    "model_fingerprint": model_fingerprint_metadata()
                }
                resp = {
                    "type": "result.headers.v1",
                    "session_id": session_id,
                    "roi_id": rid,
                    "detected_headers": detected_headers,
                    "mapping": mapping,
                    "canonical_order": canonical_cols,
                    "header_lock": lock_obj,
                    "model_meta": meta,
                    "status": "ok" if detected_headers else "invalid",
                    "prompt_sha256": prompt_sha,
                    "request_sha256": request_sha,
                }
                if not detected_headers:
                    slice_source = raw_slice or text or ""
                    details = {
                        "slice": slice_source[:256],
                        "slice_sha256": sha256_bytes(slice_source.encode("utf-8")),
                        "request_sha256": request_sha,
                        "prompt_sha256": prompt_sha,
                    }
                    _log_raw_output(session_id, rid, "headers", "empty_headers", text or "", details)
                resp_sha = sha256_bytes(canon(resp).encode("utf-8"))
                resp["response_sha256"] = resp_sha
            except Exception as e:
                resp = {
                    "type": "result.headers.v1",
                    "session_id": session_id,
                    "roi_id": rid,
                    "detected_headers": [],
                    "mapping": [],
                    "canonical_order": canonical_cols,
                    "header_lock": {"canonical_order": canonical_cols, "mapping": [], "unmapped": canonical_cols, "lock_sha256": sha256_bytes(b"")},
                    "model_meta": {
                        "response_sha256": sha256_bytes(b""),
                        "parse_repair": "none",
                        "decode_kwargs_accepted": {"max_new_tokens": max_new_tokens},
                        "budget_class": budget.get("class") or "typical",
                        "est_cells": budget.get("est_cells") or 0,
                        "est_source": budget.get("est_source") or "default",
                        "runner_mode": "persistent",
                        "model_fingerprint": model_fingerprint_metadata()
                    },
                    "status": "invalid"
                }
                resp["prompt_sha256"] = prompt_sha
                if "request_sha" in locals() and request_sha:
                    resp["request_sha256"] = request_sha
                resp["response_sha256"] = sha256_bytes(canon(resp).encode("utf-8"))
                slice_source = text or ""
                details = {
                    "slice": slice_source[:256],
                    "slice_sha256": sha256_bytes(slice_source.encode("utf-8")),
                    "error": str(e)
                }
                _log_raw_output(session_id, rid, "headers", f"exception:{type(e).__name__}", text or "", details)
            sys.stdout.write(canon(resp)+"\n"); sys.stdout.flush()
            continue
        if msg.get("type") == "infer.data.v1":
            rid = msg.get("roi_id") or f"roi-{processed:06x}"
            b64 = msg.get("image_b64")
            img_path = msg.get("image_path")
            decode = msg.get("decode", {})
            session_id = msg.get("session_id") or "sess-00000000-x"
            try:
                max_new_tokens = int(decode.get("max_new_tokens", MAX_TOKENS))
            except (TypeError, ValueError):
                max_new_tokens = MAX_TOKENS
            override = session_overrides.get(session_id)
            if override and override.get("max_tokens"):
                max_new_tokens = max(max_new_tokens, override["max_tokens"])
            compact = bool(msg.get("compact", True))
            payer = msg.get("payer_hint")
            budget = msg.get("budget") or {}
            header_lock = msg.get("header_lock") or {}
            columns = header_lock.get("canonical_order") if isinstance(header_lock, dict) else None
            image_sha = str(msg.get("image_sha256") or "")
            text = ""
            raw_slice = None
            request_sha = ""
            prompt_sha = ""
            try:
                if b64:
                    im = decode_image(b64)
                elif img_path:
                    im = Image.open(img_path).convert("RGB")
                else:
                    raise RuntimeError("missing image")
                prompt = make_data_prompt(header_lock, payer, compact)
                prompt_sha = compute_prompt_sha(prompt)
                t1 = time.time()
                text = generate_text(MODEL, PROCESSOR, CFG, im, prompt, max_new_tokens)
                parsed, raw_slice = extract_json(text)
                sec = time.time() - t1; processed += 1
                request_meta = {
                    "session_id": session_id,
                    "roi_id": rid,
                    "stage": "data",
                    "prompt_sha256": prompt_sha,
                    "image_sha256": image_sha,
                    "model": CURRENT_MODEL_ID,
                    "max_new_tokens": max_new_tokens,
                    "header_lock_sha256": header_lock.get("lock_sha256") if isinstance(header_lock, dict) else None,
                }
                request_sha = sha256_bytes(canon(request_meta).encode("utf-8"))
                rows = 0
                cols = 0
                normalized_cells: list[Dict[str, Any]] = []
                status = "invalid"
                reason_tag = None
                if parsed is None:
                    slice_source = raw_slice or text or ""
                    details = {
                        "slice": slice_source[:256],
                        "slice_sha256": sha256_bytes(slice_source.encode("utf-8")),
                        "request_sha256": request_sha,
                        "prompt_sha256": prompt_sha,
                    }
                    _log_raw_output(session_id, rid, "data", "parse_error", text or "", details)
                    reason_tag = "parse_error"
                else:
                    try:
                        rows, cols, normalized_cells = normalize_table(parsed)
                        valid, tier = valid_table(rows, cols, normalized_cells)
                        if valid:
                            canonical_order = []
                            if isinstance(header_lock, dict):
                                canonical_order = header_lock.get("canonical_order") or header_lock.get("headers") or []
                            if canonical_order:
                                cell_texts = {str(cell.get("text", "")).strip().lower() for cell in normalized_cells if str(cell.get("text", "")).strip()}
                                header_texts = {str(h).strip().lower() for h in canonical_order if str(h).strip()}
                                overlap = cell_texts & header_texts
                                if cell_texts and len(overlap) / max(1, len(cell_texts)) > 0.5:
                                    valid = False
                                    reason_tag = "header_echo"
                                    slice_source = raw_slice or text or ""
                                    details = {
                                        "slice": slice_source[:256],
                                        "slice_sha256": sha256_bytes(slice_source.encode("utf-8")),
                                        "request_sha256": request_sha,
                                        "prompt_sha256": prompt_sha,
                                    }
                                    _log_raw_output(session_id, rid, "data", reason_tag, text or "", details)
                                    rows = 0
                                    cols = 0
                                    normalized_cells = []
                                    status = "invalid"
                                else:
                                    status = "ok"
                            else:
                                status = "ok"
                        else:
                            reason_tag = tier
                            slice_source = raw_slice or text or ""
                            details = {
                                "slice": slice_source[:256],
                                "slice_sha256": sha256_bytes(slice_source.encode("utf-8")),
                                "request_sha256": request_sha,
                                "prompt_sha256": prompt_sha,
                            }
                            _log_raw_output(session_id, rid, "data", reason_tag, text or "", details)
                            rows = 0
                            cols = 0
                            normalized_cells = []
                    except ValueError as norm_err:
                        slice_source = raw_slice or text or ""
                        reason_tag = str(norm_err)
                        details = {
                            "slice": slice_source[:256],
                            "slice_sha256": sha256_bytes(slice_source.encode("utf-8")),
                            "request_sha256": request_sha,
                            "prompt_sha256": prompt_sha,
                        }
                        _log_raw_output(session_id, rid, "data", reason_tag, text or "", details)
                        rows = 0
                        cols = 0
                        normalized_cells = []
                bc = budget.get("class") or "typical"
                est_cells = budget.get("est_cells") or (rows * cols if rows and cols else None)
                est_source = budget.get("est_source") or "vision_lines"
                meta = {
                    "attempts": 1,
                    "response_sha256": sha256_bytes((text or "").encode("utf-8")),
                    "parse_repair": "none",
                    "budget_class": bc,
                    "est_cells": est_cells or 0,
                    "est_source": est_source,
                    "sec": round(sec, 3),
                    "chars_out": len(text or ""),
                    "cells_out": rows * cols,
                    "decode_kwargs_accepted": {"max_new_tokens": max_new_tokens},
                    "runner_mode": "persistent",
                    "model_fingerprint": model_fingerprint_metadata()
                }
                resp = {
                    "type": "result.data.v1",
                    "session_id": session_id,
                    "roi_id": rid,
                    "rows": rows,
                    "cols": cols if cols else (len(columns) if isinstance(columns, list) else 0),
                    "cells": normalized_cells,
                    "rectangularized": bool(status == "ok"),
                    "model_meta": meta,
                    "header_lock_sha256": header_lock.get("lock_sha256") or "",
                    "status": status,
                    "prompt_sha256": prompt_sha,
                    "request_sha256": request_sha,
                }
                resp_sha = sha256_bytes(canon(resp).encode("utf-8"))
                resp["response_sha256"] = resp_sha
                stats = session_stats[session_id]
                stats["total"] += 1
                if status != "ok":
                    stats["invalid"] += 1
                    if reason_tag == "parse_error":
                        stats["parse_error"] += 1
                maybe_escalate(session_id)
            except Exception as e:
                resp = {
                    "type": "result.data.v1",
                    "session_id": session_id,
                    "roi_id": rid,
                    "rows": 0,
                    "cols": 0,
                    "cells": [],
                    "rectangularized": False,
                    "model_meta": {
                        "attempts": 1,
                        "response_sha256": sha256_bytes(b""),
                        "parse_repair": "none",
                        "budget_class": budget.get("class") or "typical",
                        "est_cells": budget.get("est_cells") or 0,
                        "est_source": budget.get("est_source") or "default",
                        "runner_mode": "persistent",
                        "model_fingerprint": model_fingerprint_metadata()
                    },
                    "header_lock_sha256": header_lock.get("lock_sha256") or "",
                    "status": "invalid"
                }
                resp["prompt_sha256"] = prompt_sha
                if request_sha:
                    resp["request_sha256"] = request_sha
                resp["response_sha256"] = sha256_bytes(canon(resp).encode("utf-8"))
                slice_source = text or ""
                details = {
                    "slice": slice_source[:256],
                    "slice_sha256": sha256_bytes(slice_source.encode("utf-8")),
                    "error": str(e)
                }
                if request_sha:
                    details["request_sha256"] = request_sha
                    details["prompt_sha256"] = prompt_sha
                _log_raw_output(session_id, rid, "data", f"exception:{type(e).__name__}", text or "", details)
                stats = session_stats[session_id]
                stats["total"] += 1
                stats["invalid"] += 1
                maybe_escalate(session_id)
            sys.stdout.write(canon(resp)+"\n"); sys.stdout.flush()
            continue
        if msg.get("type") == "infer":
            rid = msg.get("id") or f"roi-{processed}"
            b64 = msg.get("image_b64")
            img_path = msg.get("image_path")
            decode = msg.get("decode", {})
            max_new_tokens = int(decode.get("max_new_tokens", MAX_TOKENS))
            compact = bool(msg.get("compact", True))
            payer = msg.get("payer_hint")
            schema_hint = msg.get("schema_hint") or {}
            columns = schema_hint.get("columns")
            budget = msg.get("budget") or {}
            try:
                if b64:
                    im = decode_image(b64)
                elif img_path:
                    im = Image.open(img_path).convert("RGB")
                    w,h = im.size
                    if w*h > VLM_MAX_PIXELS:
                        import math
                        scale = math.sqrt(VLM_MAX_PIXELS/(w*h))
                        nw, nh = max(1,int(w*scale)), max(1,int(h*scale))
                        im = im.resize((nw, nh))
                else:
                    raise RuntimeError("missing image_b64 and image_path")
                prompt = make_data_prompt(header_lock, payer, compact)
                t1=time.time()
                text = generate_text(MODEL, PROCESSOR, CFG, im, prompt, max_new_tokens)
                parsed, raw_slice = extract_json(text)
                rows = 0
                cols = 0
                normalized_cells: list[Dict[str, Any]] = []
                if parsed is None:
                    snippet = (raw_slice or text)[:256]
                    _log_raw_output(msg.get("session_id"), rid, "legacy", "parse_error", text, {"slice": snippet})
                else:
                    try:
                        rows, cols, normalized_cells = normalize_table(parsed)
                    except ValueError as norm_err:
                        snippet = (raw_slice or text)[:256]
                        _log_raw_output(msg.get("session_id"), rid, "legacy", str(norm_err), text, {"slice": snippet})
                        rows = 0
                        cols = 0
                        normalized_cells = []
                sec = time.time()-t1; processed += 1; total_sec += sec
                # Derive budget info for meta (prefer provided budget block)
                bc = None
                est_cells = None
                est_source = None
                if isinstance(budget, dict):
                    bc = budget.get("class")
                    est_cells = budget.get("est_cells")
                    est_source = budget.get("est_source")
                if bc is None:
                    # Invert token budget approximately when class missing
                    try:
                        est_cells = max(1, min(120, int(round((max_new_tokens - 384) / 10.0))))
                        bc = "small" if est_cells < 36 else ("typical" if est_cells <= 120 else "large")
                    except Exception:
                        bc = None
                        est_cells = None
                meta = {
                    "attempts": 1,
                    "sec": round(sec,3),
                    "decode_kwargs_accepted": {"max_new_tokens": max_new_tokens},
                    "chars_out": len(text or ""),
                    "cells_out": rows*cols,
                    **({"budget_class": bc} if bc is not None else {}),
                    **({"est_cells": est_cells} if est_cells is not None else {}),
                    **({"est_source": est_source} if est_source is not None else {})
                }
                # Emit vlm.response.v1 schema (tables/text_blocks)
                table = {"rows": rows, "cols": cols, "cells": normalized_cells}
                resp = {
                    "schema": "vlm.response.v1",
                    "tables": [table] if rows and cols else [],
                    "text_blocks": [{"text": text}] if text else [],
                    "model_meta": meta
                }
            except Exception as e:
                resp = {"schema": "vlm.response.v1", "tables": [], "text_blocks": [], "model_meta": {"attempts": 1, "error": str(e)}}
            sys.stdout.write(canon(resp)+"\n"); sys.stdout.flush()
        elif msg.get("type") == "close":
            break

    goodbye = {"type":"goodbye","stats":{"roi_count":processed, "avg_sec_per_roi": (total_sec/processed if processed else 0.0)}}
    sys.stdout.write(canon(goodbye)+"\n"); sys.stdout.flush()

if __name__ == "__main__":
    main()
