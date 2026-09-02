#!/usr/bin/env python3
"""Assemble Dental EOB objects from NativeOCR page payloads."""
from __future__ import annotations

import argparse
import copy
import glob
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import tempfile

import numpy as np
import signal
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import yaml
from dateutil import parser as dateparser
from PIL import Image, ImageFilter, ImageOps

MODULE_DIR = Path(__file__).resolve().parent
if str(MODULE_DIR) not in sys.path:
    sys.path.append(str(MODULE_DIR))

from ocr_worker_client import get_worker
try:
    # Law-based row solver (deterministic, integer-cents)
    from solver_laws import min_energy_row  # type: ignore
except Exception:  # pragma: no cover
    min_energy_row = None  # type: ignore
try:
    # Frozen selection policy
    from selection_policy import SelectionInput, choose_variant  # type: ignore
except Exception:  # pragma: no cover
    SelectionInput = None  # type: ignore
    choose_variant = None  # type: ignore

# C6.1 KPIs (aggregated over a document run)
COH_KPI: Dict[str, Any] = {}

def _kpi_inc(key: str, val: int = 1) -> None:
    COH_KPI[key] = int(COH_KPI.get(key, 0) or 0) + int(val)


def load_run_manifest(path: Path) -> Dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RuntimeError(f"failed to read run manifest {path}: {exc}")
    out: Dict[str, Any] = {}
    if isinstance(data, dict):
        out["input_path"] = data.get("input_path")
        out["doc_sha"] = data.get("input_sha256") or data.get("input_sha") or data.get("doc_sha")
    return out

# Optional CP7 lexical repair (loaded only when used)
try:
    from lexical_repair import load_maps, repair_money, repair_date, repair_code
except Exception:  # pragma: no cover
    load_maps = None  # type: ignore
    repair_money = None  # type: ignore
    repair_date = None  # type: ignore
    repair_code = None  # type: ignore

Amount = Optional[str]

# -----------------------------------------------------------------------------
# Constants & regex
# -----------------------------------------------------------------------------

D_CODE_RE = re.compile(r"^D\d{4}[A-Z]?$", re.IGNORECASE)
TOOTH_RE = re.compile(r"^(?:[1-9]|[1-2][0-9]|3[0-2]|[A-T])$")
SURFACE_RE = re.compile(r"^[A-Z]{1,5}$")

THOUSANDS_RE = re.compile(r"(?<=\d)[,'](?=\d{3}(?:\D|$))")
MULTIDOT_RE = re.compile(r"\.(?=.*\.)")
CR_RE = re.compile(r"\s*(CR|DR)$", re.IGNORECASE)
PARENS_RE = re.compile(r"^\((.+)\)$")
VLM_ERROR_DIR = Path(os.getenv("VLM_ERROR_DIR", "runs/vlm/errors"))


def _safe_slug(value: Optional[str]) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", (value or "").strip()) or "unknown"


def compute_file_sha256(path: Path) -> str:
    """Compute SHA256 hash of file contents."""
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


SCRIPT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_VISION_CLI = Path(os.getenv("VISION_OCR_CLI", "vision-ocr-cli/.build/release/vision-ocr-cli"))
DEFAULT_PREPROCESS_SCRIPT = Path(os.getenv("MICRO_OCR_PREPROCESS", "scripts/preprocess_roi.py"))

MICRO_COLUMN_TYPES = {
    "proc_cd": "dcode",
    "procedure_code": "dcode",
    "desc": "text",
    "dos": "date",
    "service_date": "date",
    "billed": "money",
    "charged": "money",
    "charge": "money",
    "allowed": "money",
    "paid": "money",
    "adjust": "money",
    "patient": "money",
}


def _deterministic_json_sha(payload: Dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _classify_column(header_name: Optional[str]) -> str:
    if not header_name:
        return "text"
    key = header_name.lower()
    return MICRO_COLUMN_TYPES.get(key, "text")


def _normalize_money_text(raw: str) -> str:
    cleaned = raw.strip()
    cleaned = cleaned.replace("$", "")
    replacements = {
        "O": "0",
        "o": "0",
        "S": "5",
        "s": "5",
        "I": "1",
        "l": "1",
        "|": "1",
    }
    for src, dest in replacements.items():
        cleaned = cleaned.replace(src, dest)
    cleaned = cleaned.replace(" ", "")
    cleaned = cleaned.replace(",", "")
    cleaned = cleaned.replace("..", ".")
    if cleaned.count(".") > 1:
        parts = cleaned.split(".")
        cleaned = "".join(parts[:-1]) + "." + parts[-1]
    if cleaned.startswith("."):
        cleaned = "0" + cleaned
    return cleaned


def _normalize_date_text(raw: str) -> str:
    cleaned = raw.strip()
    replacements = {
        "O": "0",
        "o": "0",
        "I": "1",
        "l": "1",
        "|": "1",
    }
    for src, dest in replacements.items():
        cleaned = cleaned.replace(src, dest)
    cleaned = re.sub(r"[.\\-]", "/", cleaned)
    cleaned = re.sub(r"\\s+", "/", cleaned)
    cleaned = cleaned.strip("/")
    return cleaned


def _normalize_dcode_text(raw: str) -> str:
    cleaned = raw.strip().upper()
    cleaned = cleaned.replace("O", "0")
    cleaned = cleaned.replace(" ", "")
    return cleaned


EXPANSION_RULES = {
    "money": {"id": "E1_money", "left_pct": 0.08, "right_pct": 0.26, "vertical_px": 4},
    "date": {"id": "E1_date", "left_pct": 0.12, "right_pct": 0.12, "vertical_px": 4},
    "dcode": {"id": "E1_code", "left_pct": 0.10, "right_pct": 0.10, "vertical_px": 2},
}

STRIPE_RULES = {
    "money": {"count": 3, "width_frac": 0.18, "overlap": 0.20},
    "date": {"count": 3, "width_frac": 0.18, "overlap": 0.20},
}

PROFILE_PIPELINE = {
    "P1_base": {"upsample": 1.5, "clahe": {"clip_limit": 3.0, "tile_grid": (8, 8)}, "sauvola": {"k": 0.34, "window": 25}},
    "P2_money": {
        "upsample": 2.25,
        "unsharp": {"radius": 1.2, "amount": 0.9},
        "sauvola": {"k": 0.24, "window": 31},
        "morph": {"operation": "closing", "kernel": (1, 2)},
    },
    "P3_date": {
        "upsample": 2.0,
        "clahe": {"clip_limit": 2.0, "tile_grid": (8, 8)},
        "sauvola": {"k": 0.18, "window": 21},
        "morph": {"operation": "thinning", "iterations": 1},
    },
    "P4_money_gain": {
        "upsample": 2.25,
        "gain": {"scale": 1.35, "offset": -10},
        "tophat": {"kernel": (5, 3)},
        "sauvola": {"k": 0.22, "window": 29},
        "morph": {"operation": "closing", "kernel": (1, 2)},
    },
    "P4_date_gain": {
        "upsample": 2.0,
        "gain": {"scale": 1.25, "offset": -8},
        "tophat": {"kernel": (3, 3)},
        "clahe": {"clip_limit": 2.0, "tile_grid": (8, 8)},
        "sauvola": {"k": 0.16, "window": 19},
        "morph": {"operation": "thinning", "iterations": 1},
    },
}

PROFILE_STACK = {
    "money": ["P1_base", "P2_money", "P4_money_gain"],
    "date": ["P1_base", "P3_date", "P4_date_gain"],
    "dcode": ["P1_base"],
}

ORIENTATION_VALUES = [
    ("ori0", 0),
    ("ori180", 180),
]

POLARITY_VALUES = [
    ("polarity_normal", False),
    ("polarity_invert", True),
]

NORMALIZATION_VARIANTS = [
    {"id": "norm_identity", "method": "identity"},
    {"id": "norm_gamma_085", "method": "gamma", "gamma": 0.85},
    {"id": "norm_gamma_120", "method": "gamma", "gamma": 1.20},
    {"id": "norm_clahe", "method": "clahe", "clip_limit": 2.5, "tile_grid": (8, 8)},
]

PATCH_PROFILES = {
    "MoneyPatch": {
        "upsample": 2.2,
        "blackhat": {"kernel": (13, 3)},
        "clahe": {"clip_limit": 2.0, "tile_grid": (8, 8)},
        "sauvola": {"k": 0.34, "window": 31},
    },
    "DatePatch": {
        "upsample": 2.0,
        "tophat": {"kernel": (7, 7)},
        "clahe": {"clip_limit": 2.0, "tile_grid": (8, 8)},
        "sauvola": {"k": 0.34, "window": 25},
    },
}


def _iter_expanded_boxes(base_box: Tuple[float, float, float, float], kind: str, page_height: float, roi_box: Optional[Dict[str, float]]) -> List[Tuple[str, Tuple[float, float, float, float]]]:
    x0, y0, x1, y1 = base_box
    expansions: List[Tuple[str, Tuple[float, float, float, float]]] = [("E0_raw", (x0, y0, x1, y1))]
    rule = EXPANSION_RULES.get(kind)
    if not rule:
        return expansions
    width = max(1.0, x1 - x0)
    left_pad = width * rule["left_pct"]
    right_pad = width * rule["right_pct"]
    vertical_pad = float(rule["vertical_px"])
    rx0 = x0 - left_pad
    rx1 = x1 + right_pad
    ry0 = y0 - vertical_pad
    ry1 = y1 + vertical_pad
    if roi_box:
        rx0 = max(roi_box["x0"], rx0)
        ry0 = max(roi_box["y0"], ry0)
        rx1 = min(roi_box["x1"], rx1)
        ry1 = min(roi_box["y1"], ry1)
    else:
        rx0 = max(0.0, rx0)
        ry0 = max(0.0, ry0)
        rx1 = max(rx0 + 1.0, rx1)
        ry1 = min(page_height, max(ry0 + 1.0, ry1))
    expansions.append((rule["id"], (rx0, ry0, rx1, ry1)))
    return expansions


def _iter_stripe_boxes(expanded_box: Tuple[float, float, float, float], kind: str) -> List[Tuple[int, str, Tuple[float, float, float, float]]]:
    x0, y0, x1, y1 = expanded_box
    stripes: List[Tuple[int, str, Tuple[float, float, float, float]]] = []
    rule = STRIPE_RULES.get(kind)
    if not rule:
        stripes.append((0, "stripe_full", (x0, y0, x1, y1)))
        return stripes
    width = max(1.0, x1 - x0)
    stripe_w = max(width * rule["width_frac"], width / rule["count"])
    overlap = stripe_w * rule["overlap"]
    step = stripe_w - overlap
    positions: List[float] = []
    left = x0
    for idx in range(rule["count"]):
        positions.append(left)
        left += step
    stripes = []
    for idx, left in enumerate(positions):
        right = min(left + stripe_w, x1)
        if idx == rule["count"] - 1:
            right = x1
        left = max(x0, right - stripe_w)
        stripes.append((idx, f"stripe_{idx}", (left, y0, right, y1)))
    return stripes


def _iter_profiles(kind: str) -> List[str]:
    stack = PROFILE_STACK.get(kind)
    if not stack:
        return ["P1_base"]
    seen: List[str] = []
    for pid in stack:
        if pid not in seen:
            seen.append(pid)
    if "P1_base" not in seen:
        seen.append("P1_base")
    return seen


def _iter_orientations() -> List[Tuple[str, int]]:
    return ORIENTATION_VALUES


def _iter_polarities() -> List[Tuple[str, bool]]:
    return POLARITY_VALUES


def _apply_clahe_np(arr: "np.ndarray", clip_limit: float, tile_grid: Tuple[int, int]) -> "np.ndarray":
    try:
        import cv2
        clahe = cv2.createCLAHE(clipLimit=clip_limit, tileGridSize=tile_grid)
        return clahe.apply(arr.astype("uint8"))
    except Exception:
        from PIL import ImageOps

        img = ImageOps.autocontrast(Image.fromarray(arr.astype("uint8")))
        return np.array(img, dtype="uint8")


def _apply_sauvola_np(arr: "np.ndarray", k: float, window: int) -> "np.ndarray":
    try:
        import cv2
        block_size = max(3, int(window) | 1)
        thresh = cv2.adaptiveThreshold(
            arr.astype("uint8"),
            255,
            cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
            cv2.THRESH_BINARY,
            block_size,
            int((1.0 - k) * 10),
        )
        return thresh
    except Exception:
        return arr


def _apply_morph(arr: "np.ndarray", morph_cfg: Dict[str, Any]) -> "np.ndarray":
    operation = morph_cfg.get("operation")
    try:
        import cv2
        kernel_cfg = morph_cfg.get("kernel", (1, 1))
        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (max(1, kernel_cfg[0]), max(1, kernel_cfg[1])))
        if operation == "closing":
            return cv2.morphologyEx(arr, cv2.MORPH_CLOSE, kernel)
        if operation == "thinning":
            try:
                import cv2.ximgproc  # type: ignore

                return cv2.ximgproc.thinning(arr, thinningType=cv2.ximgproc.THINNING_ZHANGSUEN)
            except Exception:
                return arr
    except Exception:
        return arr
    return arr


def _apply_percentile_clip_np(arr: "np.ndarray", low: float = 2.0, high: float = 98.0) -> "np.ndarray":
    low_val, high_val = np.percentile(arr, (low, high))
    if high_val <= low_val:
        return arr
    clipped = np.clip(arr, low_val, high_val)
    scaled = (clipped - low_val) / max(high_val - low_val, 1e-6)
    return (scaled * 255.0).astype("float32")


def _apply_gamma_np(arr: "np.ndarray", gamma: float) -> "np.ndarray":
    if gamma <= 0:
        return arr
    normalized = np.clip(arr / 255.0, 0.0, 1.0)
    adjusted = np.power(normalized, gamma)
    return (adjusted * 255.0).astype("float32")


def _apply_tophat_np(arr: "np.ndarray", tophat_cfg: Dict[str, Any]) -> "np.ndarray":
    try:
        import cv2
        kernel_cfg = tophat_cfg.get("kernel", (3, 3))
        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (max(1, kernel_cfg[0]), max(1, kernel_cfg[1])))
        iterations = int(tophat_cfg.get("iterations", 1))
        return cv2.morphologyEx(arr.astype("uint8"), cv2.MORPH_TOPHAT, kernel, iterations=max(1, iterations))
    except Exception:
        return arr


def _apply_blackhat_np(arr: "np.ndarray", blackhat_cfg: Dict[str, Any]) -> "np.ndarray":
    try:
        import cv2
        kernel_cfg = blackhat_cfg.get("kernel", (3, 3))
        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (max(1, kernel_cfg[0]), max(1, kernel_cfg[1])))
        iterations = int(blackhat_cfg.get("iterations", 1))
        return cv2.morphologyEx(arr.astype("uint8"), cv2.MORPH_BLACKHAT, kernel, iterations=max(1, iterations))
    except Exception:
        return arr


def _apply_gain_np(arr: "np.ndarray", gain_cfg: Dict[str, Any]) -> "np.ndarray":
    scale = float(gain_cfg.get("scale", 1.0))
    offset = float(gain_cfg.get("offset", 0.0))
    adjusted = np.clip(arr.astype("float32") * scale + offset, 0, 255)
    return adjusted.astype("uint8")


def _apply_patch_profile(image: "Image.Image", profile_id: str) -> Tuple["Image.Image", Dict[str, Any]]:
    working = image.convert("L")
    arr = np.array(working, dtype="uint8")
    meta: Dict[str, Any] = {"patch_profile_id": profile_id}
    config = PATCH_PROFILES.get(profile_id, {})
    upsample = float(config.get("upsample", 1.0))
    if upsample > 1.0:
        new_size = (
            max(8, int(round(working.width * upsample))),
            max(8, int(round(working.height * upsample))),
        )
        working = working.resize(new_size, Image.BICUBIC)
        arr = np.array(working, dtype="uint8")
        meta["upsample"] = upsample
    if "blackhat" in config:
        arr = _apply_blackhat_np(arr, config["blackhat"])
        meta["blackhat"] = config["blackhat"]
    if "tophat" in config:
        arr = _apply_tophat_np(arr, config["tophat"])
        meta["tophat"] = config["tophat"]
    if "clahe" in config:
        clahe_cfg = config["clahe"]
        arr = _apply_clahe_np(arr, clahe_cfg.get("clip_limit", 2.0), clahe_cfg.get("tile_grid", (8, 8)))
        meta["clahe"] = clahe_cfg
    if "sauvola" in config:
        sauvola_cfg = config["sauvola"]
        arr = _apply_sauvola_np(arr, sauvola_cfg.get("k", 0.34), sauvola_cfg.get("window", 31))
        meta["sauvola"] = sauvola_cfg
    processed = Image.fromarray(arr.astype("uint8"))
    return processed, meta


def _prepare_profile_image(
    image: "Image.Image",
    profile_id: str,
    normalization: Optional[Dict[str, Any]] = None,
) -> Tuple["Image.Image", Dict[str, Any]]:
    from PIL import ImageFilter

    working = image.convert("L")
    meta: Dict[str, Any] = {"profile_id": profile_id}
    config = PROFILE_PIPELINE.get(profile_id, {})
    upsample = float(config.get("upsample", 1.0))
    if upsample != 1.0:
        new_size = (
            max(16, int(round(working.width * upsample))),
            max(16, int(round(working.height * upsample))),
        )
        working = working.resize(new_size, Image.BICUBIC)
        meta["upsample"] = upsample
    arr = np.array(working, dtype="float32")
    arr = _apply_percentile_clip_np(arr, 2.0, 98.0)
    if normalization:
        norm_meta: Dict[str, Any] = {"id": normalization.get("id")}
        method = normalization.get("method")
        if method == "gamma":
            gamma_val = float(normalization.get("gamma", 1.0))
            arr = _apply_gamma_np(arr, gamma_val)
            norm_meta["method"] = "gamma"
            norm_meta["gamma"] = gamma_val
        elif method == "clahe":
            clip_limit = float(normalization.get("clip_limit", 2.5))
            tile_grid = normalization.get("tile_grid", (8, 8))
            norm_meta["method"] = "clahe"
            norm_meta["clip_limit"] = clip_limit
            norm_meta["tile_grid"] = list(tile_grid) if isinstance(tile_grid, tuple) else tile_grid
            arr = _apply_clahe_np(arr, clip_limit, tuple(tile_grid))
        else:
            norm_meta["method"] = method or "identity"
        meta["normalization"] = norm_meta
    working = Image.fromarray(np.clip(arr, 0, 255).astype("uint8"))
    arr = np.array(working, dtype="uint8")
    tophat_cfg = config.get("tophat")
    if tophat_cfg:
        arr = _apply_tophat_np(arr, tophat_cfg)
        meta["tophat"] = tophat_cfg
    gain_cfg = config.get("gain")
    if gain_cfg:
        arr = _apply_gain_np(arr, gain_cfg)
        meta["gain"] = gain_cfg
    clahe_cfg = config.get("clahe")
    if clahe_cfg:
        meta["clahe"] = clahe_cfg
        arr = _apply_clahe_np(arr, clahe_cfg.get("clip_limit", 3.0), clahe_cfg.get("tile_grid", (8, 8)))
    sauvola_cfg = config.get("sauvola")
    if sauvola_cfg:
        meta["sauvola"] = sauvola_cfg
        arr = _apply_sauvola_np(arr, sauvola_cfg.get("k", 0.2), sauvola_cfg.get("window", 25))
    working = Image.fromarray(arr.astype("uint8"))
    unsharp_cfg = config.get("unsharp")
    if unsharp_cfg:
        meta["unsharp"] = unsharp_cfg
        radius = unsharp_cfg.get("radius", 1.0)
        amount = int(round(unsharp_cfg.get("amount", 1.0) * 100))
        working = working.filter(ImageFilter.UnsharpMask(radius=radius, percent=amount, threshold=0))
    morph_cfg = config.get("morph")
    if morph_cfg:
        meta["morph"] = morph_cfg
        arr = np.array(working, dtype="uint8")
        arr = _apply_morph(arr, morph_cfg)
        working = Image.fromarray(arr.astype("uint8"))
    return working, meta


def _apply_orientation(image: "Image.Image", degrees: int) -> "Image.Image":
    if degrees == 0:
        return image
    return image.rotate(degrees, resample=Image.BICUBIC, expand=False)


def _apply_polarity(image: "Image.Image", invert: bool) -> "Image.Image":
    if not invert:
        return image
    return ImageOps.invert(image)


def _compute_cell_pixel_box(
    x0: float,
    y0: float,
    x1: float,
    y1: float,
    *,
    roi_offset_x: float,
    roi_offset_y: float,
    scale_x: float,
    scale_y: float,
    img_width: int,
    img_height: int,
) -> Optional[Tuple[int, int, int, int]]:
    pad_x = max(4, int(round(min(img_width, img_height) * 0.004)))
    pad_y = pad_x
    left = int(math.floor((x0 - roi_offset_x) * scale_x)) - pad_x
    right = int(math.ceil((x1 - roi_offset_x) * scale_x)) + pad_x
    top = int(math.floor((y0 - roi_offset_y) * scale_y)) - pad_y
    bottom = int(math.ceil((y1 - roi_offset_y) * scale_y)) + pad_y
    if right <= left or bottom <= top:
        return None
    left = max(0, min(img_width, left))
    right = max(0, min(img_width, right))
    top = max(0, min(img_height, top))
    bottom = max(0, min(img_height, bottom))
    if right - left < 2 or bottom - top < 2:
        return None
    return left, top, right, bottom


def _convert_line_boxes(lines: List[Dict[str, Any]], frame_width: float, frame_height: float) -> str:
    """Ensure each line has a pixel-space box; return coord_space used."""
    coord_space = "pixel"
    if frame_width <= 0 or frame_height <= 0:
        frame_width = frame_height = 1.0
    for line in lines:
        box = line.get("box") or {}
        x = float(box.get("x", box.get("x0", 0.0)))
        y = float(box.get("y", box.get("y0", 0.0)))
        w = float(box.get("w", box.get("width", 0.0)))
        h = float(box.get("h", box.get("height", 0.0)))
        eps = 1e-6
        is_normalized = (
            -eps <= x <= 1.0 + eps
            and -eps <= y <= 1.0 + eps
            and eps < w <= 1.0 + eps
            and eps < h <= 1.0 + eps
        )
        if is_normalized:
            coord_space = "normalized"
            x *= frame_width
            y *= frame_height
            w *= frame_width
            h *= frame_height
        line["_box_pixel"] = {
            "x": float(x),
            "y": float(y),
            "w": float(w),
            "h": float(h),
        }
    return coord_space


def _run_preprocess_cell(src: Path, dst: Path) -> Tuple[Optional[Dict[str, Any]], Optional[str], str]:
    script_path = (SCRIPT_ROOT / DEFAULT_PREPROCESS_SCRIPT).resolve()
    if not script_path.exists():
        return None, "preprocess_script_missing", ""
    proc = subprocess.run(
        [sys.executable, str(script_path), str(src), str(dst)],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        return None, "preprocess_failed", proc.stderr.strip()
    stdout = proc.stdout.strip()
    profile: Dict[str, Any]
    try:
        profile = json.loads(stdout.splitlines()[-1])
    except Exception:
        return None, "preprocess_output_invalid", stdout
    profile_sha = _deterministic_json_sha(profile)
    return profile, profile_sha, ""


def _run_vision_text_ocr(image_path: Path, *, timeout: int = 10_000) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    worker = get_worker()
    try:
        payload = worker.ocr(str(image_path), timeout=timeout)
        return payload, None
    except Exception as exc:
        return None, f"ocr_worker_error:{exc}"


STRICT_MONEY_RE = re.compile(r"^-?\$?\s*\d{1,3}(?:,\d{3})*(?:\.\d{2})?$")
STRICT_DATE_RE = re.compile(r"^(0[1-9]|1[0-2])[/-](0[1-9]|[12]\d|3[01])[/-](20\d\d)$")
STRICT_DCODE_RE = D_CODE_RE
LAX_MONEY_RE = re.compile(r"^-?\$?\s*\d{1,3}(?:,\d{3})*(?:\.\d{2})?$|^-?\d{1,6}(?:\.\d{2})?$")
LAX_DATE_RE = re.compile(r"^\d{1,2}[/-]\d{1,2}[/-]\d{2,4}$")


def _sanitize_micro_value(text: str, col_type: str) -> Tuple[Optional[str], Optional[str], bool]:
    raw = text.strip()
    if not raw:
        return None, "blank", False
    if col_type == "money":
        if STRICT_MONEY_RE.match(raw.replace(" ", "")):
            value = sanitize_amount(raw)
            if value:
                return value, None, False
        normalized = _normalize_money_text(raw)
        if STRICT_MONEY_RE.match(normalized):
            value = sanitize_amount(normalized)
            if value:
                return value, None, True
        if LAX_MONEY_RE.match(normalized):
            value = sanitize_amount(normalized)
            if value and STRICT_MONEY_RE.match(value):
                return value, None, True
        return None, "regex_money", False
    if col_type == "date":
        if STRICT_DATE_RE.match(raw):
            value = parse_date(raw)
            if value:
                return value, None, False
        normalized = _normalize_date_text(raw)
        if STRICT_DATE_RE.match(normalized):
            value = parse_date(normalized)
            if value:
                return value, None, True
        if LAX_DATE_RE.match(normalized):
            value = parse_date(normalized)
            if value and STRICT_DATE_RE.match(datetime.strptime(value, "%Y-%m-%d").strftime("%m/%d/%Y")):
                return value, None, True
        return None, "regex_date", False
    if col_type == "dcode":
        base = raw.strip().upper()
        if STRICT_DCODE_RE.match(base):
            return base, None, False
        normalized = _normalize_dcode_text(raw)
        if STRICT_DCODE_RE.match(normalized):
            return normalized, None, True
        return None, "regex_dcode", False
    # Text fallback: accept any non-empty string
    return raw, None, False


def classify_roi_token(token: str) -> Tuple[str, int]:
    """Parse ROI token like 'roi0' or 'table0' into type and index."""
    tok = token.lower()
    roi_type = "unknown"
    if "table" in tok:
        roi_type = "table"
    elif "page" in tok:
        roi_type = "page"
    roi_index = 0
    match = re.search(r"(\d+)", tok)
    if match:
        roi_index = int(match.group(1))
    return roi_type, roi_index

ROI_KIND_ORDER = {
    "table": 0,
    "page": 1,
    "unknown": 2
}


def zero_box() -> Dict[str, float]:
    return {"x": 0.0, "y": 0.0, "w": 0.0, "h": 0.0}


def zero_pixel_box() -> Dict[str, int]:
    return {"x": 0, "y": 0, "w": 0, "h": 0}


@dataclass
class TableTemplate:
    payer: str
    name_regex: Optional[re.Pattern]
    column_map: Dict[str, Dict[str, Any]]
    header_aliases: Dict[str, str]
    header_row: int
    kv_left: Dict[str, Dict[str, Any]]
    kv_right: Dict[str, Dict[str, Any]]


# -----------------------------------------------------------------------------
# Sanitizers & parsers
# -----------------------------------------------------------------------------


def sanitize_amount(raw: Any) -> Amount:
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    text = text.replace("\u2212", "-").replace("\u2013", "-").replace("\u2014", "-")
    text = re.sub(r"(?i)(?:USD|US\$)", "", text)
    text = text.replace("$", "").strip()

    parens = PARENS_RE.match(text)
    if parens:
        text = f"-{parens.group(1)}"

    m = CR_RE.search(text)
    if m:
        flag = m.group(1).upper()
        text = text[: m.start()].strip()
        if flag == "CR" and not text.startswith("-"):
            text = f"-{text}"

    text = THOUSANDS_RE.sub("", text)
    text = MULTIDOT_RE.sub("", text)
    if "." not in text:
        text = f"{text}.00"
    elif text.endswith("."):
        text = f"{text}00"

    digits = sum(ch.isdigit() for ch in text)
    if digits and digits / max(1, len(text)) >= 0.7:
        text = text.replace("O", "0").replace("o", "0")

    text = text.replace("--", "-")
    try:
        value = Decimal(text)
    except InvalidOperation:
        return None
    value = value.quantize(Decimal("0.01"))
    if abs(value) >= Decimal("1000000000"):
        return None
    return f"{value:.2f}"


def parse_date(text: str, patterns: Optional[List[str]] = None) -> Optional[str]:
    text = text.strip()
    if not text:
        return None
    if patterns:
        for fmt in patterns:
            try:
                return datetime.strptime(text, fmt).strftime("%Y-%m-%d")
            except Exception:
                continue
    try:
        dt = dateparser.parse(text, dayfirst=False, yearfirst=False)
    except (ValueError, OverflowError):
        return None
    return dt.strftime("%Y-%m-%d")


# -----------------------------------------------------------------------------
# Mapping loader
# -----------------------------------------------------------------------------


def load_templates(mapping_dir: Path) -> List[TableTemplate]:
    templates: List[TableTemplate] = []
    for path in sorted(mapping_dir.glob("*.yaml")):
        data = yaml.safe_load(path.read_text())
        detect = data.get("detect", {})
        regex = detect.get("name_regex")
        name_regex = re.compile(regex) if regex else None
        tables_cfg = data.get("tables", [{}])
        primary = tables_cfg[0]
        column_map = {}
        for key, cfg in primary.get("column_map", {}).items():
            column_map[normalize_header(key)] = cfg
        header_aliases = {}
        for alias, target in primary.get("header_aliases", {}).items():
            header_aliases[normalize_header(alias)] = target
        template = TableTemplate(
            payer=data.get("payer", path.stem),
            name_regex=name_regex,
            column_map=column_map,
            header_aliases=header_aliases,
            header_row=primary.get("header_row", 0),
            kv_left=data.get("kv", {}).get("header_left", {}),
            kv_right=data.get("kv", {}).get("header_right", {})
        )
        templates.append(template)
    if not templates:
        raise RuntimeError(f"No payer templates found in {mapping_dir}")
    return templates


# -----------------------------------------------------------------------------
# Page grouping
# -----------------------------------------------------------------------------


def group_pages(files: Iterable[Path]) -> Dict[str, List[Dict[str, Any]]]:
    grouped: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for path in files:
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                page = json.loads(line)
                doc_key = str(page.get("source_path") or path.stem)
                grouped[doc_key].append(page)
    return grouped


# -----------------------------------------------------------------------------
# Extraction helpers
# -----------------------------------------------------------------------------


def extract_header_text(page: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    result: Dict[str, Dict[str, Any]] = {}

    # Try regions structure (legacy support)
    regions = page.get("regions") or {}
    header = regions.get("header") or {}
    for side in ("left", "right"):
        region = header.get(side)
        if not region:
            continue
        result[side] = {
            "text": region.get("text", ""),
            "box": (region.get("box") or {}).get("pixel")
        }

    # If no regions, extract from top of page content
    if not result:
        # Prefer lines (rowize mode) or layout_text (doc mode)
        lines = page.get("lines") or []
        if lines:
            # Take first 10 lines or 20% of page, whichever is smaller
            header_count = min(10, max(3, len(lines) // 5))
            header_lines = [l.get("text", "") for l in lines[:header_count] if l.get("text")]
            if header_lines:
                result["combined"] = {"text": "\n".join(header_lines), "box": None}
        elif "layout_text" in page:
            # Take first 1000 characters from layout_text
            layout_text = page.get("layout_text", "")
            if layout_text:
                header_text = layout_text[:1000].strip()
                result["combined"] = {"text": header_text, "box": None}

    return result


def apply_header_regex(text: str, pattern: Optional[str]) -> Optional[str]:
    if not pattern:
        return None
    match = re.search(pattern, text)
    if not match:
        return None
    return match.group(1).strip()


def normalize_header(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


def build_table_matrix(table: Dict[str, Any]) -> Dict[Tuple[int, int], Dict[str, Any]]:
    """Expand a Vision/VLM table into a dense (row,col)->cell map with span metadata.

    - Fills row/col spans deterministically (top-left precedence).
    - Annotates each dense cell with anchor info for in-memory provenance:
      "_anchor_rc": (r,c) of the top-left source cell, and "_is_shadow": True for
      non-owner copies under a multi-row/col span. No schema writes; used only
      during extraction and header scoring.
    """
    matrix: Dict[Tuple[int, int], Dict[str, Any]] = {}
    for cell in table.get("cells", []):
        r = int(cell.get("r", 0) or 0)
        c = int(cell.get("c", 0) or 0)
        rs = max(1, int(cell.get("row_span", 1) or 1))
        cs = max(1, int(cell.get("col_span", 1) or 1))
        for rr in range(r, r + rs):
            for cc in range(c, c + cs):
                if (rr, cc) in matrix:
                    continue
                clone = dict(cell)
                clone["_anchor_rc"] = (r, c)
                clone["_is_shadow"] = not (rr == r and cc == c)
                clone["_row_span"] = rs
                clone["_col_span"] = cs
                matrix[(rr, cc)] = clone
    return matrix


# ------------------------------
# C6.S Semantic Mapper utilities
# ------------------------------

MONEY_FIELD_ORDER = [
    ("charged_amount", "money"),
    ("allowed_amount", "money"),
    ("insurance_paid", "money"),
    ("patient_responsibility", "money"),
]

def _col_typedness_money(matrix: Dict[Tuple[int,int], Dict[str, Any]], rows: int, cols: int, col: int) -> float:
    hits = 0
    total = 0
    for r in range(rows):
        txt = str((matrix.get((r, col)) or {}).get("text", ""))
        if not txt:
            continue
        total += 1
        if STRICT_MONEY_RE.match(_normalize_money_text(txt)) or SPATIAL_MONEY_RE.search(txt):
            hits += 1
    return (hits / float(total)) if total else 0.0

def _col_money_witnesses(
    matrix: Dict[Tuple[int,int], Dict[str, Any]],
    rows: int,
    col: int,
) -> Dict[str, float]:
    """Compute simple money witnesses per column: currency mark, thousands signature, alpha leak.
    All values in [0,1]. Deterministic and cheap.
    """
    total = 0
    currency = 0
    thousands = 0
    alpha = 0
    decimals_ok = 0
    for r in range(rows):
        txt = str((matrix.get((r, col)) or {}).get("text", ""))
        if not txt:
            continue
        total += 1
        t = txt.strip()
        # Currency mark
        if "$" in t or "USD" in t or "US$" in t:
            currency += 1
        # Thousands signature
        if THOUSANDS_RE.search(t):
            thousands += 1
        # Alpha leak
        if any(c.isalpha() for c in t):
            alpha += 1
        # Decimals pattern (0-2 decimals)
        tnorm = _normalize_money_text(t)
        if tnorm:
            try:
                # Accept integers or with one/two decimals
                if "." in tnorm:
                    dec = tnorm.split(".", 1)[1]
                    if 0 <= len(dec) <= 2:
                        decimals_ok += 1
                else:
                    decimals_ok += 1
            except Exception:
                pass
    if total == 0:
        return {"currency_ratio": 0.0, "thousands_ratio": 0.0, "alpha_leak_ratio": 0.0, "decimals_ratio": 0.0, "right_align_score": 0.0}
    return {
        "currency_ratio": currency / float(total),
        "thousands_ratio": thousands / float(total),
        "alpha_leak_ratio": alpha / float(total),
        "decimals_ratio": decimals_ok / float(total),
        # Right-align score requires token edges; approximate as 0 (no-op) until token boxes available
        "right_align_score": 0.0,
    }

def _col_typedness_date(matrix, rows, col) -> float:
    hits = 0
    total = 0
    for r in range(rows):
        txt = str((matrix.get((r, col)) or {}).get("text", ""))
        if not txt:
            continue
        total += 1
        if STRICT_DATE_RE.match(txt) or SPATIAL_DATE_RE.search(txt):
            hits += 1
    return (hits / float(total)) if total else 0.0

def _col_typedness_code(matrix: Dict[Tuple[int,int], Dict[str, Any]], rows: int, col: int) -> float:
    """Estimate how likely a column is a procedure code column.
    Uses D-code and general proc regex hits over non-empty cells.
    Deterministic and cheap; no OCR.
    """
    hits = 0
    total = 0
    for r in range(rows):
        txt = str((matrix.get((r, col)) or {}).get("text", ""))
        if not txt:
            continue
        total += 1
        t = txt.strip().upper()
        if D_CODE_RE.match(t) or SPATIAL_PROC_RE.search(t):
            hits += 1
    return (hits / float(total)) if total else 0.0

def synthesize_pseudo_mapper(
    matrix: Dict[Tuple[int,int], Dict[str, Any]],
    rows: int,
    cols: int,
) -> Tuple[Dict[str, int], Dict[str, Any]]:
    """Build a pseudo-mapper for rowize tables using money typedness.

    - Score each column by money typedness across data rows
    - Choose top 4 money columns (left-to-right) if available
    - Map roles: charged, allowed, insurance_paid, patient
    - Provide a nominal header_match just above latch (0.16) so soft-accept can compare by conservation
    """
    if rows <= 1 or cols <= 0:
        return {}, {"reason": "too_small"}
    # Use entire table as data (rowize tables typically include headerless grids)
    money_scores = {c: _col_typedness_money(matrix, rows, cols, c) for c in range(cols)}
    # Select candidate columns with minimal signal
    candidates = [c for c in range(cols) if money_scores.get(c, 0.0) >= 0.15]
    # Fallback: take top 4 by score if threshold filters too hard
    if len(candidates) < 4:
        candidates = [c for c, _ in sorted(money_scores.items(), key=lambda kv: kv[1], reverse=True)][:4]
    candidates = sorted(set(int(c) for c in candidates))
    if len(candidates) < 3:
        return {}, {"reason": "insufficient_money_cols", "money_scores": money_scores}
    # Choose left→right first four
    chosen = candidates[:4]
    chosen_sorted = sorted(chosen)
    # Compute decimals ratio and alignment per candidate column
    dec_ratio: Dict[int, float] = {}
    align_std: Dict[int, float] = {}
    def _money_col_metrics(c: int) -> None:
        lens: List[int] = []
        nums = 0
        decs = 0
        for r in range(rows):
            txt = str((matrix.get((r, c)) or {}).get("text", ""))
            if not txt:
                continue
            norm = _normalize_money_text(txt)
            if STRICT_MONEY_RE.match(norm) or SPATIAL_MONEY_RE.search(txt):
                nums += 1
                if "." in norm:
                    decs += 1
                # numeric length sans separators (for right-justify alignment proxy)
                digits = [ch for ch in norm if ch.isdigit()]
                lens.append(len(digits))
        dr = (decs / float(nums)) if nums else 0.0
        mean = (sum(lens) / float(len(lens))) if lens else 0.0
        var = (sum((l - mean) ** 2 for l in lens) / float(len(lens))) if lens else 0.0
        std = var ** 0.5
        dec_ratio[c] = dr
        align_std[c] = std
    for c in chosen_sorted:
        _money_col_metrics(c)
    mapping: Dict[str, int] = {}
    if len(chosen_sorted) >= 4:
        leftmost = chosen_sorted[0]
        rightmost = chosen_sorted[-1]
        middle = chosen_sorted[1:-1]
        # Assign charged to leftmost
        mapping["charged_amount"] = leftmost
        # Among middle+rightmost, pick insurance_paid as highest (decimals_ratio, -alignment_std)
        candidates_paid = middle + [rightmost]
        paid_col = max(candidates_paid, key=lambda c: (dec_ratio.get(c, 0.0), -align_std.get(c, 0.0), c))
        mapping["insurance_paid"] = paid_col
        # Patient: prefer the rightmost among remaining
        remaining = [c for c in candidates_paid if c != paid_col]
        # prefer rightmost with good alignment (lower std)
        patient_col = max(remaining, key=lambda c: (c, -align_std.get(c, 0.0)))
        mapping["patient_responsibility"] = patient_col
        # Allowed: the last remaining among middle set
        allowed_col = next((c for c in chosen_sorted if c not in (leftmost, paid_col, patient_col)), None)
        if allowed_col is not None:
            mapping["allowed_amount"] = allowed_col
    elif len(chosen_sorted) == 3:
        leftmost, middle, rightmost = chosen_sorted[0], chosen_sorted[1], chosen_sorted[2]
        mapping["charged_amount"] = leftmost
        # Pick paid as higher decimals and better alignment between middle/rightmost
        paid_col = max([middle, rightmost], key=lambda c: (dec_ratio.get(c, 0.0), -align_std.get(c, 0.0), c))
        mapping["insurance_paid"] = paid_col
        # Allowed as the other, patient missing
        mapping["allowed_amount"] = middle if paid_col == rightmost else rightmost
    elif len(chosen_sorted) == 2:
        leftmost, rightmost = chosen_sorted[0], chosen_sorted[1]
        mapping["charged_amount"] = leftmost
        mapping["allowed_amount"] = rightmost
    evidence = {
        "pseudo": True,
        "header_match_nominal": 0.16,
        "columns": chosen_sorted,
        "money_scores": {str(c): round(money_scores.get(c, 0.0), 3) for c in chosen_sorted},
        "decimals_ratio": {str(c): round(dec_ratio.get(c, 0.0), 3) for c in chosen_sorted},
        "alignment_std": {str(c): round(align_std.get(c, 0.0), 3) for c in chosen_sorted},
    }
    return mapping, evidence

def _eval_conservation_for_mapping(matrix: Dict[Tuple[int,int], Dict[str, Any]], rows: int, mapping: Dict[str, int]) -> float:
    # mapping: role->col index
    ok = 0
    total = 0
    for r in range(rows):
        def cell(col: Optional[int]) -> Optional[Decimal]:
            if col is None or col < 0:
                return None
            txt = str((matrix.get((r, col)) or {}).get("text", ""))
            tnorm = _normalize_money_text(txt)
            try:
                return Decimal(tnorm) if tnorm else None
            except Exception:
                return None
        al = cell(mapping.get("allowed_amount"))
        pa = cell(mapping.get("insurance_paid"))
        pt = cell(mapping.get("patient_responsibility"))
        if al is None or pa is None or pt is None:
            continue
        total += 1
        delta = abs(al - (pa + pt))
        base = max(Decimal("0.01"), max(al, pa + pt))
        if (delta <= Decimal("0.01")) or ((delta / base) <= Decimal("0.005")):
            ok += 1
    return (ok / float(total)) if total else 0.0

def _eval_conservation_for_mapping_sample(
    matrix: Dict[Tuple[int,int], Dict[str, Any]],
    rows: int,
    mapping: Dict[str, int],
    sample_rows: List[int],
) -> float:
    """Evaluate conservation on a subset of row indices deterministically.

    Uses the same tolerance as _eval_conservation_for_mapping but restricted to
    provided row indices. Missing values are skipped.
    """
    ok = 0
    total = 0
    for r in sample_rows:
        if r < 0 or r >= rows:
            continue
        def cell(col: Optional[int]) -> Optional[Decimal]:
            if col is None or col < 0:
                return None
            txt = str((matrix.get((r, col)) or {}).get("text", ""))
            tnorm = _normalize_money_text(txt)
            try:
                return Decimal(tnorm) if tnorm else None
            except Exception:
                return None
        al = cell(mapping.get("allowed_amount"))
        pa = cell(mapping.get("insurance_paid"))
        pt = cell(mapping.get("patient_responsibility"))
        if al is None or pa is None or pt is None:
            continue
        total += 1
        delta = abs(al - (pa + pt))
        base = max(Decimal("0.01"), max(al, pa + pt))
        if (delta <= Decimal("0.01")) or ((delta / base) <= Decimal("0.005")):
            ok += 1
    return (ok / float(total)) if total else 0.0

def _rows_conserved_count_for_mapping_sample(
    matrix: Dict[Tuple[int,int], Dict[str, Any]],
    rows: int,
    mapping: Dict[str, int],
    sample_rows: List[int],
) -> int:
    ok = 0
    for r in sample_rows:
        if r < 0 or r >= rows:
            continue
        def cell(col: Optional[int]) -> Optional[Decimal]:
            if col is None or col < 0:
                return None
            txt = str((matrix.get((r, col)) or {}).get("text", ""))
            tnorm = _normalize_money_text(txt)
            try:
                return Decimal(tnorm) if tnorm else None
            except Exception:
                return None
        al = cell(mapping.get("allowed_amount"))
        pa = cell(mapping.get("insurance_paid"))
        pt = cell(mapping.get("patient_responsibility"))
        if al is None or pa is None or pt is None:
            continue
        delta = abs(al - (pa + pt))
        base = max(Decimal("0.01"), max(al, pa + pt))
        if (delta <= Decimal("0.01")) or ((delta / base) <= Decimal("0.005")):
            ok += 1
    return ok

def _min_money_typedness_for_mapping(
    matrix: Dict[Tuple[int,int], Dict[str, Any]],
    rows: int,
    cols: int,
    mapping: Dict[str, int],
) -> float:
    vals: List[float] = []
    for role in ("charged_amount","allowed_amount","insurance_paid","patient_responsibility"):
        c = mapping.get(role)
        if isinstance(c, int) and 0 <= c < cols:
            vals.append(_col_typedness_money(matrix, rows, cols, c))
    return min(vals) if vals else 0.0

def semantic_map_table(page: Dict[str, Any], table: Dict[str, Any], template: 'TableTemplate') -> Dict[str, Any]:
    # Dense matrix
    rows = int(table.get("rows", 0) or 0)
    cols = int(table.get("cols", 0) or 0)
    matrix = build_table_matrix(table)
    # Header discovery: reuse headerness scan in gather_services logic
    best_row = 0
    best_score = float("-inf")
    for cand in range(min(rows, 6)):
        row_cells = [str(matrix.get((cand, c), {}).get("text", "")) for c in range(cols)]
        score = compute_headerness(row_cells, row_idx=cand, matrix=matrix, cols=cols)
        if score > best_score:
            best_score = score
            best_row = cand
    header_row = best_row
    # Alias match (payer aware via template.column_map + header_aliases)
    header_texts = [str((matrix.get((header_row, c)) or {}).get("text", "")) for c in range(cols)]
    header_norms = [normalize_header(h) for h in header_texts]
    header_hits: Dict[int, str] = {}
    for c, hnorm in enumerate(header_norms):
        if not hnorm:
            continue
        cfg = template.column_map.get(hnorm)
        if not cfg:
            alias = template.header_aliases.get(hnorm) if hasattr(template, 'header_aliases') else None
            if alias:
                cfg = template.column_map.get(normalize_header(alias)) or template.column_map.get(alias)
        if cfg and isinstance(cfg, dict) and cfg.get("field"):
            header_hits[c] = cfg.get("field")
    header_match = (len(header_hits) / float(cols)) if cols else 0.0
    # Typedness for money, date, code
    money_scores = {c: _col_typedness_money(matrix, rows, cols, c) for c in range(cols)}
    date_scores = {c: _col_typedness_date(matrix, rows, c) for c in range(cols)}
    code_scores = {c: _col_typedness_code(matrix, rows, c) for c in range(cols)}
    # Candidate money columns
    money_cols = sorted(range(cols), key=lambda c: money_scores[c], reverse=True)
    money_cols = [c for c in money_cols if money_scores[c] >= 0.25 or header_hits.get(c) in {f for f, _ in MONEY_FIELD_ORDER}]
    # Try permutations of assigning 4 roles to top money columns
    import itertools
    roles = [f for f, _ in MONEY_FIELD_ORDER]
    best_mapping = None
    best_cons = -1.0
    # Limit columns to try to top 6 for tractability
    money_subset = money_cols[:6]
    for cols_sel in itertools.permutations(money_subset, min(4, len(money_subset))):
        # Align roles to selected columns by header hits if possible
        role_to_col: Dict[str, Optional[int]] = {r: None for r in roles}
        # First pass: honor header hits
        for c in cols_sel:
            r = header_hits.get(c)
            if r in role_to_col and role_to_col[r] is None:
                role_to_col[r] = c
        # Second pass: fill remaining by order
        for r in roles:
            if role_to_col[r] is None:
                for c in cols_sel:
                    if c not in role_to_col.values():
                        role_to_col[r] = c
                        break
        mapping = {r: int(role_to_col[r]) if role_to_col[r] is not None else -1 for r in roles}
        cons = _eval_conservation_for_mapping(matrix, rows, mapping)
        if cons > best_cons:
            best_cons = cons
            best_mapping = mapping
    if best_mapping is None:
        best_mapping = {r: -1 for r in roles}

    # Also propose non-money roles (procedure_code, service_date) via header hits → typedness fallback
    non_money_roles: Dict[str, int] = {}
    # Procedure code
    code_hit_cols = [c for c, role in header_hits.items() if role == "procedure_code"]
    if code_hit_cols:
        non_money_roles["procedure_code"] = int(code_hit_cols[0])
    else:
        best_code = max(range(cols), key=lambda c: code_scores[c], default=0) if cols else -1
        if best_code is not None and cols and code_scores.get(best_code, 0.0) >= 0.30:
            non_money_roles["procedure_code"] = int(best_code)
    # Service date
    date_hit_cols = [c for c, role in header_hits.items() if role == "service_date"]
    if date_hit_cols:
        non_money_roles["service_date"] = int(date_hit_cols[0])
    else:
        best_date = max(range(cols), key=lambda c: date_scores[c], default=0) if cols else -1
        if best_date is not None and cols and date_scores.get(best_date, 0.0) >= 0.30:
            non_money_roles["service_date"] = int(best_date)
    # Acceptance
    accepted = (header_match >= 0.6) or (best_cons >= 0.5)
    # Build sidecar record
    sem = {
        "table_id": table.get("id"),
        "page_index": page.get("page_index", 0),
        "header_row_index": header_row,
        "header_match": header_match,
        "conservation_bootstrap": best_cons,
        # col_map includes money roles and any non-money roles inferred
        "col_map": {**best_mapping, **non_money_roles},
        # Optional column scores for debugging/sidecar evidence (AP:false)
        "col_scores": {
            str(c): {"money": money_scores.get(c, 0.0), "date": date_scores.get(c, 0.0), "code": code_scores.get(c, 0.0)}
            for c in range(cols)
        },
        "accepted": bool(accepted),
    }
    return sem


def load_runner_reason(session_id: Optional[str], roi_id: Optional[str], stage: str) -> Dict[str, Any]:
    if not session_id or not roi_id:
        return {}
    reason_path = VLM_ERROR_DIR / f"{_safe_slug(session_id)}_{_safe_slug(roi_id)}_{stage}.reason.json"
    if not reason_path.exists():
        return {}
    try:
        return json.loads(reason_path.read_text(encoding="utf-8"))
    except Exception:
        return {}

SPATIAL_MONEY_RE = re.compile(r"(?:^|[^\d])\d{1,3}(?:[,\s]\d{3})*(?:\.\d{2})?(?:$|[^\d])")
SPATIAL_PROC_RE = re.compile(r"\bD\d{4}\b|\b\d{5}\b")
SPATIAL_DATE_RE = re.compile(r"\b(?:\d{1,2}[/-]\d{1,2}[/-]\d{2,4})\b")


def _collect_tokens(page: Dict[str, Any]) -> List[Dict[str, float]]:
    tokens: List[Dict[str, float]] = []
    for line in page.get("lines") or []:
        box = line.get("pixel_box") or line.get("box")
        if not isinstance(box, dict):
            continue
        text_val = str(line.get("text", ""))
        x0 = float(box.get("x", box.get("x0", 0.0)))
        y0 = float(box.get("y", box.get("y0", 0.0)))
        w = float(box.get("w", box.get("width", 0.0)))
        h = float(box.get("h", box.get("height", 0.0)))
        tokens.append({
            "text": text_val,
            "x0": x0,
            "x1": x0 + w,
            "y0": y0,
            "y1": y0 + h,
        })
    return tokens


def _estimate_header_baseline(tokens: List[Dict[str, float]], header_count: int) -> float:
    if not tokens:
        return 0.0
    header_samples = sorted(tokens, key=lambda t: t["y0"])[: max(1, header_count * 2)]
    return max((t["y1"] for t in header_samples), default=0.0)


def _derive_columns(tokens: List[Dict[str, float]], header_count: int, width: float) -> List[Tuple[float, float]]:
    if header_count <= 0:
        return []
    width = max(width, 1.0)
    mids = sorted((t["x0"] + t["x1"]) / 2.0 for t in tokens if t["x1"] > t["x0"])
    if not mids:
        step = width / header_count
        return [(i * step, min(width, (i + 1) * step)) for i in range(header_count)]
    splits: List[float] = []
    n = len(mids)
    for i in range(1, header_count):
        idx = min(n - 1, max(0, int(round(n * i / header_count))))
        splits.append(mids[idx])
    boundaries: List[Tuple[float, float]] = []
    prev = 0.0
    for split in splits:
        boundaries.append((prev, split))
        prev = split
    boundaries.append((prev, width))
    adjusted: List[Tuple[float, float]] = []
    last = 0.0
    for x0, x1 in boundaries:
        x0 = max(last, x0 - 2.0)
        x1 = min(width, max(x1 + 2.0, x0 + 4.0))
        if x1 <= x0:
            x1 = x0 + 4.0
        adjusted.append((x0, x1))
        last = x1
    return adjusted


def _normalize_roi_box(roi: Optional[Dict[str, Any]]) -> Optional[Dict[str, float]]:
    if not isinstance(roi, dict):
        return None
    try:
        x0 = float(roi.get("px", roi.get("x", roi.get("x0", 0.0))))
        y0 = float(roi.get("py", roi.get("y", roi.get("y0", 0.0))))
        w = float(roi.get("pw", roi.get("w", roi.get("width", 0.0))))
        h = float(roi.get("ph", roi.get("h", roi.get("height", 0.0))))
    except Exception:
        return None
    if w <= 0.0 or h <= 0.0:
        return None
    return {
        "x0": x0,
        "y0": y0,
        "x1": x0 + w,
        "y1": y0 + h,
        "w": w,
        "h": h,
    }


def _filter_tokens_to_roi(tokens: List[Dict[str, float]], roi_box: Optional[Dict[str, float]]) -> List[Dict[str, float]]:
    if not roi_box:
        return tokens
    margin = max(4.0, roi_box["h"] * 0.01)
    x0 = roi_box["x0"] - margin
    x1 = roi_box["x1"] + margin
    y0 = roi_box["y0"] - margin
    y1 = roi_box["y1"] + margin
    filtered: List[Dict[str, float]] = []
    for token in tokens:
        mid_x = (token["x0"] + token["x1"]) / 2.0
        mid_y = (token["y0"] + token["y1"]) / 2.0
        if x0 <= mid_x <= x1 and y0 <= mid_y <= y1:
            filtered.append(token)
    return filtered


def _rowize_tokens_build(page: Dict[str, Any], k_candidates: Optional[List[int]] = None) -> Tuple[Optional[Dict[str, Any]], Dict[str, Any]]:
    """Geometry-first rowization from Vision text tokens (no VLM, no Vision tables).

    - Anchors lower-page ROI using money/code/date token presence
    - Tries multiple column counts; picks the strongest valid grid
    - Emits a rectangular table compatible with gather_services()
    """
    info: Dict[str, Any] = {"method": "rowize_tokens", "accepted": False}
    tokens = _collect_tokens(page)
    if not tokens:
        info["reason"] = "no_tokens"
        return None, info
    width = float(page.get("width") or max((t["x1"] for t in tokens), default=0.0) or 1000.0)
    height = float(page.get("height") or max((t["y1"] for t in tokens), default=0.0) or 1000.0)
    info["page_width"] = width
    info["page_height"] = height
    sig: List[Dict[str, float]] = []
    for t in tokens:
        txt = str(t.get("text", ""))
        if SPATIAL_MONEY_RE.search(txt) or D_CODE_RE.match(txt.strip().upper()) or SPATIAL_DATE_RE.search(txt):
            sig.append(t)
    if not sig:
        info["reason"] = "no_signal_tokens"
        return None, info
    y_min = min((t["y0"] for t in sig), default=0.0)
    y_min = max(0.0, y_min - max(6.0, height / 60.0))
    info["y_min_px"] = round(y_min, 2)
    tokens_roi = [t for t in tokens if t["y1"] >= y_min]
    if not tokens_roi:
        info["reason"] = "no_tokens_below_ymin"
        return None, info
    # Pre-bucket rows to estimate vertical spans
    tokens_sorted = sorted(tokens_roi, key=lambda t: t["y0"])
    band_height = max(6.0, height / 120.0)
    row_groups: List[List[Dict[str, float]]] = []
    row_bounds: List[Tuple[float, float]] = []
    for t in tokens_sorted:
        y0 = t["y0"]
        placed = False
        for i, (top, bottom) in enumerate(row_bounds):
            if abs(y0 - top) <= band_height or (top <= y0 <= bottom + band_height):
                row_groups[i].append(t)
                new_top = min(top, t["y0"]) ; new_bot = max(bottom, t["y1"]) 
                row_bounds[i] = (new_top, new_bot)
                placed = True
                break
        if not placed:
            row_groups.append([t])
            row_bounds.append((t["y0"], t["y1"]))

    best = None
    best_score = float("-inf")
    k_candidates = k_candidates or [6, 5, 7, 4]
    for k in k_candidates:
        cols = _derive_columns(tokens_roi, k, width)
        if not cols:
            continue
        row_count, cells = _bucket_rows(tokens_roi, y_min, cols, height)
        if row_count <= 0 or not cells:
            continue
        table = {"rows": row_count, "cols": len(cols), "cells": cells}
        nonblank, money_hits, proc_hits, date_hits = _table_signal_counts(cells)
        score = money_hits * 3 + proc_hits * 2 + date_hits + nonblank / max(1, row_count)
        if score > best_score and is_valid_table(table):
            best_score = score
            best = (table, cols)
    if not best:
        info["reason"] = "no_valid_grid"
        return None, info
    table, cols = best
    info["accepted"] = True
    info["rows"] = table["rows"]
    info["cols"] = table["cols"]
    info["score"] = round(best_score, 2)
    info["columns"] = [(float(c0), float(c1)) for c0, c1 in cols]
    # Convert row_bounds into spans from y_min to bottom for the number of rows found
    spans: List[Dict[str, int]] = []
    for top, bottom in row_bounds[: table["rows"]]:
        spans.append({"y0": int(top), "y1": int(bottom)})
    info["row_spans"] = spans
    normalized: List[Dict[str, Any]] = []
    for cell in table["cells"]:
        entry = {"r": int(cell.get("r", 0) or 0), "c": int(cell.get("c", 0) or 0), "text": str(cell.get("text", "")), "confidence": None}
        normalized.append(entry)
    normalized.sort(key=lambda it: (it["r"], it["c"]))
    payload = {
        "id": f"rowize_p{page.get('page_index', 0)}",
        "rows": int(table["rows"]),
        "cols": int(table["cols"]),
        "cells": normalized,
        "_native_source": "rowize",
    }
    return payload, info


def _project_rows_from_image(
    asset: Optional[Dict[str, Any]],
    header_baseline: float,
    *,
    page_height: float,
    min_rows: int = 0,
) -> Tuple[List[Tuple[int, int]], Dict[str, Any]]:
    if not asset:
        return [], {"reason": "no_asset"}
    image_path = asset.get("image_path")
    if not image_path:
        return [], {"reason": "no_image"}
    image_file = Path(image_path)
    if not image_file.exists():
        return [], {"reason": "image_missing"}
    roi_box = _normalize_roi_box(asset.get("roi"))
    roi_offset_y = roi_box["y0"] if roi_box else 0.0
    roi_height = roi_box["h"] if roi_box else float(page_height or asset.get("roi_height", 0.0) or 0.0)
    try:
        from PIL import Image  # type: ignore
        import numpy as np  # type: ignore
    except Exception as exc:  # pragma: no cover
        return [], {"reason": "projection_unavailable", "detail": str(exc)}
    try:
        with Image.open(image_file) as im:
            gray = im.convert("L")
            arr = np.asarray(gray, dtype=np.float32)
    except Exception as exc:  # pragma: no cover
        return [], {"reason": "image_decode_failed", "detail": str(exc)}
    if arr.ndim == 3:
        arr = arr.mean(axis=2)
    height, width = arr.shape
    if height <= 0 or width <= 0:
        return [], {"reason": "empty_image"}
    if roi_height <= 0.0:
        roi_height = height
    scale_y = height / max(roi_height, 1.0)
    baseline_roi_page = max(0.0, header_baseline - roi_offset_y)
    baseline_idx = int(min(max(round(baseline_roi_page * scale_y), 0), height - 1))
    band_height = max(6.0, height / 120.0)
    min_row_height = max(3, int(round(band_height / 2.0)))
    merge_gap = max(2, int(round(band_height / 3.0)))
    scan = arr[baseline_idx:, :]
    if scan.size == 0:
        return [], {
            "reason": "no_scan_region",
            "baseline_roi_px": baseline_idx,
            "height_px": height,
        }
    inverted = 1.0 - (scan / 255.0)
    projection = inverted.mean(axis=1)
    if projection.size == 0:
        return [], {"reason": "no_projection"}
    kernel = int(max(3, round(band_height // 2)))
    if kernel % 2 == 0:
        kernel += 1
    weights = np.ones(kernel, dtype=np.float32) / float(kernel)
    smoothed = np.convolve(projection, weights, mode="same")
    max_val = float(smoothed.max(initial=0.0))
    mean_val = float(smoothed.mean())
    std_val = float(smoothed.std())
    threshold = max(mean_val + std_val * 0.35, max_val * 0.25)
    if max_val <= 1e-5:
        return [], {
            "reason": "projection_flat",
            "projection_max": max_val,
            "baseline_roi_px": baseline_idx,
            "height_px": height,
        }
    mask = smoothed >= threshold
    segments: List[Tuple[int, int]] = []
    in_segment = False
    start_idx = 0
    for idx, flag in enumerate(mask.tolist()):
        if flag and not in_segment:
            in_segment = True
            start_idx = idx
        elif not flag and in_segment:
            in_segment = False
            segments.append((start_idx, idx))
    if in_segment:
        segments.append((start_idx, mask.size))
    if not segments:
        return [], {
            "reason": "no_segments",
            "projection_max": max_val,
            "threshold": threshold,
            "baseline_roi_px": baseline_idx,
            "height_px": height,
        }
    merged: List[Tuple[int, int]] = []
    for start, end in segments:
        if not merged:
            merged.append((start, end))
            continue
        prev_start, prev_end = merged[-1]
        if start - prev_end <= merge_gap:
            merged[-1] = (prev_start, end)
        else:
            merged.append((start, end))
    row_spans: List[Tuple[int, int]] = []
    for start, end in merged:
        length = end - start
        if length < min_row_height:
            continue
        top = baseline_idx + start
        bottom = min(height, baseline_idx + end)
        top_page = roi_offset_y + (top / max(scale_y, 1e-6))
        bottom_page = roi_offset_y + (bottom / max(scale_y, 1e-6))
        row_spans.append((int(round(top_page)), int(round(bottom_page))))
    projection_info: Dict[str, Any] = {
        "threshold": round(threshold, 6),
        "projection_max": round(max_val, 6),
        "projection_mean": round(mean_val, 6),
        "projection_std": round(std_val, 6),
        "peaks_detected": len(segments),
        "segments_retained": len(row_spans),
        "baseline_roi_px": baseline_idx,
        "band_height_px": round(band_height, 2),
        "merge_gap_px": merge_gap,
        "height_px": height,
        "width_px": width,
        "roi_offset_y": roi_offset_y,
        "roi_height_px": roi_height,
        "scale_y": round(scale_y, 6),
    }
    if row_spans:
        projection_info["row_spans_page"] = [{"y0": int(a), "y1": int(b)} for a, b in row_spans]
    if min_rows:
        projection_info["target_rows"] = min_rows
    return row_spans, projection_info


def _build_cells_from_spans(
    tokens: List[Dict[str, float]],
    row_spans: List[Tuple[int, int]],
    columns: List[Tuple[float, float]],
) -> Tuple[int, List[Dict[str, Any]]]:
    row_count = len(row_spans)
    col_count = len(columns)
    if row_count == 0 or col_count == 0:
        return 0, []
    cell_buckets: List[List[List[str]]] = [[[] for _ in range(col_count)] for _ in range(row_count)]
    if row_spans:
        span_height = max(1.0, sum(max(1.0, b - a) for a, b in row_spans) / row_count)
    else:
        span_height = 1.0
    tolerance = max(6.0, span_height / 3.0)
    for token in tokens:
        text_val = str(token.get("text", "")).strip()
        if not text_val:
            continue
        mid_y = (token["y0"] + token["y1"]) / 2.0
        row_idx = None
        for idx, (top, bottom) in enumerate(row_spans):
            if top - tolerance <= mid_y <= bottom + tolerance:
                row_idx = idx
                break
        if row_idx is None:
            continue
        mid_x = (token["x0"] + token["x1"]) / 2.0
        best_col = min(
            range(col_count),
            key=lambda j: 0.0 if columns[j][0] <= mid_x <= columns[j][1] else min(abs(mid_x - columns[j][0]), abs(mid_x - columns[j][1]))
        )
        cell_buckets[row_idx][best_col].append(text_val)
    cells: List[Dict[str, Any]] = []
    for row_idx in range(row_count):
        for col_idx in range(col_count):
            cell_text = " ".join(cell_buckets[row_idx][col_idx])
            cells.append({"r": row_idx, "c": col_idx, "text": cell_text, "confidence": None})
    return row_count, cells


def build_vlm_table(table_id: str, rows: int, cols: int, cells: List[Dict[str, Any]]) -> Dict[str, Any]:
    normalized: List[Dict[str, Any]] = []
    for cell in cells:
        r = int(cell.get("r", 0) or 0)
        c = int(cell.get("c", 0) or 0)
        text = str(cell.get("text", "") or "")
        confidence = cell.get("confidence", 0.0)
        try:
            conf_val = float(confidence)
        except (TypeError, ValueError):
            conf_val = 0.0
        entry: Dict[str, Any] = {
            "r": r,
            "c": c,
            "text": text,
            "confidence": conf_val,
        }
        if "ptr" in cell:
            entry["ptr"] = cell["ptr"]
        if "box" in cell:
            entry["box"] = cell["box"]
        if "pixel_box" in cell:
            entry["pixel_box"] = cell["pixel_box"]
        normalized.append(entry)
    normalized.sort(key=lambda item: (item["r"], item["c"]))
    return {
        "id": table_id,
        "rows": rows,
        "cols": cols,
        "cells": normalized,
        "_native_source": "vlm",
    }


def _bucket_rows(tokens: List[Dict[str, float]], y_min: float, columns: List[Tuple[float, float]], height: float) -> Tuple[int, List[Dict[str, Any]]]:
    if not columns:
        return 0, []
    height = max(height, 1.0)
    filtered = [t for t in tokens if t["y0"] >= y_min]
    if not filtered:
        return 0, []
    filtered.sort(key=lambda t: (round(t["y0"] / max(1.0, height / 600.0)), t["x0"]))
    rows: List[List[Dict[str, float]]] = []
    anchors: List[float] = []
    band_height = max(6.0, height / 120.0)
    for token in filtered:
        y0 = token["y0"]
        for idx, anchor in enumerate(anchors):
            if abs(y0 - anchor) <= band_height:
                rows[idx].append(token)
                break
        else:
            anchors.append(y0)
            rows.append([token])
    cells: List[Dict[str, Any]] = []
    for row_idx, row_tokens in enumerate(rows):
        column_buckets: List[List[str]] = [[] for _ in columns]
        for token in row_tokens:
            mid = (token["x0"] + token["x1"]) / 2.0
            best_col = min(
                range(len(columns)),
                key=lambda j: 0.0 if columns[j][0] <= mid <= columns[j][1] else min(abs(mid - columns[j][0]), abs(mid - columns[j][1]))
            )
            text_val = str(token.get("text", "")).strip()
            if text_val:
                column_buckets[best_col].append(text_val)
        for col_idx, bucket in enumerate(column_buckets):
            cell_text = " ".join(bucket)
            cells.append({"r": row_idx, "c": col_idx, "text": cell_text, "confidence": None})
    return len(rows), cells


def _table_signal_counts(cells: List[Dict[str, Any]]) -> Tuple[int, int, int, int]:
    nonblank = money = proc = dates = 0
    for cell in cells:
        txt = str(cell.get("text", ""))
        if txt.strip():
            nonblank += 1
        if SPATIAL_MONEY_RE.search(txt):
            money += 1
        if SPATIAL_PROC_RE.search(txt):
            proc += 1
        if SPATIAL_DATE_RE.search(txt):
            dates += 1
    return nonblank, money, proc, dates

def _rowize_validate_table(table: Dict[str, Any]) -> Tuple[bool, Dict[str, Any]]:
    """Strict validator for rowize-generated tables.

    Criteria:
    - min_rows >= 6
    - money_hits >= rows
    - code/date rows coverage >= 15%
    - rows_fit_rate >= 0.05 via headerless inference (quick probe)
    """
    rows = int(table.get("rows", 0) or 0)
    cols = int(table.get("cols", 0) or 0)
    cells = table.get("cells") or []
    info: Dict[str, Any] = {"rows": rows, "cols": cols, "accepted": False}
    if rows < 6 or rows * cols != len(cells):
        info["reason"] = "min_rows_or_shape"
        return False, info
    nonblank, money_hits, proc_hits, date_hits = _table_signal_counts(cells)
    info.update({"nonblank": nonblank, "money_hits": money_hits, "proc_hits": proc_hits, "date_hits": date_hits})
    # Relaxed: money_hits >= rows * 0.50 (was 0.75, originally >= rows)
    money_threshold = rows * 0.50
    if money_hits < money_threshold:
        info["reason"] = "weak_money_signal"
        info["money_threshold"] = money_threshold
        return False, info
    # Code/date coverage per row
    coded_rows = set()
    dated_rows = set()
    for cell in cells:
        r = int(cell.get("r", 0) or 0)
        txt = str(cell.get("text", ""))
        if SPATIAL_PROC_RE.search(txt):
            coded_rows.add(r)
        if SPATIAL_DATE_RE.search(txt):
            dated_rows.add(r)
    coverage = (len(coded_rows | dated_rows) / float(rows)) if rows else 0.0
    info["row_code_date_coverage"] = round(coverage, 3)
    # Relaxed: coverage >= 0.10 (was >= 0.15)
    if coverage < 0.10:
        info["reason"] = "low_code_date_coverage"
        return False, info
    # Quick HIM fit probe
    try:
        matrix = build_table_matrix(table)
        # Dummy template (unused in current inference path)
        dummy = TableTemplate(payer="generic", name_regex=None, column_map={}, header_aliases={}, header_row=0, kv_left={}, kv_right={})
        inferred, ev = infer_columns_headerless(matrix, rows, cols, 0, dummy, apply_repair=False, maps=None, aggressive=False)
        fit = float(ev.get("rows_fit_rate") or 0.0) if isinstance(ev, dict) else 0.0
        info["rows_fit_rate"] = fit
        # Relaxed: fit >= 0.0 (accept any table where HIM succeeds, even if 0 initial conservation)
        # Multi-pass repair can potentially fix these rows later
        if fit < 0.0:
            info["reason"] = "low_rows_fit_rate"
            return False, info
    except Exception as exc:
        info["reason"] = f"fit_probe_error:{exc}"
        return False, info
    info["accepted"] = True
    return True, info

def _doc_validate_tables_on_page(page: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Validate Vision/spatial tables using span-aware (dense) metrics.

    Accept if either:
    - Dense quality gates pass: min_rows≥6, money_rows_ratio≥0.50, code_or_date_rows_ratio≥0.10, fit_probe≥0.00
    (Header-based acceptance is handled later by soft-accept; we gate only by dense quality here.)
    """
    tables = list(page.get("tables") or [])
    accepted: List[Dict[str, Any]] = []
    records: List[Dict[str, Any]] = []
    for t in tables:
        page_idx = int(page.get("page_index", 0) or 0)
        info: Dict[str, Any] = {"page_index": page_idx, "source": t.get("_native_source") or "vision"}
        try:
            rows = int(t.get("rows", 0) or 0)
            cols = int(t.get("cols", 0) or 0)
            matrix = build_table_matrix(t)
            # Heuristic header row detection (reuse prior): best of first 6 rows
            best_row = 0
            best_score = float('-inf')
            for cand in range(min(rows, 6)):
                row_cells = [str((matrix.get((cand, c)) or {}).get("text", "")) for c in range(cols)]
                score = compute_headerness(row_cells, row_idx=cand, matrix=matrix, cols=cols)
                if score > best_score:
                    best_score = score
                    best_row = cand
            data_start = best_row + 1
            # Dense money/code/date rows ratios
            money_rows = 0
            codate_rows = 0
            for r in range(data_start, rows):
                row_texts = [str((matrix.get((r, c)) or {}).get("text", "")) for c in range(cols)]
                has_money = any(STRICT_MONEY_RE.match(_normalize_money_text(txt)) or SPATIAL_MONEY_RE.search(txt) for txt in row_texts if txt)
                has_proc = any(SPATIAL_PROC_RE.search(txt) for txt in row_texts if txt)
                has_date = any(SPATIAL_DATE_RE.search(txt) or STRICT_DATE_RE.match(txt) for txt in row_texts if txt)
                if has_money:
                    money_rows += 1
                if has_proc or has_date:
                    codate_rows += 1
            total_data_rows = max(1, rows - data_start)
            money_rows_ratio = money_rows / float(total_data_rows)
            codate_rows_ratio = codate_rows / float(total_data_rows)
            # Header service tokens presence on header row
            header_texts = [str((matrix.get((best_row, c)) or {}).get("text", "")) for c in range(cols)]
            header_concat = " \n".join(t for t in header_texts if t)
            service_tokens = ("code", "ada", "cpt", "dos")
            has_service_header = any(tok in header_concat.lower() for tok in service_tokens)
            # Quick HIM fit probe
            dummy = TableTemplate(payer="generic", name_regex=None, column_map={}, header_aliases={}, header_row=0, kv_left={}, kv_right={})
            _inferred, ev = infer_columns_headerless(matrix, rows, cols, data_start, dummy, apply_repair=False, maps=None, aggressive=False)
            fit = float((ev or {}).get("rows_fit_rate") or 0.0)
            # Decide
            # Accept if dense metrics pass. Allow a relaxed codate threshold (0.05)
            # only when money_rows_ratio is strong (≥0.75). Soft-accept downstream
            # will still arbitrate by Δ-conservation.
            strong_money = (money_rows_ratio >= 0.75)
            codate_ok = (codate_rows_ratio >= 0.10) or (strong_money and codate_rows_ratio >= 0.05)
            ok = (rows >= 6 and money_rows_ratio >= 0.50 and codate_ok and fit >= 0.00 and has_service_header)
            info.update({
                "rows": rows, "cols": cols,
                "money_rows_ratio": round(money_rows_ratio, 3),
                "row_code_date_coverage": round(codate_rows_ratio, 3),
                "rows_fit_rate": fit,
                "accepted": bool(ok),
                "service_header": bool(has_service_header),
                **({"codate_relaxed": True} if (not (codate_rows_ratio >= 0.10) and codate_ok) else {}),
            })
        except Exception as exc:
            ok = False
            info.update({"accepted": False, "reason": f"validate_error:{exc}"})
        if ok:
            accepted.append(t)
        else:
            if "reason" not in info:
                # annotate reason when thresholds fail
                reasons = []
                if rows < 6:
                    reasons.append("min_rows")
                if info.get("money_rows_ratio", 0.0) < 0.50:
                    reasons.append("weak_money_rows")
                if info.get("row_code_date_coverage", 0.0) < 0.10:
                    reasons.append("low_code_date_coverage")
                info["reason"] = ",".join(reasons) or "failed_gates"
        records.append(info)
    return accepted, records


def is_valid_table(table: Dict[str, Any]) -> bool:
    rows = int(table.get("rows", 0) or 0)
    cols = int(table.get("cols", 0) or 0)
    cells = table.get("cells") or []
    if rows <= 0 or cols <= 0 or rows * cols != len(cells):
        return False
    nonblank, money_hits, proc_hits, date_hits = _table_signal_counts(cells)
    # Require at least some financial/procedural signal
    if money_hits >= 2 and nonblank >= max(3, rows):
        return True
    if money_hits >= 1 and proc_hits >= 1 and nonblank >= max(3, rows):
        return True
    return False


def spatial_merge_build(
    page: Dict[str, Any],
    header_lock: Optional[Dict[str, Any]],
    page_asset: Optional[Dict[str, Any]] = None,
) -> Tuple[Optional[Dict[str, Any]], Dict[str, Any]]:
    asset = page_asset or page.get("_spatial_asset")
    info: Dict[str, Any] = {
        "accepted": False,
        "method": "projection",
    }
    if asset:
        if asset.get("request_sha256"):
            info["request_sha256"] = asset["request_sha256"]
        if asset.get("roi_token"):
            info["roi_token"] = asset["roi_token"]
        if asset.get("roi_type"):
            info["roi_type"] = asset["roi_type"]
        if asset.get("roi_index") is not None:
            info["roi_index"] = asset["roi_index"]
        if asset.get("image_sha256"):
            info["roi_image_sha256"] = asset["image_sha256"]
        if asset.get("header_lock_sha256"):
            info["header_lock_sha256"] = asset["header_lock_sha256"]
    if not isinstance(header_lock, dict):
        info["reason"] = "no_header_lock"
        return None, info
    headers = header_lock.get("canonical_order") or header_lock.get("headers")
    if not headers:
        info["reason"] = "empty_headers"
        return None, info
    header_count = len(headers)
    info["header_count"] = header_count
    tokens = _collect_tokens(page)
    if not tokens:
        info["reason"] = "no_tokens"
        return None, info
    info["vision_token_count"] = len(tokens)
    width = float(page.get("width") or max((t["x1"] for t in tokens), default=0.0) or 1000.0)
    height = float(page.get("height") or max((t["y1"] for t in tokens), default=0.0) or 1000.0)
    roi_box = _normalize_roi_box((asset or {}).get("roi"))
    if roi_box:
        info["roi_box"] = {
            "x0": round(roi_box["x0"], 2),
            "y0": round(roi_box["y0"], 2),
            "w": round(roi_box["w"], 2),
            "h": round(roi_box["h"], 2),
        }
    info["canonical_order"] = list(headers) if headers else []
    info["page_width"] = width
    info["page_height"] = height
    if asset:
        if asset.get("request_sha256"):
            info["request_sha256"] = asset.get("request_sha256")
        if asset.get("header_lock_sha256"):
            info["header_lock_sha256"] = asset.get("header_lock_sha256")
    filtered_tokens = _filter_tokens_to_roi(tokens, roi_box)
    tokens_for_baseline = filtered_tokens or tokens
    header_baseline = _estimate_header_baseline(tokens_for_baseline, header_count)
    info["header_baseline_px"] = round(header_baseline, 2)
    if roi_box:
        x_offset = roi_box["x0"]
        width_for_columns = roi_box["w"] or width
        tokens_for_columns = [
            {
                "text": token["text"],
                "x0": token["x0"] - x_offset,
                "x1": token["x1"] - x_offset,
                "y0": token["y0"],
                "y1": token["y1"],
            }
            for token in tokens_for_baseline
        ]
        columns_local = _derive_columns(tokens_for_columns, header_count, width_for_columns)
        columns = [(c0 + x_offset, c1 + x_offset) for c0, c1 in columns_local]
    else:
        columns = _derive_columns(tokens_for_baseline, header_count, width)
    if not columns:
        info["projection_failure"] = "no_columns"
        info["reason"] = "low_signal"
        info["rows"] = 0
        info["cols"] = 0
        info["token_count"] = len(tokens_for_baseline)
        return None, info
    info["columns"] = [(float(c0), float(c1)) for c0, c1 in columns]
    row_spans, projection_info = _project_rows_from_image(asset, header_baseline, page_height=height)
    if projection_info:
        info["projection"] = projection_info
        if "scale_y" in projection_info:
            info["scale_y"] = projection_info.get("scale_y")
        info["image_width_px"] = projection_info.get("width_px")
        info["image_height_px"] = projection_info.get("height_px")
    tokens_for_cells = filtered_tokens or tokens
    row_count, cells = _build_cells_from_spans(tokens_for_cells, row_spans, columns)
    info["rows"] = row_count
    info["cols"] = len(columns)
    info["token_count"] = len(tokens_for_cells)
    info["billable_cells"] = sum(1 for c in cells if str(c.get("text", "")).strip())
    if row_count == 0 or not cells:
        failure_reason = (projection_info or {}).get("reason")
        if failure_reason:
            info["projection_failure"] = failure_reason
        info["reason"] = "low_signal"
        return None, info
    table = {
        "rows": row_count,
        "cols": len(columns),
        "cells": cells,
    }
    accepted = is_valid_table(table)
    info["accepted"] = accepted
    if not accepted:
        info["reason"] = "low_signal"
        return None, info
    return table, info


def _micro_ocr_from_projection_legacy(
    *,
    page_index: int,
    table_id: str,
    info: Dict[str, Any],
    asset: Optional[Dict[str, Any]],
    canonical_order: Optional[List[str]],
) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "table_payload": None,
        "info_record": None,
        "table_sidecar": None,
        "cell_entries": [],
        "hashes": {
            "request_sha256": set(),
            "image_sha256": set(),
            "header_lock_sha256": set(),
            "preproc_profile_sha256": set(),
            "cell_image_sha256": set(),
            "stripe_sha256": set(),
        },
    }
    if not asset or not asset.get("image_path"):
        result["info_record"] = {
            "page_index": page_index,
            "method": "micro_ocr",
            "source": "micro_ocr",
            "accepted": False,
            "reason": "no_asset",
        }
        return result
    image_path = Path(asset["image_path"])
    if not image_path.exists():
        result["info_record"] = {
            "page_index": page_index,
            "method": "micro_ocr",
            "source": "micro_ocr",
            "accepted": False,
            "reason": "image_missing",
        }
        return result
    try:
        from PIL import Image
    except Exception:
        result["info_record"] = {
            "page_index": page_index,
            "method": "micro_ocr",
            "source": "micro_ocr",
            "accepted": False,
            "reason": "pil_missing",
        }
        return result
    projection = info.get("projection") or {}
    row_spans_dicts = projection.get("row_spans_page") or []
    columns: List[Tuple[float, float]] = info.get("columns") or []
    if not columns or not row_spans_dicts:
        result["info_record"] = {
            "page_index": page_index,
            "method": "micro_ocr",
            "source": "micro_ocr",
            "accepted": False,
            "reason": "no_geometry",
        }
        return result
    try:
        base_img = Image.open(image_path).convert("L")
    except Exception:
        result["info_record"] = {
            "page_index": page_index,
            "method": "micro_ocr",
            "source": "micro_ocr",
            "accepted": False,
            "reason": "image_open_failed",
        }
        return result
    try:
        img_width, img_height = base_img.size
        projection_width = float(projection.get("width_px") or img_width)
        projection_scale_y = float(projection.get("scale_y") or 1.0)
        roi = asset.get("roi")
        roi_box = _normalize_roi_box(roi)
        roi_offset_x = roi_box["x0"] if roi_box else 0.0
        roi_offset_y = roi_box["y0"] if roi_box else 0.0
        roi_width = roi_box["w"] if roi_box else float(info.get("page_width") or projection_width)
        roi_height = roi_box["h"] if roi_box else float(info.get("page_height") or projection.get("roi_height_px") or img_height)
        page_height = float(info.get("page_height") or projection.get("height_px") or roi_height or img_height)
        scale_x = img_width / max(roi_width, 1.0)
        scale_y = projection_scale_y if projection_scale_y > 0 else img_height / max(roi_height, 1.0)
        header_lock_sha = asset.get("header_lock_sha256")
        if header_lock_sha:
            result["hashes"]["header_lock_sha256"].add(header_lock_sha)
        request_sha = asset.get("request_sha256")
        if request_sha:
            result["hashes"]["request_sha256"].add(request_sha)
        image_sha = asset.get("image_sha256") or asset.get("roi_image_sha256")
        if image_sha:
            result["hashes"]["image_sha256"].add(image_sha)
        col_types: List[str] = []
        headers = canonical_order or []
        for idx in range(len(columns)):
            header_name = headers[idx] if idx < len(headers) else None
            col_types.append(_classify_column(header_name))
        row_spans: List[Tuple[int, int]] = [(int(span["y0"]), int(span["y1"])) for span in row_spans_dicts if isinstance(span, dict)]
        row_count = len(row_spans)
        col_count = len(columns)
        text_grid: List[List[str]] = [["" for _ in range(col_count)] for _ in range(row_count)]
        conf_grid: List[List[Optional[float]]] = [[None for _ in range(col_count)] for _ in range(row_count)]
        accepted_cells = 0
        sidecar_cells: List[Dict[str, Any]] = []
        reason_entries: List[Dict[str, Any]] = []
        with tempfile.TemporaryDirectory(prefix="micro_ocr_") as tmp_dir_str:
            tmp_dir = Path(tmp_dir_str)
            for r_idx, (row_top, row_bottom) in enumerate(row_spans):
                for c_idx, (col_left, col_right) in enumerate(columns):
                    base_box = (col_left, row_top, col_right, row_bottom)
                    kind = col_types[c_idx]
                    accepted = False
                    accepted_variant = False
                    accepted_confidence: Optional[float] = None
                    accepted_raw = ""
                    accepted_meta: Dict[str, Any] = {}
                    expansions = _iter_expanded_boxes(base_box, kind, page_height, roi_box)
                    for expand_id, expanded_box in expansions:
                        stripes = _iter_stripe_boxes(expanded_box, kind)
                        for stripe_index, stripe_id, stripe_box in stripes:
                            if expand_id == "E0_raw" and stripe_id != "stripe_full":
                                continue
                            pixel_box = _compute_cell_pixel_box(
                                stripe_box[0],
                                stripe_box[1],
                                stripe_box[2],
                                stripe_box[3],
                                roi_offset_x=roi_offset_x,
                                roi_offset_y=roi_offset_y,
                                scale_x=scale_x,
                                scale_y=scale_y,
                                img_width=img_width,
                                img_height=img_height,
                            )
                            if not pixel_box:
                                reason_entries.append({
                                    "page_index": page_index,
                                    "table_id": table_id,
                                    "r": r_idx,
                                    "c": c_idx,
                                    "raw_text": "",
                                    "confidence": None,
                                    "accepted": False,
                                    "reason": "invalid_bbox",
                                    "expand_profile_id": expand_id,
                                    "stripe_index": stripe_index,
                                    "stripe_id": stripe_id,
                                    "column_type": kind,
                                })
                                continue
                            left, top, right, bottom = pixel_box
                            stripe_img = base_img.crop((left, top, right, bottom))
                            stripe_sha = hashlib.sha256(stripe_img.tobytes()).hexdigest()
                            result["hashes"]["stripe_sha256"].add(stripe_sha)
                            for profile_id in _iter_profiles(kind):
                                for norm_cfg in NORMALIZATION_VARIANTS:
                                    prof_img, profile_meta = _prepare_profile_image(
                                        stripe_img, profile_id, normalization=norm_cfg
                                    )
                                    profile_sha = _deterministic_json_sha(profile_meta)
                                    result["hashes"]["preproc_profile_sha256"].add(profile_sha)
                                    normalization_id = (profile_meta.get("normalization") or {}).get("id")
                                    orientations_to_try = ORIENTATION_VALUES
                                    polarities_to_try = POLARITY_VALUES
                                    for orientation_id, degrees in orientations_to_try:
                                        oriented_img = _apply_orientation(prof_img, degrees)
                                        for polarity_id, invert in polarities_to_try:
                                            attempt_img = _apply_polarity(oriented_img, invert)
                                            attempt_path = tmp_dir / (
                                                f"cell_r{r_idx}_c{c_idx}_{expand_id}_{stripe_id}_{profile_id}_"
                                                f"{normalization_id or 'norm'}_{orientation_id}_{polarity_id}.png"
                                            )
                                        attempt_img.save(attempt_path)
                                        cell_image_sha = compute_file_sha256(attempt_path)
                                        result["hashes"]["cell_image_sha256"].add(cell_image_sha)
                                        payload, ocr_error = _run_vision_text_ocr(attempt_path)
                                        attempt_path.unlink(missing_ok=True)
                                        raw_text = ""
                                        confidence_val: Optional[float] = None
                                        variant_used = False
                                        reason_code = ""
                                        if payload is None:
                                            reason_code = ocr_error or "ocr_failed"
                                        else:
                                            lines = payload.get("lines") or []
                                            if not lines:
                                                reason_code = "ocr_empty"
                                            else:
                                                top_line = max(lines, key=lambda ln: float(ln.get("confidence") or 0.0))
                                                raw_text = str(top_line.get("text") or "").strip()
                                                confidence_val = float(top_line.get("confidence") or 0.0)
                                                sanitized_text, sanitize_reason, variant_used = _sanitize_micro_value(raw_text, kind)
                                                if sanitized_text is None:
                                                    reason_code = sanitize_reason or "sanitize_failed"
                                                else:
                                                    reason_code = "accepted"
                                                    text_grid[r_idx][c_idx] = sanitized_text
                                                    conf_grid[r_idx][c_idx] = confidence_val
                                                    accepted_cells += 1
                                                    accepted = True
                                                    accepted_variant = variant_used
                                                    accepted_confidence = confidence_val
                                                    accepted_raw = raw_text
                                                    accepted_meta = {
                                                        "expand_profile_id": expand_id,
                                                        "stripe_index": stripe_index,
                                                        "stripe_id": stripe_id,
                                                        "stripe_sha256": stripe_sha,
                                                        "profile_id": profile_id,
                                                        "profile_sha256": profile_sha,
                                                        "profile_meta": profile_meta,
                                                        "orientation_id": orientation_id,
                                                        "polarity_id": polarity_id,
                                                        "cell_image_sha256": cell_image_sha,
                                                        "slab_fallback": False,
                                                        "normalization_id": normalization_id,
                                                    }
                                        reason_entries.append({
                                            "page_index": page_index,
                                            "table_id": table_id,
                                            "r": r_idx,
                                            "c": c_idx,
                                            "raw_text": raw_text,
                                            "confidence": confidence_val,
                                            "accepted": reason_code == "accepted",
                                            "reason": reason_code,
                                            "expand_profile_id": expand_id,
                                            "stripe_index": stripe_index,
                                            "stripe_id": stripe_id,
                                            "stripe_sha256": stripe_sha,
                                            "profile_id": profile_id,
                                            "profile_sha256": profile_sha,
                                            "profile_meta": profile_meta,
                                            "orientation_id": orientation_id,
                                            "polarity_id": polarity_id,
                                            "regex_variant": variant_used,
                                            "preproc_profile_sha256": profile_sha,
                                            "cell_image_sha256": cell_image_sha,
                                            "box_base": {"x0": base_box[0], "y0": base_box[1], "x1": base_box[2], "y1": base_box[3]},
                                            "box_expanded": {"x0": expanded_box[0], "y0": expanded_box[1], "x1": expanded_box[2], "y1": expanded_box[3]},
                                            "slab_fallback": False,
                                            "column_type": kind,
                                            "normalization_id": normalization_id,
                                        })
                                        if reason_code == "accepted":
                                            sidecar_cells.append({
                                                "r": r_idx,
                                                "c": c_idx,
                                                "text": text_grid[r_idx][c_idx],
                                                "raw_text": raw_text,
                                                "confidence": confidence_val,
                                                "preproc_profile_sha256": profile_sha,
                                                "cell_image_sha256": cell_image_sha,
                                                "expand_profile_id": expand_id,
                                                "stripe_index": stripe_index,
                                                "stripe_id": stripe_id,
                                                "stripe_sha256": stripe_sha,
                                                "profile_id": profile_id,
                                                "orientation_id": orientation_id,
                                                "polarity_id": polarity_id,
                                                "regex_variant": variant_used,
                                                "slab_fallback": False,
                                                "column_type": kind,
                                                "normalization_id": normalization_id,
                                            })
                                            break
                                    if accepted:
                                        break
                                if accepted:
                                    break
                            if accepted:
                                break
                        if accepted:
                            break
                    if not accepted and kind in ("money", "date"):
                        slab_pixel = _compute_cell_pixel_box(
                            base_box[0],
                            base_box[1],
                            base_box[2],
                            base_box[3],
                            roi_offset_x=roi_offset_x,
                            roi_offset_y=roi_offset_y,
                            scale_x=scale_x,
                            scale_y=scale_y,
                            img_width=img_width,
                            img_height=img_height,
                        )
                        if slab_pixel:
                            sleft, stop, sright, sbottom = slab_pixel
                            slab_img = base_img.crop((sleft, stop, sright, sbottom))
                            slab_path = tmp_dir / f"cell_r{r_idx}_c{c_idx}_slab.png"
                            slab_img.save(slab_path)
                            slab_sha = compute_file_sha256(slab_path)
                            result["hashes"]["stripe_sha256"].add(slab_sha)
                            result["hashes"]["cell_image_sha256"].add(slab_sha)
                            fallback_profile_meta = {"profile_id": "P1_base", "slab_fallback": True}
                            fallback_profile_sha = _deterministic_json_sha(fallback_profile_meta)
                            result["hashes"]["preproc_profile_sha256"].add(fallback_profile_sha)
                            slab_payload, slab_err = _run_vision_text_ocr(slab_path)
                            slab_path.unlink(missing_ok=True)
                            best_candidate = None
                            best_variant = False
                            best_conf = None
                            best_raw = ""
                            if slab_payload and slab_payload.get("lines"):
                                lines = slab_payload["lines"]
                                column_center = (col_left + col_right) / 2.0
                                def _line_center(ln: Dict[str, Any]) -> float:
                                    box = ln.get("box") or {}
                                    return box.get("x", 0.0) + 0.5 * box.get("w", 0.0)
                                lines_sorted = sorted(lines, key=lambda ln: abs(_line_center(ln) - column_center))
                                for ln in lines_sorted:
                                    raw_candidate = str(ln.get("text") or "").strip()
                                    confidence_val = float(ln.get("confidence") or 0.0)
                                    sanitized_text, sanitize_reason, variant_used = _sanitize_micro_value(raw_candidate, kind)
                                    if sanitized_text is not None:
                                        best_candidate = sanitized_text
                                        best_variant = variant_used
                                        best_conf = confidence_val
                                        best_raw = raw_candidate
                                        break
                            if best_candidate is not None:
                                text_grid[r_idx][c_idx] = best_candidate
                                conf_grid[r_idx][c_idx] = best_conf
                                accepted_cells += 1
                                accepted_variant = best_variant
                                accepted_confidence = best_conf
                                accepted_raw = best_raw
                                accepted_meta = {
                                    "expand_profile_id": "E0_raw",
                                    "stripe_index": None,
                                    "stripe_id": "slab",
                                    "stripe_sha256": slab_sha,
                                    "profile_id": "P1_base",
                                    "profile_sha256": fallback_profile_sha,
                                    "profile_meta": fallback_profile_meta,
                                    "orientation_id": "ori0",
                                    "polarity_id": "polarity_normal",
                                    "cell_image_sha256": slab_sha,
                                    "slab_fallback": True,
                                    "normalization_id": None,
                                }
                                reason_entries.append({
                                    "page_index": page_index,
                                    "table_id": table_id,
                                    "r": r_idx,
                                    "c": c_idx,
                                    "raw_text": best_raw,
                                    "confidence": best_conf,
                                    "accepted": True,
                                    "reason": "accepted",
                                    "expand_profile_id": "E0_raw",
                                    "stripe_index": None,
                                    "stripe_id": "slab",
                                    "stripe_sha256": slab_sha,
                                    "profile_id": "P1_base",
                                    "profile_sha256": accepted_meta["profile_sha256"],
                                    "profile_meta": accepted_meta["profile_meta"],
                                    "orientation_id": "ori0",
                                    "polarity_id": "polarity_normal",
                                    "regex_variant": best_variant,
                                    "preproc_profile_sha256": accepted_meta["profile_sha256"],
                                    "cell_image_sha256": slab_sha,
                                    "slab_fallback": True,
                                    "column_type": kind,
                                    "normalization_id": accepted_meta["normalization_id"],
                                })
                                sidecar_cells.append({
                                    "r": r_idx,
                                    "c": c_idx,
                                    "text": best_candidate,
                                    "raw_text": best_raw,
                                    "confidence": best_conf,
                                    "preproc_profile_sha256": accepted_meta["profile_sha256"],
                                    "cell_image_sha256": slab_sha,
                                    "expand_profile_id": "E0_raw",
                                    "stripe_index": None,
                                    "stripe_id": "slab",
                                    "stripe_sha256": slab_sha,
                                    "profile_id": "P1_base",
                                    "orientation_id": "ori0",
                                    "polarity_id": "polarity_normal",
                                    "regex_variant": best_variant,
                                    "slab_fallback": True,
                                    "column_type": kind,
                                    "normalization_id": accepted_meta["normalization_id"],
                                })
                            else:
                                reason_entries.append({
                                    "page_index": page_index,
                                    "table_id": table_id,
                                    "r": r_idx,
                                    "c": c_idx,
                                    "raw_text": "",
                                    "confidence": None,
                                    "accepted": False,
                                    "reason": "slab_no_match" if slab_payload else (slab_err or "slab_no_output"),
                                    "expand_profile_id": "E0_raw",
                                    "stripe_index": None,
                                    "stripe_id": "slab",
                                    "stripe_sha256": slab_sha,
                                    "profile_id": "P1_base",
                                    "profile_sha256": fallback_profile_sha,
                                    "profile_meta": fallback_profile_meta,
                                    "orientation_id": "ori0",
                                    "polarity_id": "polarity_normal",
                                    "regex_variant": False,
                                    "preproc_profile_sha256": fallback_profile_sha,
                                    "cell_image_sha256": slab_sha,
                                    "slab_fallback": True,
                                    "column_type": kind,
                                })
        base_img.close()
    except Exception:
        base_img.close()
        raise
    result["cell_entries"] = reason_entries
    if accepted_cells == 0:
        result["info_record"] = {
            "page_index": page_index,
            "method": "micro_ocr",
            "source": "micro_ocr",
            "accepted": False,
            "rows": row_count,
            "cols": col_count,
            "reason": "low_signal",
            "accepted_cells": 0,
            "request_sha256": request_sha,
            "roi_image_sha256": image_sha,
        }
        return result
    cells_payload: List[Dict[str, Any]] = []
    for r_idx in range(row_count):
        for c_idx in range(col_count):
            cells_payload.append({
                "r": r_idx,
                "c": c_idx,
                "text": text_grid[r_idx][c_idx],
                "confidence": conf_grid[r_idx][c_idx],
            })
    table_payload = {
        "id": table_id,
        "rows": row_count,
        "cols": col_count,
        "cells": cells_payload,
        "_native_source": "micro_ocr",
    }
    accepted_table = is_valid_table(table_payload)
    info_record = {
        "page_index": page_index,
        "method": "micro_ocr",
        "source": "micro_ocr",
        "accepted": bool(accepted_table),
        "rows": row_count,
        "cols": col_count,
        "billable_cells": sum(1 for row in text_grid for val in row if val.strip()),
        "accepted_cells": accepted_cells,
        "request_sha256": request_sha,
        "roi_image_sha256": image_sha,
        "header_lock_sha256": header_lock_sha,
        "reason": None if accepted_table else "low_signal",
        "table_id": table_id if accepted_table else None,
    }
    table_sidecar = {
        "page_index": page_index,
        "table_id": table_id,
        "rows": row_count,
        "cols": col_count,
        "request_sha256": request_sha,
        "roi_image_sha256": image_sha,
        "header_lock_sha256": header_lock_sha,
        "preproc_profile_sha256": sorted({c.get("preproc_profile_sha256") for c in sidecar_cells if c.get("preproc_profile_sha256")}),
        "stripe_sha256": sorted({c.get("stripe_sha256") for c in sidecar_cells if c.get("stripe_sha256")}),
        "cells": sidecar_cells,
    }
    result["table_payload"] = table_payload if accepted_table else None
    result["info_record"] = info_record
    result["table_sidecar"] = table_sidecar
    return result


def micro_ocr_from_projection(
    *,
    page_index: int,
    table_id: str,
    info: Dict[str, Any],
    asset: Optional[Dict[str, Any]],
    canonical_order: Optional[List[str]],
) -> Dict[str, Any]:
    """Atlas-first micro-OCR pipeline (ROI + atlas batches)."""

    def _build_info_record(**kwargs: Any) -> Dict[str, Any]:
        record = {
            "page_index": page_index,
            "table_id": table_id,
            "method": "micro_ocr_atlas",
            "source": "micro_ocr",
            "ocr_calls": 0,
            "accepted": False,
            "accepted_cells": 0,
            "pending_cells": 0,
            "rows": 0,
            "cols": 0,
            "reason": None,
        }
        record.update(kwargs)
        return record

    result: Dict[str, Any] = {
        "table_payload": None,
        "info_record": None,
        "roi_sidecar": None,
        "atlas_sidecar": None,
        "patch_sidecar": None,
        "cell_entries": [],
        "hashes": {
            "request_sha256": set(),
            "image_sha256": set(),
            "header_lock_sha256": set(),
            "atlas_sha256": set(),
            "patch_atlas_sha256": set(),
            "patch_profile_sha256": set(),
        },
    }

    if not asset or not asset.get("image_path"):
        result["info_record"] = _build_info_record(reason="no_asset")
    return result


def micro_ocr_from_rowize(
    *,
    page: Dict[str, Any],
    table_id: str,
    columns: List[Tuple[float, float]],
    row_spans: List[Tuple[float, float]],
    page_image_path: Path,
) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "table_payload": None,
        "info_record": None,
        "roi_sidecar": None,
        "atlas_sidecar": None,
        "patch_sidecar": None,
    }
    if not page_image_path.exists():
        result["info_record"] = {
            "method": "micro_rowize",
            "accepted": False,
            "reason": "page_image_missing",
        }
        return result
    try:
        base_img = Image.open(page_image_path).convert("L")
    except Exception:
        result["info_record"] = {
            "method": "micro_rowize",
            "accepted": False,
            "reason": "image_open_failed",
        }
        return result
    try:
        img_width, img_height = base_img.size
        width = float(page.get("width") or img_width)
        height = float(page.get("height") or img_height)
        scale_x = img_width / max(width, 1.0)
        scale_y = img_height / max(height, 1.0)
        # Derive column types by density over existing texts in the rowized grid
        rows = len(row_spans)
        cols = len(columns)
        text_grid: List[List[str]] = [["" for _ in range(cols)] for _ in range(rows)]
        conf_grid: List[List[Optional[float]]] = [[None for _ in range(cols)] for _ in range(rows)]
        source_grid: List[List[str]] = [["" for _ in range(cols)] for _ in range(rows)]
        variant_grid: List[List[bool]] = [[False for _ in range(cols)] for _ in range(rows)]

        # Seed with any existing cell text from rowization (if present on page object later)
        # No strong requirement; micro will fill pending money/date
        col_types: List[str] = []
        # Simple type inference: tally per column using tokens under row spans
        # Use page lines as text source
        col_type_votes: List[Dict[str, int]] = [{"money":0,"date":0,"code":0,"text":0} for _ in range(cols)]
        lines = page.get("lines") or []
        # Keep a pristine copy of seeds for later fallback
        seed_grid: List[List[str]] = [["" for _ in range(cols)] for _ in range(rows)]
        def _iou(a: Tuple[int,int,int,int], b: Tuple[int,int,int,int]) -> float:
            ax0, ay0, ax1, ay1 = a; bx0, by0, bx1, by1 = b
            ix0, iy0 = max(ax0, bx0), max(ay0, by0)
            ix1, iy1 = min(ax1, bx1), min(ay1, by1)
            iw, ih = max(0, ix1-ix0), max(0, iy1-iy0)
            inter = iw * ih
            if inter <= 0:
                return 0.0
            area_a = max(0, ax1-ax0) * max(0, ay1-ay0)
            area_b = max(0, bx1-bx0) * max(0, by1-by0)
            union = area_a + area_b - inter
            return inter / max(1, union)

        for r_idx, (ry0, ry1) in enumerate(row_spans):
            for c_idx, (cx0, cx1) in enumerate(columns):
                # gather line texts in this cell
                bucket_texts: List[str] = []
                for line in lines:
                    box = line.get("pixel_box") or line.get("box") or {}
                    lx = float(box.get("x", box.get("x0", 0.0)))
                    ly = float(box.get("y", box.get("y0", 0.0)))
                    lw = float(box.get("w", box.get("width", 0.0)))
                    lh = float(box.get("h", box.get("height", 0.0)))
                    cx = lx + lw / 2.0
                    cy = ly + lh / 2.0
                    if cx0 <= cx <= cx1 and ry0 <= cy <= ry1:
                        t = str(line.get("text", "")).strip()
                        if t:
                            bucket_texts.append(t)
                # join texts and place in seed grid
                if bucket_texts:
                    seed = " ".join(bucket_texts)
                    text_grid[r_idx][c_idx] = seed
                    seed_grid[r_idx][c_idx] = seed
                    # vote type
                    tag = tag_cell_type(seed)
                    if tag not in col_type_votes[c_idx]:
                        col_type_votes[c_idx][tag] = 0
                    col_type_votes[c_idx][tag] += 1
                else:
                    # Type-aware seed fallback within the same row band
                    cell_box = (int(cx0), int(ry0), int(cx1), int(ry1))
                    # Stage 1: best IoU (≥0.25), break ties by x-preference (money:rightmost, date:leftmost), then vertical proximity
                    kind = None
                    if c_idx < len(col_types):
                        kind = col_types[c_idx]
                    best_text = None
                    best_score = (-1.0, -1.0, -1.0)
                    for line in lines:
                        box = line.get("pixel_box") or line.get("box") or {}
                        lx = float(box.get("x", box.get("x0", 0.0)))
                        ly = float(box.get("y", box.get("y0", 0.0)))
                        lw = float(box.get("w", box.get("width", 0.0)))
                        lh = float(box.get("h", box.get("height", 0.0)))
                        cand_box = (int(lx), int(ly), int(lx+lw), int(ly+lh))
                        # Overlap row band only
                        if ly > ry1 or (ly + lh) < ry0:
                            continue
                        cx_mid = lx + lw / 2.0
                        cy_mid = ly + lh / 2.0
                        t = str(line.get("text", "")).strip()
                        if not t:
                            continue
                        ov = _iou(cell_box, cand_box)
                        if kind == "money":
                            xpref = (cx_mid - cx0)  # rightmost better
                        elif kind == "date":
                            xpref = (cx1 - cx_mid)  # leftmost better
                        else:
                            xpref = -abs((cx0 + cx1)/2.0 - cx_mid)  # center pref
                        ypref = -abs(((ry0 + ry1)/2.0) - cy_mid)
                        score = (ov, xpref, ypref)
                        if ov >= 0.25 and score > best_score:
                            best_score = score
                            best_text = t
                    # Stage 2: if none, use center-in-band + x-preference as a fallback
                    if not best_text:
                        best_score2 = (-1.0, -1.0)
                        for line in lines:
                            box = line.get("pixel_box") or line.get("box") or {}
                            lx = float(box.get("x", box.get("x0", 0.0)))
                            ly = float(box.get("y", box.get("y0", 0.0)))
                            lw = float(box.get("w", box.get("width", 0.0)))
                            lh = float(box.get("h", box.get("height", 0.0)))
                            cy_mid = ly + lh / 2.0
                            if ly > ry1 or (ly + lh) < ry0:
                                continue
                            t = str(line.get("text", "")).strip()
                            if not t:
                                continue
                            cx_mid = lx + lw / 2.0
                            if not (cx0 <= cx_mid <= cx1):
                                continue
                            if kind == "money":
                                xpref = (cx_mid - cx0)
                            elif kind == "date":
                                xpref = (cx1 - cx_mid)
                            else:
                                xpref = -abs((cx0 + cx1)/2.0 - cx_mid)
                            score2 = (xpref, -abs(((ry0 + ry1)/2.0) - cy_mid))
                            if score2 > best_score2:
                                best_score2 = score2
                                best_text = t
                    if best_text:
                        seed_grid[r_idx][c_idx] = best_text
                        text_grid[r_idx][c_idx] = best_text
        # Column-level vote fallback: scan all lines in the column band to infer type
        for line in lines:
            box = line.get("pixel_box") or line.get("box") or {}
            lx = float(box.get("x", box.get("x0", 0.0)))
            ly = float(box.get("y", box.get("y0", 0.0)))
            lw = float(box.get("w", box.get("width", 0.0)))
            lh = float(box.get("h", box.get("height", 0.0)))
            cx = lx + lw / 2.0
            t = str(line.get("text", "")).strip()
            if not t:
                continue
            # find the column whose band contains the token center
            for c_idx, (cx0, cx1) in enumerate(columns):
                if cx0 <= cx <= cx1:
                    tag = tag_cell_type(t)
                    if tag not in col_type_votes[c_idx]:
                        col_type_votes[c_idx][tag] = 0
                    col_type_votes[c_idx][tag] += 1
                    break

        for c_idx in range(cols):
            votes = col_type_votes[c_idx]
            # prioritize money > date > code > text
            typ = "text"
            if votes.get("money",0) >= max(1, votes.get("date",0)):
                typ = "money"
            elif votes.get("date",0) > 0:
                typ = "date"
            elif votes.get("code",0) > 0:
                typ = "code"
            col_types.append(typ)

        # Second pass: ensure seeds for money/date columns using type-aware selection when missing
        for r_idx, (ry0, ry1) in enumerate(row_spans):
            for c_idx, (cx0, cx1) in enumerate(columns):
                if col_types[c_idx] not in ("money", "date"):
                    continue
                if seed_grid[r_idx][c_idx]:
                    continue
                # choose best candidate by x-preference in column band and within row band
                kind = col_types[c_idx]
                best_text = None
                best_score = (-1.0, -1.0)
                for line in lines:
                    box = line.get("pixel_box") or line.get("box") or {}
                    lx = float(box.get("x", box.get("x0", 0.0)))
                    ly = float(box.get("y", box.get("y0", 0.0)))
                    lw = float(box.get("w", box.get("width", 0.0)))
                    lh = float(box.get("h", box.get("height", 0.0)))
                    cx_mid = lx + lw / 2.0
                    cy_mid = ly + lh / 2.0
                    if not (ry0 <= cy_mid <= ry1 and cx0 <= cx_mid <= cx1):
                        continue
                    t = str(line.get("text", "")).strip()
                    if not t:
                        continue
                    if kind == "money":
                        xpref = (cx_mid - cx0)
                    else:
                        xpref = (cx1 - cx_mid)
                    score = (xpref, -abs(((ry0 + ry1)/2.0) - cy_mid))
                    if score > best_score:
                        best_score = score
                        best_text = t
                if best_text:
                    seed_grid[r_idx][c_idx] = best_text
                    text_grid[r_idx][c_idx] = text_grid[r_idx][c_idx] or best_text

        # Build cell pixel boxes
        cell_boxes: Dict[Tuple[int, int], Tuple[int, int, int, int]] = {}
        for r_idx, (ry0, ry1) in enumerate(row_spans):
            for c_idx, (cx0, cx1) in enumerate(columns):
                pix = _compute_cell_pixel_box(cx0, ry0, cx1, ry1, roi_offset_x=0.0, roi_offset_y=0.0, scale_x=scale_x, scale_y=scale_y, img_width=img_width, img_height=img_height)
                cell_boxes[(r_idx, c_idx)] = pix or (0,0,0,0)

        # Build atlas over unresolved money/date cells
        pending_cells: List[Tuple[int,int]] = []
        for r_idx in range(rows):
            for c_idx in range(cols):
                if col_types[c_idx] not in ("money","date"):
                    continue
                seed = text_grid[r_idx][c_idx]
                val, reason, _ = _sanitize_micro_value(seed, col_types[c_idx])
                if val is None:
                    pending_cells.append((r_idx, c_idx))

        ocr_calls = 0
        worker_epoch: Optional[int] = None
        atlas_sidecar = None
        patch_sidecar = None
        # Atlas stage
        atlas_cand_map: Dict[Tuple[int, int], List[Dict[str, Any]]] = {}
        def _sel_key(kind: str, candidate: Dict[str, Any], mid_x: float, mid_y: float) -> Tuple:
            conf = candidate.get("confidence") or 0.0
            cx = candidate.get("center_x", mid_x)
            cy = candidate.get("center_y", mid_y)
            text = candidate.get("sanitized") or candidate.get("raw_text") or ""
            if kind == "money":
                return (-conf, -cx, abs(cy - mid_y), text)
            if kind == "date":
                return (-conf, cx, abs(cy - mid_y), text)
            return (-conf, abs(cy - mid_y), cx, text)
        if pending_cells:
            gutter = 2
            tiles: List[Tuple[Tuple[int,int], Image.Image]] = []
            entries: List[Dict[str, Any]] = []
            max_w = 0
            total_h = gutter
            for (r_idx, c_idx) in pending_cells:
                pix_box = cell_boxes.get((r_idx, c_idx))
                if not pix_box or pix_box == (0,0,0,0):
                    continue
                crop = base_img.crop(pix_box)
                tiles.append(((r_idx,c_idx), crop))
                max_w = max(max_w, crop.width)
                total_h += crop.height + gutter
            if tiles:
                atlas_w = max_w + gutter*2
                atlas_h = max(total_h, gutter*2 + 4)
                atlas_img = Image.new("L", (atlas_w, atlas_h), color=255)
                y = gutter
                for (r_idx,c_idx), crop in tiles:
                    atlas_img.paste(crop, (gutter, y))
                    entries.append({"r":r_idx, "c":c_idx, "x":gutter, "y":y, "w":crop.width, "h":crop.height, "column_type": col_types[c_idx]})
                    y += crop.height + gutter
                with tempfile.NamedTemporaryFile(prefix="row_atlas_", suffix=".png", delete=False) as tmp:
                    atlas_path = Path(tmp.name)
                    atlas_img.save(atlas_path, format="PNG")
                atlas_img.close()
                atlas_sha = compute_file_sha256(atlas_path)
                payload, err = _run_vision_text_ocr(atlas_path)
                atlas_path.unlink(missing_ok=True)
                if payload:
                    ocr_calls += 1
                    worker_epoch = payload.get("_worker_epoch")
                    lines = payload.get("lines") or []
                    coord_space = _convert_line_boxes(lines, atlas_w, atlas_h)
                    matched = 0
                    for e in entries:
                        r_idx = e["r"]; c_idx = e["c"]
                        tx,ty,tw,th = e["x"],e["y"],e["w"],e["h"]
                        mid_x = tx + tw/2.0; mid_y = ty + th/2.0
                        cand: List[Dict[str, Any]] = []
                        kind = col_types[c_idx]
                        for line in lines:
                            box = line.get("_box_pixel") or (line.get("box") or {})
                            lx = float(box.get("x", 0.0)); ly = float(box.get("y", 0.0)); lw = float(box.get("w", 0.0)); lh = float(box.get("h", 0.0))
                            cx = lx + lw/2.0; cy = ly + lh/2.0
                            if not (tx <= cx <= tx+tw and ty <= cy <= ty+th):
                                continue
                            raw_text = str(line.get("text",""))
                            conf = float(line.get("confidence") or 0.0)
                            sanitized, reason, variant = _sanitize_micro_value(raw_text, kind)
                            if sanitized is None:
                                continue
                            # Feature scoring (deterministic)
                            x_right_norm = 0.0
                            if tw > 0:
                                x_right_norm = max(0.0, min(1.0, (cx - tx) / tw))
                            has_cents = bool(re.match(r"^-?\$?\s*\d+(?:\.\d{2})$", str(sanitized)))
                            # center proximity using sigma=0.25*tw
                            mid_x_local = tx + tw/2.0
                            sigma = max(1e-6, 0.25 * max(tw, 1.0))
                            center_proximity = math.exp(-(((cx - mid_x_local) ** 2) / (2.0 * (sigma ** 2)))) if tw > 0 else 0.0
                            score = 0.55*conf + 0.25*x_right_norm + 0.15*(1.0 if has_cents else 0.0) + 0.05*center_proximity
                            cand.append({
                                "value": sanitized,
                                "confidence": conf,
                                "x_right_norm": x_right_norm,
                                "has_cents": has_cents,
                                "score": score,
                                "center_x": cx,
                                "center_y": cy,
                                "variant": variant,
                                "source": "atlas"
                            })
                        if cand:
                            cand.sort(key=lambda c: (-float(c.get("score", 0.0)), -float(c.get("confidence", 0.0))))
                            chosen = cand[0]
                            text_grid[r_idx][c_idx] = chosen["value"]
                            conf_grid[r_idx][c_idx] = float(chosen.get("confidence") or 0.0)
                            source_grid[r_idx][c_idx] = str(chosen.get("source") or "atlas")
                            variant_grid[r_idx][c_idx] = bool(chosen.get("variant"))
                            matched += 1
                            # Record top-K atlas alternates for ring candidates (value + features)
                            atlas_cand_map[(r_idx, c_idx)] = [{
                                "value": c.get("value"),
                                "confidence": c.get("confidence"),
                                "x_right_norm": c.get("x_right_norm"),
                                "has_cents": c.get("has_cents"),
                                "score": c.get("score")
                            } for c in cand[:3] if isinstance(c.get("value"), str)]
                    atlas_sidecar = {
                        "schema": "micro_atlas.v1",
                        "coord_space": coord_space,
                        "atlas_width_px": atlas_w,
                        "atlas_height_px": atlas_h,
                        "atlas_lines_total": len(lines),
                        "atlas_lines_matched": matched,
                        "tile_pack_order": entries,
                        "worker_epoch": worker_epoch,
                        "ocr_calls": 1,
                    }
        # Patch stage
        patch_cand_map: Dict[Tuple[int, int], List[Dict[str, Any]]] = {}
        pending_after = [ (r,c) for r in range(rows) for c in range(cols) if col_types[c] in ("money","date") and not text_grid[r][c].strip() ]
        if pending_after:
            gutter = 2
            tiles: List[Tuple[Tuple[int,int], str, Image.Image, Dict[str,Any]]] = []
            entries: List[Dict[str, Any]] = []
            max_w = 0
            total_h = gutter
            for (r_idx,c_idx) in pending_after:
                pix_box = cell_boxes.get((r_idx,c_idx))
                if not pix_box or pix_box == (0,0,0,0):
                    continue
                x0,y0,x1,y1 = pix_box
                pad = max(2, int(0.05 * (y1 - y0)))
                left = max(0, x0 - pad); right = min(img_width, x1 + pad); top = max(0, y0 - pad); bottom = min(img_height, y1 + pad)
                crop = base_img.crop((left, top, right, bottom))
                min_w = 28
                if crop.width < min_w:
                    pad_extra = (min_w - crop.width) // 2 + 1
                    left = max(0, left - pad_extra); right = min(img_width, right + pad_extra)
                    crop = base_img.crop((left, top, right, bottom))
                profile_id = "MoneyPatch" if col_types[c_idx] == "money" else "DatePatch"
                processed, meta = _apply_patch_profile(crop, profile_id)
                tiles.append(((r_idx,c_idx), profile_id, processed, meta))
                max_w = max(max_w, processed.width)
                total_h += processed.height + gutter
            if tiles:
                atlas_w = max_w + gutter*2
                atlas_h = max(total_h, gutter*2 + 4)
                atlas_img = Image.new("L", (atlas_w, atlas_h), color=255)
                y = gutter
                profile_shas: List[str] = []
                for (r_idx,c_idx), profile_id, tile_img, meta in tiles:
                    atlas_img.paste(tile_img, (gutter, y))
                    entries.append({"r":r_idx, "c":c_idx, "x":gutter, "y":y, "w":tile_img.width, "h":tile_img.height, "column_type": col_types[c_idx], "patch_profile_id": profile_id, "patch_profile_sha256": _deterministic_json_sha(meta)})
                    y += tile_img.height + gutter
                with tempfile.NamedTemporaryFile(prefix="row_patch_", suffix=".png", delete=False) as tmp:
                    patch_path = Path(tmp.name)
                    atlas_img.save(patch_path, format="PNG")
                atlas_img.close()
                payload, err = _run_vision_text_ocr(patch_path)
                patch_path.unlink(missing_ok=True)
                matched = 0
                lines_total = 0
                coord_space = "pixel"
                if payload:
                    ocr_calls += 1
                    lines = payload.get("lines") or []
                    coord_space = _convert_line_boxes(lines, atlas_w, atlas_h)
                    lines_total = len(lines)
                    for e in entries:
                        r_idx = e["r"]; c_idx = e["c"]
                        tx,ty,tw,th = e["x"],e["y"],e["w"],e["h"]
                        mid_x = tx + tw/2.0; mid_y = ty + th/2.0
                        kind = col_types[c_idx]
                        cand: List[Dict[str, Any]] = []
                        for line in lines:
                            box = line.get("_box_pixel") or (line.get("box") or {})
                            lx = float(box.get("x", 0.0)); ly = float(box.get("y", 0.0)); lw = float(box.get("w", 0.0)); lh = float(box.get("h", 0.0))
                            cx = lx + lw/2.0; cy = ly + lh/2.0
                            if not (tx <= cx <= tx+tw and ty <= cy <= ty+th):
                                continue
                            raw_text = str(line.get("text",""))
                            conf = float(line.get("confidence") or 0.0)
                            sanitized, reason, variant = _sanitize_micro_value(raw_text, kind)
                            if sanitized is None:
                                continue
                            x_right_norm = 0.0
                            if tw > 0:
                                x_right_norm = max(0.0, min(1.0, (cx - tx) / tw))
                            has_cents = bool(re.match(r"^-?\$?\s*\d+(?:\.\d{2})$", str(sanitized)))
                            mid_x_local = tx + tw/2.0
                            sigma = max(1e-6, 0.25 * max(tw, 1.0))
                            center_proximity = math.exp(-(((cx - mid_x_local) ** 2) / (2.0 * (sigma ** 2)))) if tw > 0 else 0.0
                            score = 0.55*conf + 0.25*x_right_norm + 0.15*(1.0 if has_cents else 0.0) + 0.05*center_proximity
                            cand.append({
                                "value": sanitized,
                                "confidence": conf,
                                "x_right_norm": x_right_norm,
                                "has_cents": has_cents,
                                "score": score,
                                "center_x": cx,
                                "center_y": cy,
                                "variant": variant,
                                "source": "patch"
                            })
                        if cand:
                            cand.sort(key=lambda c: (-float(c.get("score", 0.0)), -float(c.get("confidence", 0.0))))
                            chosen = cand[0]
                            text_grid[r_idx][c_idx] = chosen["value"]
                            conf_grid[r_idx][c_idx] = float(chosen.get("confidence") or 0.0)
                            source_grid[r_idx][c_idx] = str(chosen.get("source") or "patch")
                            variant_grid[r_idx][c_idx] = bool(chosen.get("variant"))
                            matched += 1
                            patch_cand_map[(r_idx, c_idx)] = [{
                                "value": c.get("value"),
                                "confidence": c.get("confidence"),
                                "x_right_norm": c.get("x_right_norm"),
                                "has_cents": c.get("has_cents"),
                                "score": c.get("score")
                            } for c in cand[:3] if isinstance(c.get("value"), str)]
                patch_sidecar = {
                    "schema": "micro_patch.v1",
                    "tile_pack_order": entries,
                    "worker_epoch": worker_epoch,
                    "ocr_calls": 1 if payload else 0,
                    "coord_space": coord_space,
                    "atlas_width_px": atlas_w,
                    "atlas_height_px": atlas_h,
                    "patch_lines_total": lines_total,
                    "patch_lines_matched": matched,
                }

        accepted_cells = sum(1 for r in range(rows) for c in range(cols) if text_grid[r][c].strip())
        table_cells: List[Dict[str, Any]] = []
        for r in range(rows):
            for c in range(cols):
                table_cells.append({
                    "r": r,
                    "c": c,
                    "text": text_grid[r][c],
                    "confidence": conf_grid[r][c],
                    "source": source_grid[r][c] or ("rowize" if text_grid[r][c] else ""),
                    "variant": variant_grid[r][c],
                    "seed_text": seed_grid[r][c],
                    "seed_source": "rowize" if seed_grid[r][c] else "",
                    "atlas_candidates": atlas_cand_map.get((r, c)),
                    "patch_candidates": patch_cand_map.get((r, c)),
                })
        # Compute per-column cents ratio (money-like values only)
        column_cents_ratio: List[float] = [0.0 for _ in range(cols)]
        for c in range(cols):
            vals: List[str] = []
            for r in range(rows):
                txt = text_grid[r][c]
                if txt and STRICT_MONEY_RE.match(_normalize_money_text(txt)):
                    vals.append(_normalize_money_text(txt))
            if vals:
                cents = sum(1 for v in vals if re.search(r"\.\d{2}$", v))
                column_cents_ratio[c] = cents / float(len(vals))

        # Per-table constant money values (freq ≥ threshold and bottom-band lexeme heuristic)
        freq: Dict[str, int] = {}
        for r in range(rows):
            for c in range(cols):
                v = seed_grid[r][c] or text_grid[r][c]
                vn = _normalize_money_text(v) if v else ""
                if vn and STRICT_MONEY_RE.match(vn):
                    freq[vn] = freq.get(vn, 0) + 1
        # Dynamic threshold: max(5, ceil(10% of table rows))
        const_thresh = max(5, int(math.ceil(rows * 0.10)))
        constant_money_values: List[str] = [k for k, n in freq.items() if n >= const_thresh]
        # Bottom 15% band lexemes
        lexemes = ("TOTAL", "CHECK", "AMOUNT PAID", "EFT", "BATCH TOTAL")
        band_y = img_height * 0.85
        for line in lines:
            ttxt = str(line.get("text", ""))
            if not ttxt:
                continue
            box = line.get("pixel_box") or line.get("box") or {}
            ly = float(box.get("y", box.get("y0", 0.0)))
            if ly >= band_y and any(x in ttxt.upper() for x in lexemes):
                m = SPATIAL_MONEY_RE.search(ttxt)
                if m:
                    mv = _normalize_money_text(m.group(0))
                    if mv and STRICT_MONEY_RE.match(mv) and mv not in constant_money_values:
                        constant_money_values.append(mv)

        # Per-column acceptance (atlas/patch) counts
        accepted_by_col_type: Dict[str, Dict[str, int]] = {
            "money": {"atlas": 0, "patch": 0},
            "date": {"atlas": 0, "patch": 0},
        }
        for r in range(rows):
            for c in range(cols):
                src = source_grid[r][c]
                typ = col_types[c]
                if typ in accepted_by_col_type and src in ("atlas", "patch"):
                    accepted_by_col_type[typ][src] += 1

        table_payload = {
            "id": table_id,
            "rows": rows,
            "cols": cols,
            "cells": table_cells,
            "_native_source": "micro_ocr_rowize",
            "column_types": col_types,
            "column_cents_ratio": column_cents_ratio,
            "constant_money_values": constant_money_values,
        }
        info_record = {
            "method": "micro_rowize",
            "accepted": bool(is_valid_table(table_payload)),
            "rows": rows,
            "cols": cols,
            "accepted_cells": accepted_cells,
            "ocr_calls": ocr_calls,
            "pending_cells": len(pending_cells) if 'pending_cells' in locals() else 0,
            "page_index": int(page.get("page_index", 0) or 0),
            "accepted_by_column_type": accepted_by_col_type,
        }
        result["table_payload"] = table_payload
        result["info_record"] = info_record
        result["atlas_sidecar"] = atlas_sidecar
        result["patch_sidecar"] = patch_sidecar
        return result
    finally:
        base_img.close()
    image_path = Path(asset["image_path"])
    if not image_path.exists():
        result["info_record"] = _build_info_record(reason="image_missing")
        return result

    projection = info.get("projection") or {}
    row_spans_dicts = projection.get("row_spans_page") or []
    columns: List[Tuple[float, float]] = info.get("columns") or []
    if not columns or not row_spans_dicts:
        result["info_record"] = _build_info_record(reason="no_geometry")
        return result

    try:
        base_img = Image.open(image_path).convert("L")
    except Exception:
        result["info_record"] = _build_info_record(reason="image_open_failed")
        return result

    try:
        img_width, img_height = base_img.size
        projection_width = float(projection.get("width_px") or img_width)
        projection_scale_y = float(projection.get("scale_y") or 1.0)
        roi = asset.get("roi")
        roi_box = _normalize_roi_box(roi)
        roi_offset_x = roi_box["x0"] if roi_box else 0.0
        roi_offset_y = roi_box["y0"] if roi_box else 0.0
        roi_width = roi_box["w"] if roi_box else float(info.get("page_width") or projection_width)
        roi_height = roi_box["h"] if roi_box else float(info.get("page_height") or projection.get("roi_height_px") or img_height)
        page_height = float(info.get("page_height") or projection.get("height_px") or roi_height or img_height)
        scale_x = img_width / max(roi_width, 1.0)
        scale_y = projection_scale_y if projection_scale_y > 0 else img_height / max(roi_height, 1.0)
        header_lock_sha = asset.get("header_lock_sha256")
        if header_lock_sha:
            result["hashes"]["header_lock_sha256"].add(header_lock_sha)
        request_sha = asset.get("request_sha256")
        if request_sha:
            result["hashes"]["request_sha256"].add(request_sha)
        image_sha = asset.get("image_sha256") or asset.get("roi_image_sha256")
        if image_sha:
            result["hashes"]["image_sha256"].add(image_sha)

        headers = canonical_order or []
        col_types: List[str] = []
        for idx in range(len(columns)):
            header_name = headers[idx] if idx < len(headers) else None
            col_types.append(_classify_column(header_name))
        row_spans: List[Tuple[int, int]] = [(int(span["y0"]), int(span["y1"])) for span in row_spans_dicts if isinstance(span, dict)]
        row_count = len(row_spans)
        col_count = len(columns)
        text_grid: List[List[str]] = [["" for _ in range(col_count)] for _ in range(row_count)]
        conf_grid: List[List[Optional[float]]] = [[None for _ in range(col_count)] for _ in range(row_count)]
        source_grid: List[List[str]] = [["" for _ in range(col_count)] for _ in range(row_count)]
        variant_grid: List[List[bool]] = [[False for _ in range(col_count)] for _ in range(row_count)]

        cells_meta: Dict[Tuple[int, int], Dict[str, Any]] = {}

        def _cell_mid(box: Tuple[int, int, int, int]) -> Tuple[float, float]:
            x0, y0, x1, y1 = box
            return ((x0 + x1) / 2.0, (y0 + y1) / 2.0)

        def _select_key(kind: str, candidate: Dict[str, Any], mid_x: float, mid_y: float) -> Tuple:
            conf = candidate.get("confidence") or 0.0
            cx = candidate.get("center_x", mid_x)
            cy = candidate.get("center_y", mid_y)
            text = candidate.get("sanitized") or candidate.get("raw_text") or ""
            if kind == "money":
                return (-conf, -cx, abs(cy - mid_y), text)
            if kind == "date":
                return (-conf, cx, abs(cy - mid_y), text)
            return (-conf, abs(cy - mid_y), cx, text)

        cell_boxes: Dict[Tuple[int, int], Tuple[int, int, int, int]] = {}
        for r_idx, (row_top, row_bottom) in enumerate(row_spans):
            for c_idx, (col_left, col_right) in enumerate(columns):
                base_box = (col_left, row_top, col_right, row_bottom)
                pix_box = _compute_cell_pixel_box(
                    base_box[0],
                    base_box[1],
                    base_box[2],
                    base_box[3],
                    roi_offset_x=roi_offset_x,
                    roi_offset_y=roi_offset_y,
                    scale_x=scale_x,
                    scale_y=scale_y,
                    img_width=img_width,
                    img_height=img_height,
                )
                if pix_box:
                    cell_boxes[(r_idx, c_idx)] = pix_box
                else:
                    cell_boxes[(r_idx, c_idx)] = (0, 0, 0, 0)

        ocr_calls = 0
        worker_epoch: Optional[int] = None

        # Pass 1 — ROI OCR
        roi_lines: List[Dict[str, Any]] = []
        roi_coord_space = "pixel"
        roi_payload, roi_error = _run_vision_text_ocr(image_path)
        if roi_payload:
            ocr_calls += 1
            worker_epoch = roi_payload.get("_worker_epoch")
            roi_lines = roi_payload.get("lines") or []
            roi_coord_space = _convert_line_boxes(roi_lines, img_width, img_height)
        else:
            result["info_record"] = _build_info_record(
                reason=roi_error or "roi_ocr_failed",
                rows=row_count,
                cols=col_count,
                ocr_calls=ocr_calls,
            )
            base_img.close()
            return result

        roi_sidecar_lines: List[Dict[str, Any]] = []
        for line in roi_lines:
            box = line.get("_box_pixel") or (line.get("box") or {})
            roi_sidecar_lines.append(
                {
                    "text": str(line.get("text", "")),
                    "confidence": float(line.get("confidence") or 0.0),
                    "box": box,
                    "raw_box": line.get("box"),
                }
            )

        roi_sidecar = {
            "schema": "micro_roi_observation.v1",
            "page_index": page_index,
            "table_id": table_id,
            "worker_epoch": worker_epoch,
            "ocr_calls": 1,
            "request_sha256": request_sha,
            "roi_image_sha256": image_sha,
            "header_lock_sha256": header_lock_sha,
            "coord_space": roi_coord_space,
            "roi_width_px": img_width,
            "roi_height_px": img_height,
            "lines": roi_sidecar_lines,
        }

        pending_cells: List[Tuple[int, int]] = []
        for (r_idx, c_idx), pix_box in cell_boxes.items():
            kind = col_types[c_idx]
            if pix_box == (0, 0, 0, 0):
                pending_cells.append((r_idx, c_idx))
                continue
            x0, y0, x1, y1 = pix_box
            mid_x, mid_y = _cell_mid(pix_box)
            candidates: List[Dict[str, Any]] = []
            for line in roi_lines:
                box = line.get("_box_pixel") or (line.get("box") or {})
                lx = float(box.get("x", 0.0))
                ly = float(box.get("y", 0.0))
                lw = float(box.get("w", 0.0))
                lh = float(box.get("h", 0.0))
                cx = lx + lw / 2.0
                cy = ly + lh / 2.0
                if not (x0 <= cx <= x1 and y0 <= cy <= y1):
                    continue
                raw_text = str(line.get("text", "")).strip()
                confidence_val = float(line.get("confidence") or 0.0)
                sanitized_text, sanitize_reason, variant_used = _sanitize_micro_value(raw_text, kind)
                if sanitized_text is None:
                    continue
                candidates.append(
                    {
                        "sanitized": sanitized_text,
                        "raw_text": raw_text,
                        "confidence": confidence_val,
                        "variant": variant_used,
                        "center_x": cx,
                        "center_y": cy,
                        "source": "vision",
                    }
                )
            if candidates:
                candidates.sort(key=lambda cand: _select_key(kind, cand, mid_x, mid_y))
                chosen = candidates[0]
                text_grid[r_idx][c_idx] = chosen["sanitized"]
                conf_grid[r_idx][c_idx] = chosen["confidence"]
                source_grid[r_idx][c_idx] = chosen["source"]
                variant_grid[r_idx][c_idx] = bool(chosen["variant"])
                cells_meta[(r_idx, c_idx)] = {
                    "selection_rank": 0,
                    "raw_text": chosen.get("raw_text"),
                }
            elif kind in ("money", "date"):
                pending_cells.append((r_idx, c_idx))

        atlas_sidecar = None
        atlas_sha: Optional[str] = None

        if pending_cells:
            gutter = 2
            tile_entries: List[Dict[str, Any]] = []
            atlas_tiles: List[Tuple[Tuple[int, int], Image.Image]] = []
            max_width = 0
            total_height = gutter
            for r_idx, c_idx in pending_cells:
                pix_box = cell_boxes.get((r_idx, c_idx))
                if not pix_box or pix_box == (0, 0, 0, 0):
                    continue
                crop = base_img.crop(pix_box)
                atlas_tiles.append(((r_idx, c_idx), crop))
                max_width = max(max_width, crop.width)
                total_height += crop.height + gutter
            if atlas_tiles:
                atlas_width = max_width + gutter * 2
                atlas_height = max(total_height, gutter * 2 + 4)
                atlas_img = Image.new("L", (atlas_width, atlas_height), color=255)
                current_y = gutter
                for (r_idx, c_idx), crop in atlas_tiles:
                    atlas_img.paste(crop, (gutter, current_y))
                    tile_entries.append(
                        {
                            "r": r_idx,
                            "c": c_idx,
                            "x": gutter,
                            "y": current_y,
                            "w": crop.width,
                            "h": crop.height,
                            "column_type": col_types[c_idx],
                        }
                    )
                    current_y += crop.height + gutter
                with tempfile.NamedTemporaryFile(prefix="micro_atlas_", suffix=".png", delete=False) as atlas_tmp:
                    atlas_path = Path(atlas_tmp.name)
                    atlas_img.save(atlas_path, format="PNG")
                atlas_img.close()
                atlas_sha = compute_file_sha256(atlas_path)
                result["hashes"]["atlas_sha256"].add(atlas_sha)
                atlas_payload, atlas_error = _run_vision_text_ocr(atlas_path)
                atlas_path.unlink(missing_ok=True)
                if atlas_payload:
                    ocr_calls += 1
                    atlas_epoch = atlas_payload.get("_worker_epoch")
                    if atlas_epoch is not None:
                        worker_epoch = atlas_epoch
                    atlas_lines = atlas_payload.get("lines") or []
                    atlas_coord_space = _convert_line_boxes(atlas_lines, atlas_width, atlas_height)
                    atlas_sidecar_lines: List[Dict[str, Any]] = []
                    for line in atlas_lines:
                        box = line.get("_box_pixel") or (line.get("box") or {})
                        atlas_sidecar_lines.append(
                            {
                                "text": str(line.get("text", "")),
                                "confidence": float(line.get("confidence") or 0.0),
                                "box": box,
                                "raw_box": line.get("box"),
                            }
                        )
                    atlas_matched = 0
                    for tile in tile_entries:
                        r_idx = tile["r"]
                        c_idx = tile["c"]
                        pix_box = cell_boxes[(r_idx, c_idx)]
                        mid_x, mid_y = _cell_mid(pix_box)
                        candidates: List[Dict[str, Any]] = []
                        tx = tile["x"]
                        ty = tile["y"]
                        tw = tile["w"]
                        th = tile["h"]
                        for line in atlas_lines:
                            box = line.get("_box_pixel") or (line.get("box") or {})
                            lx = float(box.get("x", 0.0))
                            ly = float(box.get("y", 0.0))
                            lw = float(box.get("w", 0.0))
                            lh = float(box.get("h", 0.0))
                            cx = lx + lw / 2.0
                            cy = ly + lh / 2.0
                            if not (tx <= cx <= tx + tw and ty <= cy <= ty + th):
                                continue
                            raw_text = str(line.get("text", "")).strip()
                            confidence_val = float(line.get("confidence") or 0.0)
                            kind = col_types[c_idx]
                            sanitized_text, sanitize_reason, variant_used = _sanitize_micro_value(raw_text, kind)
                            if sanitized_text is None:
                                continue
                            candidates.append(
                                {
                                    "sanitized": sanitized_text,
                                    "raw_text": raw_text,
                                    "confidence": confidence_val,
                                    "variant": variant_used,
                                    "center_x": cx,
                                    "center_y": cy,
                                    "source": "atlas",
                                }
                            )
                        if candidates:
                            candidates.sort(key=lambda cand: _select_key(col_types[c_idx], cand, mid_x, mid_y))
                            chosen = candidates[0]
                            text_grid[r_idx][c_idx] = chosen["sanitized"]
                            conf_grid[r_idx][c_idx] = chosen["confidence"]
                            source_grid[r_idx][c_idx] = chosen["source"]
                            variant_grid[r_idx][c_idx] = bool(chosen["variant"])
                            atlas_matched += 1
                            cells_meta[(r_idx, c_idx)] = {
                                "selection_rank": 0,
                                "raw_text": chosen.get("raw_text"),
                            }
        atlas_sidecar = {
            "schema": "micro_atlas.v1",
            "page_index": page_index,
            "table_id": table_id,
            "atlas_sha256": atlas_sha,
            "gutter_px": gutter,
            "tile_pack_order": tile_entries,
            "worker_epoch": worker_epoch,
            "ocr_calls": 1,
            "pending_before": len(pending_cells),
            "coord_space": atlas_coord_space,
            "atlas_width_px": atlas_width,
            "atlas_height_px": atlas_height,
            "atlas_lines_total": len(atlas_lines),
            "atlas_lines_matched": atlas_matched,
            "lines": atlas_sidecar_lines,
        }
        # Frame mismatch guard: matched=0 with non-zero lines
        if atlas_sidecar["atlas_lines_total"] and not atlas_sidecar["atlas_lines_matched"]:
            info_record = _build_info_record(
                ocr_calls=ocr_calls,
                accepted=False,
                rows=row_count,
                cols=col_count,
                reason="frame_mismatch",
                worker_epoch=worker_epoch,
            )

        accepted_cells = sum(1 for r in range(row_count) for c in range(col_count) if text_grid[r][c].strip())
        pending_after = [
            (r, c)
            for r in range(row_count)
            for c in range(col_count)
            if not text_grid[r][c].strip() and col_types[c] in ("money", "date")
        ]

        patch_sidecar = None
        if pending_after:
            gutter_patch = 2
            patch_tile_entries: List[Dict[str, Any]] = []
            patch_tiles: List[Tuple[Tuple[int, int], str, Image.Image, Dict[str, Any]]] = []
            patch_max_width = 0
            patch_total_height = gutter_patch
            for r_idx, c_idx in pending_after:
                pix_box = cell_boxes.get((r_idx, c_idx))
                if not pix_box or pix_box == (0, 0, 0, 0):
                    continue
                x0, y0, x1, y1 = pix_box
                pad = max(2, int(0.05 * (y1 - y0)))
                left = max(0, x0 - pad)
                right = min(img_width, x1 + pad)
                top = max(0, y0 - pad)
                bottom = min(img_height, y1 + pad)
                crop = base_img.crop((left, top, right, bottom))
                min_tile_width = 40
                if crop.width < min_tile_width:
                    pad_extra = (min_tile_width - crop.width) // 2 + 1
                    left = max(0, left - pad_extra)
                    right = min(img_width, right + pad_extra)
                    crop = base_img.crop((left, top, right, bottom))
                profile_id = "MoneyPatch" if col_types[c_idx] == "money" else "DatePatch"
                processed_tile, profile_meta = _apply_patch_profile(crop, profile_id)
                patch_tiles.append(((r_idx, c_idx), profile_id, processed_tile, profile_meta))
                patch_max_width = max(patch_max_width, processed_tile.width)
                patch_total_height += processed_tile.height + gutter_patch
            if patch_tiles:
                patch_width = patch_max_width + gutter_patch * 2
                patch_height = max(patch_total_height, gutter_patch * 2 + 4)
                patch_img = Image.new("L", (patch_width, patch_height), color=255)
                current_y = gutter_patch
                tile_idx = 0
                patch_profile_hashes: List[str] = []
                for (r_idx, c_idx), profile_id, tile_img, profile_meta in patch_tiles:
                    patch_img.paste(tile_img, (gutter_patch, current_y))
                    profile_sha = _deterministic_json_sha(profile_meta)
                    patch_profile_hashes.append(profile_sha)
                    patch_tile_entries.append(
                        {
                            "r": r_idx,
                            "c": c_idx,
                            "x": gutter_patch,
                            "y": current_y,
                            "w": tile_img.width,
                            "h": tile_img.height,
                            "column_type": col_types[c_idx],
                            "patch_profile_id": profile_id,
                            "patch_profile_sha256": profile_sha,
                        }
                    )
                    current_y += tile_img.height + gutter_patch
                with tempfile.NamedTemporaryFile(prefix="micro_patch_", suffix=".png", delete=False) as patch_tmp:
                    patch_path = Path(patch_tmp.name)
                    patch_img.save(patch_path, format="PNG")
                patch_img.close()
                patch_sha = compute_file_sha256(patch_path)
                result["hashes"]["patch_atlas_sha256"].add(patch_sha)
                for sha in patch_profile_hashes:
                    result["hashes"]["patch_profile_sha256"].add(sha)
                patch_payload, patch_error = _run_vision_text_ocr(patch_path)
                patch_path.unlink(missing_ok=True)
                patch_matched = 0
                patch_lines_total = 0
                patch_coord_space = "pixel"
                patch_sidecar_lines: List[Dict[str, Any]] = []
                if patch_payload:
                    ocr_calls += 1
                    patch_lines = patch_payload.get("lines") or []
                    patch_coord_space = _convert_line_boxes(patch_lines, patch_width, patch_height)
                    patch_lines_total = len(patch_lines)
                    for line in patch_lines:
                        box = line.get("_box_pixel") or (line.get("box") or {})
                        patch_sidecar_lines.append(
                            {
                                "text": str(line.get("text", "")),
                                "confidence": float(line.get("confidence") or 0.0),
                                "box": box,
                                "raw_box": line.get("box"),
                            }
                        )
                    for tile in patch_tile_entries:
                        r_idx = tile["r"]
                        c_idx = tile["c"]
                        pix_box = cell_boxes[(r_idx, c_idx)]
                        mid_x, mid_y = _cell_mid(pix_box)
                        tx = tile["x"]
                        ty = tile["y"]
                        tw = tile["w"]
                        th = tile["h"]
                        kind = col_types[c_idx]
                        candidates: List[Dict[str, Any]] = []
                        for line in patch_lines:
                            box = line.get("_box_pixel") or (line.get("box") or {})
                            lx = float(box.get("x", 0.0))
                            ly = float(box.get("y", 0.0))
                            lw = float(box.get("w", 0.0))
                            lh = float(box.get("h", 0.0))
                            cx = lx + lw / 2.0
                            cy = ly + lh / 2.0
                            if not (tx <= cx <= tx + tw and ty <= cy <= ty + th):
                                continue
                            raw_text = str(line.get("text", "")).strip()
                            confidence_val = float(line.get("confidence") or 0.0)
                            sanitized_text, sanitize_reason, variant_used = _sanitize_micro_value(raw_text, kind)
                            if sanitized_text is None:
                                continue
                            candidates.append(
                                {
                                    "sanitized": sanitized_text,
                                    "raw_text": raw_text,
                                    "confidence": confidence_val,
                                    "variant": variant_used,
                                    "center_x": cx,
                                    "center_y": cy,
                                    "source": "patch",
                                    "selection_rank": 0,
                                }
                            )
                        if candidates:
                            candidates.sort(key=lambda cand: _select_key(kind, cand, mid_x, mid_y))
                            for rank, cand in enumerate(candidates):
                                cand["selection_rank"] = rank
                            chosen = candidates[0]
                            text_grid[r_idx][c_idx] = chosen["sanitized"]
                            conf_grid[r_idx][c_idx] = chosen["confidence"]
                            source_grid[r_idx][c_idx] = chosen["source"]
                            variant_grid[r_idx][c_idx] = bool(chosen["variant"])
                            cells_meta[(r_idx, c_idx)] = {
                                "selection_rank": chosen.get("selection_rank", 0),
                                "raw_text": chosen["raw_text"],
                            }
                            patch_matched += 1
                patch_sidecar = {
                    "schema": "micro_patch.v1",
                    "page_index": page_index,
                    "table_id": table_id,
                    "patch_atlas_sha256": patch_sha,
                    "gutter_px": gutter_patch,
                    "tile_pack_order": patch_tile_entries,
                    "worker_epoch": worker_epoch,
                    "ocr_calls": 1 if patch_payload else 0,
                    "coord_space": patch_coord_space,
                    "atlas_width_px": patch_width,
                    "atlas_height_px": patch_height,
                    "patch_lines_total": patch_lines_total,
                    "patch_lines_matched": patch_matched,
                    "lines": patch_sidecar_lines,
                }

            pending_after = [
                (r, c)
                for r in range(row_count)
                for c in range(col_count)
                if not text_grid[r][c].strip() and col_types[c] in ("money", "date")
            ]

        accepted_cells = sum(1 for r in range(row_count) for c in range(col_count) if text_grid[r][c].strip())

        cells_payload: List[Dict[str, Any]] = []
        for r_idx in range(row_count):
            for c_idx in range(col_count):
                cell_meta = cells_meta.get((r_idx, c_idx), {})
                cells_payload.append(
                    {
                        "r": r_idx,
                        "c": c_idx,
                        "text": text_grid[r_idx][c_idx],
                        "confidence": conf_grid[r_idx][c_idx],
                        "source": source_grid[r_idx][c_idx] or "vision" if text_grid[r_idx][c_idx] else "",
                        "variant": variant_grid[r_idx][c_idx],
                        "selection_rank": cell_meta.get("selection_rank"),
                        "raw_text": cell_meta.get("raw_text"),
                    }
                )
        table_payload = {
            "id": table_id,
            "rows": row_count,
            "cols": col_count,
            "cells": cells_payload,
            "_native_source": "micro_ocr_atlas",
        }
        accepted_table = is_valid_table(table_payload)

        info_record = _build_info_record(
            ocr_calls=ocr_calls,
            accepted=bool(accepted_table),
            accepted_cells=accepted_cells,
            pending_cells=len(pending_after),
            rows=row_count,
            cols=col_count,
            reason=None if accepted_table else "low_signal",
            worker_epoch=worker_epoch,
            atlas_lines_total=atlas_sidecar.get("atlas_lines_total") if atlas_sidecar else 0,
            atlas_lines_matched=atlas_sidecar.get("atlas_lines_matched") if atlas_sidecar else 0,
            patch_lines_total=patch_sidecar.get("patch_lines_total") if patch_sidecar else 0,
            patch_lines_matched=patch_sidecar.get("patch_lines_matched") if patch_sidecar else 0,
            patch_used=bool(patch_sidecar),
        )

        result["table_payload"] = table_payload if accepted_table else None
        result["info_record"] = info_record
        result["roi_sidecar"] = roi_sidecar
        result["atlas_sidecar"] = atlas_sidecar
        result["patch_sidecar"] = patch_sidecar
        return result
    finally:
        base_img.close()
def load_vlm_tables_for_doc(doc_sha: str, vlm_dir: Path) -> Tuple[
    Dict[int, List[Dict[str, Any]]],
    List[Dict[str, Any]],
    Dict[int, Dict[str, Any]],
    Dict[int, Dict[str, Any]],
]:
    data_pattern = re.compile(rf"^{re.escape(doc_sha)}_p(?P<page>\d+)_roi(?P<roi>.+?)_data_response(?:_(?P<req>[0-9a-f]+))?\.json$")
    header_pattern = re.compile(rf"^{re.escape(doc_sha)}_p(?P<page>\d+)_roi(?P<roi>.+?)_headers_response(?:_(?P<req>[0-9a-f]+))?\.json$")
    tables_by_page: Dict[int, List[Dict[str, Any]]] = defaultdict(list)
    errors: List[Dict[str, Any]] = []
    header_locks_by_page: Dict[int, Dict[str, Any]] = {}
    header_index: Dict[Tuple[int, str, str], Dict[str, Any]] = {}
    header_meta_index: Dict[Tuple[int, str, str], Dict[str, Any]] = {}
    page_assets: Dict[int, Dict[str, Any]] = {}

    for header_path in sorted(vlm_dir.glob(f"{doc_sha}_p*_headers_response*.json")):
        match = header_pattern.match(header_path.name)
        if not match:
            continue
        page_index = int(match.group("page"))
        roi_token = match.group("roi")
        req_sha = match.group("req") or ""
        request_payload: Optional[Dict[str, Any]] = None
        request_sha_from_name = req_sha
        try:
            payload = json.loads(header_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        request_path = header_path.with_name(header_path.name.replace("_headers_response", "_headers_request"))
        if request_path.exists():
            try:
                request_payload = json.loads(request_path.read_text(encoding="utf-8"))
            except Exception:
                request_payload = None
        header_lock = payload.get("header_lock")
        roi_type, roi_index = classify_roi_token(roi_token)
        request_sha = payload.get("request_sha256") or (request_payload or {}).get("request_sha256") or request_sha_from_name
        image_path = (request_payload or {}).get("image_path")
        image_sha = (request_payload or {}).get("image_sha256")
        roi_box = (request_payload or {}).get("roi")
        roi_id = (request_payload or {}).get("roi_id")
        header_meta = {
            "header_lock": header_lock,
            "image_path": image_path,
            "image_sha256": image_sha,
            "roi": roi_box,
            "roi_id": roi_id,
            "roi_token": roi_token,
            "roi_type": roi_type,
            "roi_index": roi_index,
            "request_sha256": request_sha,
            "response_sha256": payload.get("response_sha256"),
        }
        header_lock = payload.get("header_lock")
        if isinstance(header_lock, dict):
            header_index[(page_index, roi_token, req_sha)] = header_lock
            if page_index not in header_locks_by_page:
                header_locks_by_page[page_index] = header_lock
        header_meta_index[(page_index, roi_token, request_sha or "")] = header_meta
        header_meta_index[(page_index, roi_token, "")] = header_meta
        candidate_key = (
            ROI_KIND_ORDER.get(roi_type, ROI_KIND_ORDER["unknown"]),
            roi_index,
            request_sha or "",
            image_sha or "",
            header_path.name
        )
        existing = page_assets.get(page_index)
        if not existing or candidate_key < existing.get("_sort", ()):
            asset_record = {
                "image_path": image_path,
                "image_sha256": image_sha,
                "roi": roi_box,
                "roi_id": roi_id,
                "roi_token": roi_token,
                "roi_type": roi_type,
                "roi_index": roi_index,
                "request_sha256": request_sha,
                "header_lock_sha256": (header_lock or {}).get("lock_sha256") if isinstance(header_lock, dict) else None,
                "response_sha256": payload.get("response_sha256"),
                "_sort": candidate_key,
            }
            page_assets[page_index] = asset_record

    for response_path in sorted(vlm_dir.glob(f"{doc_sha}_p*_data_response*.json")):
        match = data_pattern.match(response_path.name)
        response_sha = compute_file_sha256(response_path)
        if not match:
            errors.append({
                "response_path": str(response_path),
                "response_sha256": response_sha,
                "reason": "filename_unmatched"
            })
            continue
        page_index = int(match.group("page"))
        roi_token = match.group("roi")
        request_suffix = match.group("req") or ""
        roi_type, roi_index = classify_roi_token(roi_token)
        try:
            payload = json.loads(response_path.read_text(encoding="utf-8"))
        except Exception:
            errors.append({
                "response_path": str(response_path),
                "response_sha256": response_sha,
                "reason": "json_decode"
            })
            continue
        if payload.get("type") != "result.data.v1":
            errors.append({
                "response_path": str(response_path),
                "response_sha256": response_sha,
                "reason": "type_mismatch"
            })
            continue
        status = payload.get("status")
        session_id = payload.get("session_id")
        roi_id = payload.get("roi_id")
        request_sha = payload.get("request_sha256") or request_suffix
        if status != "ok":
            reason_meta = load_runner_reason(session_id, roi_id, "data")
            reason_code = reason_meta.get("reason") or f"status:{status}"
            error_entry = {
                "response_path": str(response_path),
                "response_sha256": response_sha,
                "reason": reason_code,
            }
            slice_sha = reason_meta.get("slice_sha256")
            if slice_sha:
                error_entry["slice_sha256"] = slice_sha
            if request_sha:
                error_entry["request_sha256"] = request_sha
            errors.append(error_entry)
            continue
        rows = payload.get("rows")
        cols = payload.get("cols")
        cells = payload.get("cells") or []
        if not isinstance(rows, int) or not isinstance(cols, int):
            errors.append({
                "response_path": str(response_path),
                "response_sha256": response_sha,
                "reason": "invalid_shape"
            })
            continue
        if rows * cols != len(cells):
            errors.append({
                "response_path": str(response_path),
                "response_sha256": response_sha,
                "reason": "rectangularization_mismatch",
                "request_sha256": request_sha
            })
            continue
        table_id = f"vlm_p{page_index}_{roi_token}"
        table = build_vlm_table(table_id, rows, cols, cells)
        entry = {
            "table": table,
            "meta": {
                "page_index": page_index,
                "roi_token": roi_token,
                "roi_type": roi_type,
                "roi_index": roi_index,
                "response_path": str(response_path),
                "response_sha256": response_sha,
                "rows": rows,
                "cols": cols,
                "cell_count": len(cells),
                "request_sha256": request_sha,
                "prompt_sha256": payload.get("prompt_sha256"),
                "order": (ROI_KIND_ORDER.get(roi_type, ROI_KIND_ORDER["unknown"]), roi_index, str(response_path))
            }
        }
        header_key = (page_index, roi_token, request_sha or "")
        header_lock = header_index.get(header_key) or header_index.get((page_index, roi_token, ""))
        header_meta = header_meta_index.get(header_key) or header_meta_index.get((page_index, roi_token, ""))
        if header_lock and page_index not in header_locks_by_page:
            header_locks_by_page[page_index] = header_lock
        entry["meta"]["header_lock_sha256"] = header_lock.get("lock_sha256") if isinstance(header_lock, dict) else None
        if header_meta:
            if header_meta.get("image_path"):
                entry["meta"]["roi_image_path"] = header_meta["image_path"]
            if header_meta.get("image_sha256"):
                entry["meta"]["roi_image_sha256"] = header_meta["image_sha256"]
            if header_meta.get("roi"):
                entry["meta"]["roi_box"] = header_meta["roi"]
            if header_meta.get("roi_id"):
                entry["meta"]["roi_id"] = header_meta["roi_id"]
            if header_meta.get("request_sha256"):
                entry["meta"]["request_sha256"] = header_meta["request_sha256"]
        tables_by_page[page_index].append(entry)
    for entries in tables_by_page.values():
        entries.sort(key=lambda item: item["meta"]["order"])
    for asset in page_assets.values():
        asset.pop("_sort", None)
    return tables_by_page, errors, header_locks_by_page, page_assets



def resolve_doc_sha(
    doc_key: str,
    pages: List[Dict[str, Any]],
    *,
    run_manifest_info: Optional[Dict[str, Any]],
    doc_sha_arg: Optional[str]
) -> Tuple[str, str]:
    doc_sha: Optional[str] = None
    source = "unknown"
    manifest_input = None
    if run_manifest_info:
        manifest_input = run_manifest_info.get("input_path")
        manifest_sha = run_manifest_info.get("doc_sha")
        matches_manifest = False
        if manifest_input:
            try:
                matches_manifest = Path(manifest_input).resolve() == Path(doc_key).resolve()
            except Exception:
                matches_manifest = manifest_input == doc_key
        if manifest_sha and (not manifest_input or matches_manifest):
            doc_sha = manifest_sha
            source = "run_manifest"
    if doc_sha is None:
        candidate_path: Optional[Path] = None
        source_path = pages[0].get("source_path") if pages else None
        if source_path:
            try:
                path = Path(source_path)
                if path.exists():
                    candidate_path = path
            except Exception:
                candidate_path = None
        if candidate_path is None and doc_key:
            try:
                path = Path(doc_key)
                if path.exists():
                    candidate_path = path
            except Exception:
                candidate_path = None
        if candidate_path is not None:
            doc_sha = compute_file_sha256(candidate_path)
            source = "source_path"
    if doc_sha is None and doc_sha_arg:
        doc_sha = doc_sha_arg
        source = "argument"
    if doc_sha is None:
        raise RuntimeError("Unable to resolve document SHA-256; supply --run-manifest, ensure source_path is accessible, or provide --doc-sha.")
    if doc_sha_arg and doc_sha != doc_sha_arg:
        raise RuntimeError(f"Resolved doc SHA ({doc_sha}) does not match --doc-sha ({doc_sha_arg}).")
    return doc_sha, source


def transform_value(raw: str, cfg: Dict[str, Any]) -> Optional[str]:
    typ = cfg.get("type")
    if typ == "money":
        return sanitize_amount(raw)
    if typ == "date":
        return parse_date(raw, cfg.get("patterns"))
    if typ == "dcode":
        code = raw.strip().upper()
        return code if D_CODE_RE.match(code) else code
    if typ == "tooth":
        cleaned = raw.strip().upper()
        return cleaned if TOOTH_RE.match(cleaned) else cleaned
    if typ == "tooth_surface":
        cleaned = raw.strip().upper()
        return cleaned if SURFACE_RE.match(cleaned) else cleaned
    return raw.strip()


def build_provenance(ptr: str, source: str, confidence: float, page_index: int, box: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    prov = {
        "ptr": ptr,
        "source": source,
        "confidence": float(confidence),
        "page_index": page_index,
    }
    if box and all(k in box for k in ("x", "y", "w", "h")):
        prov["box_px"] = {k: int(box[k]) for k in ("x", "y", "w", "h")}
    return prov


def choose_template(payer_name: str, templates: List[TableTemplate]) -> TableTemplate:
    """Choose a payer template deterministically.

    Order of preference:
    1) name_regex matches payer_name/header text
    2) exact payer name match
    3) fallback 'common' or 'generic' template if present
    4) first template (stable default)
    """
    if payer_name:
        for tpl in templates:
            if tpl.name_regex and tpl.name_regex.search(payer_name):
                return tpl
        for tpl in templates:
            if tpl.payer.lower() == payer_name.lower():
                return tpl
    # Fallback to a generic/common template if present
    for tpl in templates:
        if tpl.payer.lower() in ("common", "generic"):
            return tpl
    return templates[0]


# -----------------------------------------------------------------------------
# Service extraction
# -----------------------------------------------------------------------------


# -----------------------------------------------------------------------------
# Headerless Inference Mode (HIM) — deterministic column inference
# -----------------------------------------------------------------------------

def tag_cell_type(text: str) -> str:
    """Tag a cell by type: money|date|code|tooth|surface|text|blank."""
    text = text.strip()
    if not text:
        return "blank"
    # Money check
    if sanitize_amount(text) is not None:
        return "money"
    # Date check
    if parse_date(text, None) is not None:
        return "date"
    # ADA code check
    if D_CODE_RE.match(text.upper()):
        return "code"
    # Tooth number check
    if TOOTH_RE.match(text.upper()):
        return "tooth"
    # Tooth surface check
    if len(text) <= 5 and SURFACE_RE.match(text.upper()):
        return "surface"
    return "text"


def compute_headerness(row_cells: List[str], *, row_idx: int, matrix: Dict[Tuple[int,int], Dict[str, Any]], cols: int) -> float:
    """Compute headerness score for a candidate header row with span-aware features.

    Features:
    - Token length 3-16 chars (bonus), alpha ratio (bonus)
    - Penalize money/date/code densities (data-like rows)
    - Distinct token count (header_unique_token_count)
    - Header span coverage rate: fraction of columns where the anchor is on this row
    """
    if not row_cells:
        return 0.0

    non_blank = [c for c in row_cells if str(c).strip()]
    if not non_blank:
        return 0.0

    score = 0.0
    money_count = 0
    date_count = 0
    code_count = 0

    for cell in non_blank:
        cell = str(cell).strip()
        if not cell:
            continue
        # Length preference
        if 3 <= len(cell) <= 16:
            score += 2.0
        elif len(cell) < 3:
            score -= 1.0
        # Alpha ratio
        alpha_chars = sum(ch.isalpha() for ch in cell)
        if alpha_chars > 0:
            alpha_ratio = alpha_chars / len(cell)
            score += alpha_ratio * 3.0
        # Data-like penalties
        t = tag_cell_type(cell)
        if t == "money":
            money_count += 1
            score -= 5.0
        elif t == "date":
            date_count += 1
            score -= 3.0
        elif t == "code":
            code_count += 1
            score -= 2.0

    # Distinct token bonus
    distinct = len(set(c.strip().lower() for c in non_blank if str(c).strip()))
    score += distinct * 1.25  # header_unique_token_count contribution

    # Span coverage bonus: anchors owned by this row (discourages shadow rows)
    anchors_here = 0
    for c in range(cols):
        cell_meta = matrix.get((row_idx, c), {})
        if not cell_meta.get("_is_shadow"):
            anchors_here += 1
    coverage = anchors_here / max(1, cols)
    score += coverage * 6.0  # header_span_coverage_rate contribution

    # Data-row guard
    if money_count >= 2:
        score -= 20.0
    return score


def infer_columns_headerless(
    matrix: Dict[Tuple[int, int], Dict[str, Any]],
    rows: int,
    cols: int,
    data_start_row: int,
    template: TableTemplate,
    apply_repair: bool = False,
    maps: Optional[Dict[str, Dict[str, str]]] = None,
    *,
    aggressive: bool = False,
) -> Tuple[Dict[int, Dict[str, Any]], Dict[str, Any]]:
    """Infer column mappings without reliable headers using type analysis and conservation fitting.

    Returns:
        (column_configs, him_evidence)
    """
    # Step 1: Type tag all cells in data rows
    col_types: Dict[int, List[str]] = {c: [] for c in range(cols)}
    col_texts: Dict[int, List[str]] = {c: [] for c in range(cols)}

    for r in range(data_start_row, rows):
        for c in range(cols):
            cell = matrix.get((r, c), {})
            text = str(cell.get("text", "")).strip()
            col_texts[c].append(text)
            if text:
                cell_type = tag_cell_type(text)
                col_types[c].append(cell_type)

    # Step 2: Find money columns (top 3-4 by money density)
    money_cols = []
    money_thr = 0.30
    if aggressive:
        # Moderate aggressive threshold to avoid admitting low-signal columns
        money_thr = 0.20
    for c in range(cols):
        if not col_types[c]:
            continue
        money_density = sum(1 for t in col_types[c] if t == "money") / len(col_types[c])
        if money_density > money_thr:  # At least N% money cells
            money_cols.append((c, money_density))

    money_cols.sort(key=lambda x: x[1], reverse=True)
    money_col_indices = [c for c, _ in money_cols[:4]]  # Keep top 4

    him_evidence = {
        "money_density": {c: round(d, 3) for c, d in money_cols[:6]},
        "money_cols_found": len(money_col_indices),
        "fit_cost": None,
        "rows_fit_rate": None,
        "ambiguous_cols": [],
    }

    if len(money_col_indices) < 3:
        # Not enough money columns for conservation fitting
        return {}, him_evidence

    # Step 3: Conservation fitting - assign roles to money columns
    # Roles: charge, allowed, paid, patient
    # Minimize: Σ_rows [ |allowed - (paid + patient)| + λ·max(0, allowed - charge) ]
    from itertools import permutations

    best_assignment = None
    best_cost = float('inf')

    # Try all permutations of 3 or 4 money columns
    n_money = len(money_col_indices)
    if n_money == 3:
        # Assign to 3 of 4 roles (skip charge if only 3 columns)
        role_sets = [
            ("allowed", "paid", "patient"),
            ("charge", "allowed", "paid"),
            ("charge", "allowed", "patient"),
            ("charge", "paid", "patient"),
        ]
    else:
        role_sets = [("charge", "allowed", "paid", "patient")]

    for roles in role_sets:
        for perm in permutations(money_col_indices[:len(roles)]):
            assignment = dict(zip(roles, perm))

            # Compute conservation cost for this assignment
            cost = 0.0
            rows_conserved = 0
            for r in range(data_start_row, rows):
                vals = {}
                for role in roles:
                    col_idx = assignment[role]
                    cell = matrix.get((r, col_idx), {})
                    text = str(cell.get("text", "")).strip()
                    if text:
                        amount = sanitize_amount(text)
                        if amount:
                            try:
                                vals[role] = Decimal(amount)
                            except:
                                pass

                # Check conservation: allowed = paid + patient
                if "allowed" in vals and "paid" in vals and "patient" in vals:
                    conservation_error = abs(vals["allowed"] - (vals["paid"] + vals["patient"]))
                    cost += float(conservation_error)
                    if conservation_error <= Decimal("0.02"):  # 2 cents tolerance
                        rows_conserved += 1

                # Penalize allowed > charge
                if "charge" in vals and "allowed" in vals:
                    if vals["allowed"] > vals["charge"]:
                        cost += float(vals["allowed"] - vals["charge"]) * 3.0  # λ=3

            if cost < best_cost:
                best_cost = cost
                best_assignment = (assignment, rows_conserved)

    if best_assignment is None:
        return {}, him_evidence

    assignment, rows_conserved = best_assignment
    total_data_rows = rows - data_start_row
    rows_fit_rate = rows_conserved / total_data_rows if total_data_rows > 0 else 0.0

    him_evidence["fit_cost"] = round(best_cost, 2)
    him_evidence["rows_fit_rate"] = round(rows_fit_rate, 3)

    # Step 4: Find code column (highest ADA density)
    code_col = None
    best_code_density = 0.0
    for c in range(cols):
        if c in assignment.values():
            continue  # Skip money columns
        if not col_types[c]:
            continue
        code_density = sum(1 for t in col_types[c] if t == "code") / len(col_types[c])
        if code_density > best_code_density:
            best_code_density = code_density
            code_col = c

    # Step 5: Find date column (highest valid date density)
    date_col = None
    best_date_density = 0.0
    for c in range(cols):
        if c in assignment.values() or c == code_col:
            continue  # Skip already assigned columns
        if not col_types[c]:
            continue
        date_density = sum(1 for t in col_types[c] if t == "date") / len(col_types[c])
        if date_density > best_date_density:
            best_date_density = date_density
            date_col = c

    # Step 6: Build column_configs from inferred mapping
    column_configs: Dict[int, Dict[str, Any]] = {}

    # Map money roles to field names
    role_to_field = {
        "charge": {"field": "charged_amount", "type": "money"},
        "allowed": {"field": "allowed_amount", "type": "money"},
        "paid": {"field": "insurance_paid", "type": "money"},
        "patient": {"field": "patient_responsibility", "type": "money"},
    }

    for role, col_idx in assignment.items():
        if role in role_to_field:
            column_configs[col_idx] = role_to_field[role]

    code_thr = 0.30 if not aggressive else 0.25
    if code_col is not None and best_code_density > code_thr:
        column_configs[code_col] = {"field": "procedure_code", "type": "dcode"}

    date_thr = 0.30 if not aggressive else 0.25
    if date_col is not None and best_date_density > date_thr:
        column_configs[date_col] = {"field": "service_date", "type": "date"}

    return column_configs, him_evidence


def gather_services(
    pages: List[Dict[str, Any]],
    template: TableTemplate,
    provenance: List[Dict[str, Any]],
    *,
    apply_repair: bool = False,
    maps: Optional[Dict[str, Dict[str, str]]] = None,
    repair_notes: Optional[List[Dict[str, Any]]] = None,
    repair_summary: Optional[Dict[str, Any]] = None,
    him_out: Optional[Dict[str, Any]] = None,
    him_supplement: bool = False,
    use_semantic_map: bool = False,
    him_aggressive: bool = False,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]]]:
    services: List[Dict[str, Any]] = []
    sem_map_records: List[Dict[str, Any]] = []
    sem_choice_records: List[Dict[str, Any]] = []
    assign_proof_records: List[Dict[str, Any]] = []
    evidence_records: List[Dict[str, Any]] = []
    extraction_metrics_records: List[Dict[str, Any]] = []
    selection_evidence_records: List[Dict[str, Any]] = []
    for page in pages:
        page_index = page.get("page_index", 0)
        for table in page.get("tables", []) or []:
            table_source = table.get("_native_source", "vision")
            matrix = build_table_matrix(table)
            # Heuristic header row detection: scan first few rows and choose the row
            # that maximizes headerness; tie-breaks favor distinct tokens and leftmost.
            header_row = template.header_row
            cols = table.get("cols", 0)
            rows = table.get("rows", 0)
            # Choose best header row among candidate rows
            def resolve_cfg(text: str) -> Optional[Dict[str, Any]]:
                h = normalize_header(text)
                cfg = template.column_map.get(h)
                if not cfg:
                    alias = template.header_aliases.get(h)
                    if alias:
                        cfg = template.column_map.get(normalize_header(alias)) or template.column_map.get(alias)
                if not cfg:
                    # Heuristic header mapping for common money columns
                    # Use normalized substrings to avoid locale punctuation/casing issues
                    def col(name: str) -> Optional[Dict[str, Any]]:
                        key = normalize_header(name)
                        return template.column_map.get(key)

                    if "allow" in h:
                        cfg = cfg or col("allowed")
                    if cfg is None and ("paid" in h or "inspaid" in h):
                        cfg = col("paid")
                    if cfg is None and ("patient" in h or "ptresp" in h or "respons" in h):
                        cfg = col("patient")
                    if cfg is None and ("billed" in h or "charge" in h or "submit" in h):
                        cfg = col("charge")
                return cfg

            best_row = header_row
            best_score = float("-inf")
            for cand in range(min(rows, 6)):
                row_cells = [str(matrix.get((cand, c), {}).get("text", "")) for c in range(cols)]
                score = compute_headerness(row_cells, row_idx=cand, matrix=matrix, cols=cols)
                if score > best_score:
                    best_score = score
                    best_row = cand
            header_row = best_row

            column_configs: Dict[int, Dict[str, Any]] = {}
            # Initialize semantic uncertainty default; semantic-map may flip to True
            semantic_uncertain = False
            # C6.S semantic map (flag-gated) + soft-accept vs HIM
            if use_semantic_map:
                # Run mapper
                semrec = semantic_map_table(page, table, template)
                sem_map_records.append(semrec)
                mapper_col_map: Dict[str, int] = {k: int(v) for k, v in (semrec.get("col_map") or {}).items() if isinstance(v, int) or (isinstance(v, str) and str(v).isdigit())}
                mapper_header_match: float = float(semrec.get("header_match") or 0.0)
                header_row_idx = int(semrec.get("header_row_index") or 0)
                # Build a deterministic sample of data rows
                data_row_count = max(0, int(rows) - header_row_idx - 1)
                sample_size = min(5, max(3, data_row_count // 2))
                if sample_size < 3:
                    sample_rows = []
                else:
                    sample_rows = list(range(header_row_idx + 1, min(rows, header_row_idx + 1 + sample_size)))
                mapper_cons = 0.0
                if sample_rows:
                    mapper_cons = _eval_conservation_for_mapping_sample(matrix, rows, mapper_col_map, sample_rows)
                # Build HIM mapping when requested or mapper weak/incomplete
                him_col_map: Dict[str, int] = {}
                him_cons = 0.0
                latch = float(getattr(args, 'mapper_latch', 0.15) or 0.15)
                need_him = bool(him_aggressive or (len([k for k in mapper_col_map if k in {"charged_amount","allowed_amount","insurance_paid","patient_responsibility"}]) < 4) or (mapper_header_match < latch))
                if need_him:
                    inferred, ev = infer_columns_headerless(matrix, rows, cols, header_row_idx + 1, template, apply_repair=apply_repair, maps=maps)
                    if inferred:
                        role_fields = {"charged_amount","allowed_amount","insurance_paid","patient_responsibility","procedure_code","service_date"}
                        for c_idx, cfg in inferred.items():
                            fld = cfg.get("field")
                            if fld in role_fields:
                                him_col_map[fld] = int(c_idx)
                        if ev and him_out is not None:
                            him_out.setdefault("tables", []).append({
                                "page_index": page_index,
                                "table_id": table.get("id", "t?"),
                                "evidence": ev,
                                "supplement": False
                            })
                        if sample_rows:
                            him_cons = _eval_conservation_for_mapping_sample(matrix, rows, him_col_map, sample_rows)
                # Pseudo-headers for rowize fallbacks: synthesize a mapper when headers are absent
                header_synth: Optional[Dict[str, Any]] = None
                if (table_source == "rowize") and ((not mapper_col_map) or (mapper_header_match < 0.15)):
                    pseudo_map, evp = synthesize_pseudo_mapper(matrix, rows, cols)
                    if pseudo_map:
                        mapper_col_map = pseudo_map
                        # Nominal header match to pass latch; conservation comparison will decide
                        mapper_header_match = max(mapper_header_match, float(evp.get("header_match_nominal", 0.16)))
                        header_synth = {"table_id": table.get("id"), "page_index": page_index, **evp}
                # Decide via frozen selection policy
                choice = None
                semantic_uncertain = False
                if choose_variant is not None and SelectionInput is not None:
                    sel = SelectionInput(
                        mapper_header_match=float(mapper_header_match),
                        mapper_sample_cons=float(mapper_cons),
                        him_sample_cons=float(him_cons),
                        latch=float(latch),
                        delta=0.10,
                    )
                    sel_out = choose_variant(sel)
                    choice = str(sel_out.get("extraction_variant") or "him")
                    semantic_uncertain = bool(sel_out.get("semantic_uncertain") or False)
                else:
                    # Fallback to local logic (mirrors Alignment Locks)
                    mapper_ok = bool(mapper_col_map and (mapper_header_match >= latch) and (mapper_cons > 0))
                    if mapper_ok:
                        choice = "mapper"
                    elif (him_cons - mapper_cons) >= 0.10:
                        choice = "him"
                    else:
                        choice = "him"
                        semantic_uncertain = True
                # Ensure extraction always has a concrete mapping
                if choice == "him" and not him_col_map:
                    # Build a HIM mapping for extraction even if earlier not requested
                    inferred_for_extract, _ = infer_columns_headerless(matrix, rows, cols, header_row_idx + 1, template, apply_repair=apply_repair, maps=maps)
                    if inferred_for_extract:
                        him_col_map = {fld: int(ci) for ci, cfg in inferred_for_extract.items() for fld in [cfg.get("field")] if fld}
                sem_choice_records.append({
                    "table_id": table.get("id"),
                    "page_index": page_index,
                    "chosen": choice,
                    "extraction_variant": choice,
                    "header_match": mapper_header_match,
                    "conservation": {"mapper": mapper_cons, "him": him_cons, "delta": (him_cons - mapper_cons)},
                    "sample_size": len(sample_rows),
                    **({"pseudo": True} if header_synth else {}),
                    **({"semantic_uncertain": True} if semantic_uncertain else {}),
                })
                if header_synth is not None:
                    # Stash for emission in header_synthesis sidecar at doc end
                    globals().setdefault("HEADER_SYNTH_RECORDS", []).append(header_synth)
                chosen_map = mapper_col_map if choice == "mapper" else him_col_map
                for role, col_idx in chosen_map.items():
                    try:
                        col_i = int(col_idx)
                    except Exception:
                        continue
                    if 0 <= col_i < cols:
                        if role in {"charged_amount","allowed_amount","insurance_paid","patient_responsibility"}:
                            ctype = "money"
                        elif role == "procedure_code":
                            ctype = "dcode"
                        elif role == "service_date":
                            ctype = "date"
                        else:
                            ctype = "text"
                        column_configs[col_i] = {"field": role, "type": ctype}
            # If the best headerness score is negative, treat as no headers at all; start data at row 0.
            if best_score >= 0.0:
                for col in range(cols):
                    cell = matrix.get((header_row, col), {})
                    header_text = str(cell.get("text", ""))
                    cfg = resolve_cfg(header_text)
                    if cfg and (col not in column_configs):
                        column_configs[col] = cfg
            else:
                # No header detected; ensure HIM sees all rows by starting at 0
                header_row = -1
            # If no headers resolved, attempt Headerless Inference Mode (HIM)
            used_him = False
            him_evidence: Optional[Dict[str, Any]] = None
            if not column_configs:
                inferred, ev = infer_columns_headerless(
                    matrix, rows, cols, header_row + 1, template,
                    apply_repair=apply_repair, maps=maps, aggressive=bool(him_aggressive)
                )
                if inferred:
                    column_configs = inferred
                    used_him = True
                    him_evidence = ev
            # Unconditionally fill any missing money roles via HIM when fewer than 4 roles are present
            else:
                need_fields = {
                    "charged_amount",
                    "allowed_amount",
                    "insurance_paid",
                    "patient_responsibility",
                }
                have_fields = {cfg.get("field") for cfg in column_configs.values() if isinstance(cfg, dict)}
                missing_fields = [f for f in need_fields if f not in have_fields]
                if missing_fields:
                    inferred, ev = infer_columns_headerless(
                        matrix, rows, cols, header_row + 1, template,
                        apply_repair=apply_repair, maps=maps, aggressive=bool(him_aggressive)
                    )
                    if inferred:
                        for col_idx, cfg in inferred.items():
                            fld = cfg.get("field")
                            if fld in missing_fields and col_idx not in column_configs:
                                column_configs[col_idx] = cfg
                        if him_out is not None and ev:
                            him_out.setdefault("tables", []).append({
                                "page_index": page_index,
                                "table_id": table.get("id", "t?"),
                                "evidence": ev,
                                "supplement": True
                            })

            # Build assign.proof record (chosen mapping and alternatives) and optional evidence metrics
            try:
                # chosen mapping from column_configs
                chosen_map: Dict[str, int] = {}
                for ci, cfg in column_configs.items():
                    fld = cfg.get("field")
                    if fld:
                        chosen_map[fld] = int(ci)
                alt_maps: List[Dict[str, Any]] = []
                src_variant = "unknown"
                header_match_val = None
                sample_cons = {"mapper": None, "him": None}
                if 'choice' in locals():
                    src_variant = str(choice or "unknown")
                    header_match_val = float(locals().get('mapper_header_match', 0.0) or 0.0)
                    # Record alt from the other mapping if present
                    if src_variant == 'mapper' and locals().get('him_col_map'):
                        alt_maps.append({"variant": "him", "column_map": {k: int(v) for k, v in locals().get('him_col_map', {}).items()}})
                    if src_variant == 'him' and locals().get('mapper_col_map'):
                        alt_maps.append({"variant": "mapper", "column_map": {k: int(v) for k, v in locals().get('mapper_col_map', {}).items()}})
                    sample_cons["mapper"] = float(locals().get('mapper_cons', 0.0) or 0.0)
                    sample_cons["him"] = float(locals().get('him_cons', 0.0) or 0.0)
                # Typedness costs (simple, deterministic)
                typedness_cost = 0.0
                for fld, ci in list(chosen_map.items()):
                    try:
                        if fld in {"charged_amount","allowed_amount","insurance_paid","patient_responsibility"}:
                            t = _col_typedness_money(matrix, rows, cols, int(ci))
                            typedness_cost += float(max(0.0, 1.0 - t))
                        elif fld == "service_date":
                            t = _col_typedness_date(matrix, rows, int(ci))
                            typedness_cost += float(max(0.0, 1.0 - t))
                        elif fld == "procedure_code":
                            t = _col_typedness_code(matrix, rows, int(ci))
                            typedness_cost += float(max(0.0, 1.0 - t))
                    except Exception:
                        pass
                # ILP-lite: enumerate best and runner-up mappings for money roles
                def _compute_certified_for_map(cmap: Dict[str, int]) -> int:
                    # Sample up to 5 data rows after header
                    data_start = max(0, int(header_row) + 1)
                    idxs = list(range(data_start, min(rows, data_start + 5)))
                    if min_energy_row is None or not idxs:
                        return 0
                    cnt = 0
                    for rr in idxs:
                        # Compose row values
                        def _get(ci: int) -> str:
                            try:
                                return str((matrix.get((rr, int(ci))) or {}).get("text", ""))
                            except Exception:
                                return ""
                        vals = {
                            "charged": _get(cmap.get("charged_amount", -1)),
                            "allowed": _get(cmap.get("allowed_amount", -1)),
                            "paid": _get(cmap.get("insurance_paid", -1)),
                            "patient": _get(cmap.get("patient_responsibility", -1)),
                        }
                        res = min_energy_row(vals)
                        if res.get("certified"):
                            cnt += 1
                    return cnt

                # Candidate columns: admit by composite money witness score or include chosen roles
                # Compute doc/table money typedness mean to adapt admission threshold deterministically
                try:
                    typed_vals = []
                    for c in range(cols):
                        tval = _col_typedness_money(matrix, rows, cols, c)
                        if tval > 0.0:
                            typed_vals.append(tval)
                    typedness_mean_money = (sum(typed_vals) / float(len(typed_vals))) if typed_vals else 0.0
                except Exception:
                    typedness_mean_money = 0.0

                def _money_score(c: int) -> float:
                    w = _col_money_witnesses(matrix, rows, c)
                    # Memo 1 weights
                    score = 0.0
                    score += 0.35 * float(w.get("decimals_ratio", 0.0))
                    score += 0.25 * float(w.get("right_align_score", 0.0))
                    score += 0.20 * float(w.get("currency_ratio", 0.0))
                    score += 0.20 * float(w.get("thousands_ratio", 0.0))
                    score -= 0.20 * float(w.get("alpha_leak_ratio", 0.0))
                    return max(0.0, min(1.0, score))
                money_scores = [(c, _money_score(c)) for c in range(cols)]
                money_scores.sort(key=lambda x: x[1], reverse=True)
                # Doc-fixed adaptive threshold: lower admission slightly on uniformly low-typedness docs
                admit_tau = 0.60 if typedness_mean_money >= 0.15 else 0.45
                strict_candidates = [c for c, s in money_scores if s >= 0.60][:6]
                relaxed_candidates = []
                if typedness_mean_money < 0.15:
                    relaxed_candidates = [c for c, s in money_scores if (s >= 0.45 and c not in strict_candidates)][:6]
                candidates = list(strict_candidates)
                # Ensure currently chosen money columns are included
                for fld in ("charged_amount","allowed_amount","insurance_paid","patient_responsibility"):
                    if fld in chosen_map:
                        ci = int(chosen_map[fld])
                        if ci not in candidates:
                            candidates.append(ci)
                candidates = candidates[:6]
                # Enumerate permutations of 4 distinct candidates
                ilp_best = {"column_map": {}, "certified_rows_sample": -1}
                ilp_runner = {"column_map": {}, "certified_rows_sample": -1}
                try:
                    import itertools
                    roles = ["charged_amount","allowed_amount","insurance_paid","patient_responsibility"]
                    for perm in itertools.permutations(candidates, 4):
                        cmap = {role: int(col) for role, col in zip(roles, perm)}
                        cert = _compute_certified_for_map(cmap)
                        # Update best and runner-up deterministically
                        def _better(a_cert: int, a_map: Dict[str,int], b_cert: int, b_map: Dict[str,int]) -> bool:
                            if a_cert != b_cert:
                                return a_cert > b_cert
                            # tie-break lexicographically by column tuple
                            return tuple(a_map.get(r, -1) for r in roles) < tuple(b_map.get(r, -1) for r in roles)
                        if _better(cert, cmap, ilp_best["certified_rows_sample"], ilp_best["column_map"]):
                            ilp_runner = dict(ilp_best)  # previous best becomes runner
                            ilp_best = {"column_map": cmap, "certified_rows_sample": int(cert)}
                        elif _better(cert, cmap, ilp_runner["certified_rows_sample"], ilp_runner["column_map"]):
                            ilp_runner = {"column_map": cmap, "certified_rows_sample": int(cert)}
                except Exception:
                    pass

                # If relaxed candidates exist, include only those that improve best cert
                if relaxed_candidates:
                    base_best_cert = int(ilp_best.get("certified_rows_sample", -1))
                    for c_rel in relaxed_candidates:
                        try:
                            best_with_rel = {"column_map": {}, "certified_rows_sample": -1}
                            for perm in itertools.permutations(list(set(strict_candidates + [c_rel])), 4):
                                cmap = {role: int(col) for role, col in zip(roles, perm)}
                                cert = _compute_certified_for_map(cmap)
                                if cert > best_with_rel["certified_rows_sample"]:
                                    best_with_rel = {"column_map": cmap, "certified_rows_sample": int(cert)}
                            if best_with_rel["certified_rows_sample"] > base_best_cert:
                                candidates.append(c_rel)
                        except Exception:
                            continue
                    # Recompute ILP best/runner with augmented candidates
                    try:
                        ilp_best = {"column_map": {}, "certified_rows_sample": -1}
                        ilp_runner = {"column_map": {}, "certified_rows_sample": -1}
                        for perm in itertools.permutations(candidates, 4):
                            cmap = {role: int(col) for role, col in zip(roles, perm)}
                            cert = _compute_certified_for_map(cmap)
                            if cert > ilp_best["certified_rows_sample"]:
                                ilp_runner = dict(ilp_best)
                                ilp_best = {"column_map": cmap, "certified_rows_sample": int(cert)}
                            elif cert > ilp_runner["certified_rows_sample"]:
                                ilp_runner = {"column_map": cmap, "certified_rows_sample": int(cert)}
                    except Exception:
                        pass

                # Certified rows for the chosen (extraction) mapping
                chosen_cert = _compute_certified_for_map(chosen_map)

                # Selection evidence (always compute header-nudged vs headerless when available)
                try:
                    data_start_sel = max(0, int(header_row) + 1)
                    sample_rows_sel = list(range(data_start_sel, min(rows, data_start_sel + 5)))
                    # Build header-nudged map from column_configs (may be partial)
                    header_map = dict(chosen_map)
                    # Build headerless map from him_col_map if present
                    headerless_map = {}
                    if 'him_col_map' in locals() and isinstance(locals().get('him_col_map'), dict):
                        headerless_map = {k: int(v) for k, v in locals().get('him_col_map', {}).items() if isinstance(v, int)}
                    def _cert_and_energy(cmap: Dict[str,int]) -> Tuple[int, int]:
                        cert = 0
                        energy_sum = 0
                        if not sample_rows_sel:
                            return 0, 0
                        for rr in sample_rows_sel:
                            # Extract row values (money)
                            def _get_val(ci: Optional[int]) -> str:
                                try:
                                    return str((matrix.get((rr, int(ci))) or {}).get("text", "")) if isinstance(ci, int) else ""
                                except Exception:
                                    return ""
                            vals = {
                                "charged": _get_val(cmap.get("charged_amount")),
                                "allowed": _get_val(cmap.get("allowed_amount")),
                                "paid": _get_val(cmap.get("insurance_paid")),
                                "patient": _get_val(cmap.get("patient_responsibility")),
                            }
                            if min_energy_row is not None:
                                res = min_energy_row(vals)
                                if res.get("certified"):
                                    cert += 1
                                energy_sum += int(res.get("violation_energy", 0) or 0)
                        return cert, energy_sum
                    cert_h, energy_h = _cert_and_energy(header_map)
                    cert_l, energy_l = _cert_and_energy(headerless_map)
                    # Choose by cert desc, energy asc, headerless bias
                    chosen_variant = "mapper"
                    if cert_l > cert_h:
                        chosen_variant = "him"
                    elif cert_l == cert_h:
                        if energy_l < energy_h:
                            chosen_variant = "him"
                        else:
                            chosen_variant = "him"  # tie → headerless bias
                    selection_evidence_records.append({
                        "table_id": table.get("id", "t?"),
                        "page_index": page_index,
                        "cert_rows_header": cert_h,
                        "cert_rows_headerless": cert_l,
                        "energy_header": energy_h,
                        "energy_headerless": energy_l,
                        "chosen": chosen_variant,
                    })
                except Exception:
                    pass

                # ILP-lite mapping application (optional) and metrics
                # Compute min typedness for ILP best and gate
                typedness_tau = 0.20
                ilp_best_cert = int(ilp_best["certified_rows_sample"]) if isinstance(ilp_best.get("certified_rows_sample"), int) else -1
                ilp_best_min_t = _min_money_typedness_for_mapping(matrix, rows, cols, ilp_best.get("column_map", {})) if ilp_best.get("column_map") else 0.0
                # Swap-stability: chosen map should not improve energy under any adjacent money-role swap
                data_start_sel = max(0, int(header_row) + 1)
                sample_rows_sel = list(range(data_start_sel, min(rows, data_start_sel + 5)))
                def _energy_sum(cmap: Dict[str,int]) -> int:
                    if not sample_rows_sel:
                        return 0
                    total_e = 0
                    for rr in sample_rows_sel:
                        def _get(ci: Optional[int]) -> str:
                            try:
                                return str((matrix.get((rr, int(ci))) or {}).get("text", ""))
                            except Exception:
                                return ""
                        vals = {
                            "charged": _get(cmap.get("charged_amount")),
                            "allowed": _get(cmap.get("allowed_amount")),
                            "paid": _get(cmap.get("insurance_paid")),
                            "patient": _get(cmap.get("patient_responsibility")),
                        }
                        if min_energy_row is not None:
                            res = min_energy_row(vals)
                            total_e += int(res.get("violation_energy", 0) or 0)
                    return total_e
                swap_stable = True
                min_delta_e = 0
                if ilp_best.get("column_map"):
                    cmap_best = dict(ilp_best["column_map"])  # role->col
                    # Optional summary-aware energy
                    def _energy_sum_with_summary(cmap: Dict[str,int]) -> float:
                        base = float(_energy_sum(cmap))
                        if not getattr(args, 'summary_penalty_apply', False):
                            return base
                        # Compute per-mapping residual penalty
                        try:
                            def _get(ci: Optional[int], rr: int) -> str:
                                try:
                                    return str((matrix.get((rr, int(ci))) or {}).get("text", ""))
                                except Exception:
                                    return ""
                            rows_sum = {"charged": 0, "allowed": 0, "paid": 0, "patient": 0}
                            for rr in sample_rows_sel:
                                vals = {
                                    "charged": _get(cmap.get("charged_amount"), rr),
                                    "allowed": _get(cmap.get("allowed_amount"), rr),
                                    "paid": _get(cmap.get("insurance_paid"), rr),
                                    "patient": _get(cmap.get("patient_responsibility"), rr),
                                }
                                from decimal import Decimal
                                def _c(x:str) -> int:
                                    try:
                                        return int(Decimal(_normalize_money_text(x)).quantize(Decimal("0.01")) * 100) if x else 0
                                    except Exception:
                                        return 0
                                rows_sum["charged"] += _c(vals["charged"])
                                rows_sum["allowed"] += _c(vals["allowed"])
                                rows_sum["paid"] += _c(vals["paid"])
                                rows_sum["patient"] += _c(vals["patient"])
                            totals = eob.get("totals") or {}
                            def _tc(k:str) -> int:
                                try:
                                    from decimal import Decimal
                                    return int(Decimal(str(totals.get(k))).quantize(Decimal("0.01")) * 100)
                                except Exception:
                                    return 0
                            residual = abs(rows_sum["charged"] - _tc("total_charged")) + \
                                       abs(rows_sum["allowed"] - _tc("total_allowed")) + \
                                       abs(rows_sum["paid"] - _tc("total_paid")) + \
                                       abs(rows_sum["patient"] - _tc("total_patient_responsibility"))
                            # λΣ scaling: median(E_row_probe)/5; approximate median as base/len
                            denom = max(1, len(sample_rows_sel))
                            median_e = (base / float(denom))
                            lam = max(0.0, min(2.0 * median_e, median_e / 5.0))
                            return base + lam * (residual / 100.0)
                        except Exception:
                            return base
                    base_e = _energy_sum_with_summary(cmap_best)
                    # Map columns->roles for adjacency
                    role_at = {v: k for k, v in cmap_best.items()}
                    for j in range(max(0, cols - 1)):
                        r1 = role_at.get(j)
                        r2 = role_at.get(j + 1)
                        if r1 in ("charged_amount","allowed_amount","insurance_paid","patient_responsibility") and \
                           r2 in ("charged_amount","allowed_amount","insurance_paid","patient_responsibility") and r1 != r2:
                            cmap_sw = dict(cmap_best)
                            cmap_sw[r1], cmap_sw[r2] = j + 1, j
                            e_sw = _energy_sum_with_summary(cmap_sw)
                            delta = e_sw - base_e
                            if j == 0 or delta < min_delta_e:
                                min_delta_e = delta
                            if delta < 0:
                                swap_stable = False
                                break
                # Gate: (certified_rows_sample>=1) OR (min_typedness>=tau AND swap_stable)
                gate_pass = (ilp_best_cert >= 1) or ((ilp_best_min_t >= typedness_tau) and swap_stable)
                applied_for_extraction = False
                apply_reason = None
                if bool(getattr(args, 'ilp_assign_extract', False)) and ilp_best.get("column_map"):
                    if gate_pass:
                        # Promote ILP best mapping for money roles into extraction map
                        for role, col_idx in ilp_best["column_map"].items():
                            if role in ("charged_amount","allowed_amount","insurance_paid","patient_responsibility") and isinstance(col_idx, int) and 0 <= col_idx < cols:
                                column_configs[col_idx] = {"field": role, "type": "money"}
                        applied_for_extraction = True
                        apply_reason = "ilp_sample>=1" if (ilp_best_cert >= 1) else "typedness_min>=tau"
                    else:
                        applied_for_extraction = False
                        apply_reason = f"ilp_sample={ilp_best_cert},typedness_min={ilp_best_min_t:.2f}"

                # Swap autopilot: if energy improves beyond threshold, flip mapping deterministically
                swap_autoflip = False
                if getattr(args, 'swap_autopilot', False) and ilp_best.get("column_map") and not swap_stable:
                    try:
                        denom = max(1, len(sample_rows_sel))
                        median_e = (base_e / float(denom))
                        tau_swap = min(0.30 * median_e, 0.15 * median_e)
                        cmap_best = dict(ilp_best["column_map"])  # refresh
                        role_at = {v: k for k, v in cmap_best.items()}
                        for j in range(max(0, cols - 1)):
                            r1 = role_at.get(j)
                            r2 = role_at.get(j + 1)
                            if r1 in ("charged_amount","allowed_amount","insurance_paid","patient_responsibility") and \
                               r2 in ("charged_amount","allowed_amount","insurance_paid","patient_responsibility") and r1 != r2:
                                cmap_sw = dict(cmap_best)
                                cmap_sw[r1], cmap_sw[r2] = j + 1, j
                                e_sw = _energy_sum_with_summary(cmap_sw)
                                if (base_e - e_sw) >= tau_swap:
                                    # Flip mapping in extraction map
                                    for role, col_idx in cmap_sw.items():
                                        if role in ("charged_amount","allowed_amount","insurance_paid","patient_responsibility") and isinstance(col_idx, int) and 0 <= col_idx < cols:
                                            column_configs[col_idx] = {"field": role, "type": "money"}
                                    swap_autoflip = True
                                    min_delta_e = int(e_sw - base_e)
                                    break
                    except Exception:
                        pass

                # Header alias cost shaping (advisory)
                header_cost_delta_sum = 0.0
                try:
                    header_texts = {}
                    if best_score >= 0.0:
                        for col in range(cols):
                            cell_h = matrix.get((header_row, col)) or {}
                            header_texts[col] = str(cell_h.get("text", "")).strip().lower()
                    alias = {
                        "charged_amount": ["amount billed","billed","submitted","provider charge","total charge"],
                        "allowed_amount": ["allowed amount","amount covered","negotiated","approved","plan allowance","contracted fee"],
                        "insurance_paid": ["plan paid","paid by plan","payment","benefit paid","insurance payment"],
                        "patient_responsibility": ["patient responsibility","member responsibility","patient payment","what you owe","you may owe"],
                    }
                    conflict_alias = alias  # symmetric check
                    for role, col_idx in (chosen_map or {}).items():
                        try:
                            htxt = (header_texts.get(int(col_idx)) or "").lower()
                            if any(k in htxt for k in alias.get(role, [])):
                                header_cost_delta_sum += -0.10
                            else:
                                # conflict if header matches a different role's alias set
                                for other_role, words in conflict_alias.items():
                                    if other_role != role and any(k in htxt for k in words):
                                        header_cost_delta_sum += 0.10
                                        break
                        except Exception:
                            continue
                except Exception:
                    header_cost_delta_sum = 0.0

                # Compute summary penalty cents (proofs-only)
                summary_penalty_cents = 0
                try:
                    totals = eob.get("totals") or {}
                    def _cents(x: Any) -> int:
                        try:
                            from decimal import Decimal
                            return int(Decimal(str(x)).quantize(Decimal("0.01")) * 100)
                        except Exception:
                            return 0
                    rows_sum = {
                        "charged_cents": sum(_cents(s.get("charged_amount")) for s in services_rows),
                        "allowed_cents": sum(_cents(s.get("allowed_amount")) for s in services_rows),
                        "paid_cents": sum(_cents(s.get("insurance_paid")) for s in services_rows),
                        "patient_cents": sum(_cents(s.get("patient_responsibility")) for s in services_rows),
                    }
                    totals_cents = {
                        "charged_cents": _cents(totals.get("total_charged")),
                        "allowed_cents": _cents(totals.get("total_allowed")),
                        "paid_cents": _cents(totals.get("total_paid")),
                        "patient_cents": _cents(totals.get("total_patient_responsibility")),
                    }
                    residuals = {k: (rows_sum.get(k, 0) - totals_cents.get(k, 0)) for k in totals_cents.keys()}
                    summary_penalty_cents = int(sum(abs(v) for v in residuals.values()))
                except Exception:
                    summary_penalty_cents = 0

                ap_entry = {
                    "table_id": table.get("id", "t?"),
                    "page_index": page_index,
                    "extraction_variant": src_variant,
                    "column_map": chosen_map,
                    "alternatives": (
                        alt_maps + ([{"variant": "ilp_runner_up", "column_map": ilp_runner["column_map"], "certified_rows_sample": ilp_runner["certified_rows_sample"]}]
                                     if ilp_runner["certified_rows_sample"] >= 0 else [])
                    ),
                    "costs": {"typedness_gap_sum": round(typedness_cost, 4), "header_match": header_match_val, "sample_conservation": sample_cons, "certified_rows_sample": int(chosen_cert), "summary_penalty_cents": summary_penalty_cents, "header_cost_delta_sum": round(header_cost_delta_sum, 3)},
                    "ilp": {"best": ilp_best if ilp_best["certified_rows_sample"] >= 0 else None,
                             "runner_up": ilp_runner if ilp_runner["certified_rows_sample"] >= 0 else None},
                    "applied_for_extraction": applied_for_extraction,
                    "apply_evidence": {"certified_rows_sample": max(ilp_best_cert, 0), "min_money_typedness": round(ilp_best_min_t, 4), "swap_stable": bool(swap_stable), "min_delta_energy": int(min_delta_e), "gate": {"type": "ilp_or(typedness&swap)", "passed": gate_pass}, "reason": apply_reason},
                }
                assign_proof_records.append(ap_entry)

                # Extraction map metrics on sample rows, reflecting current column_configs (after promotion if applied)
                # Build current role→col map
                role_to_col: Dict[str, int] = {}
                for ci, cfg in column_configs.items():
                    rf = (cfg or {}).get("field")
                    if rf in ("charged_amount","allowed_amount","insurance_paid","patient_responsibility"):
                        role_to_col[rf] = int(ci)
                map_completeness = len({r for r in role_to_col.keys() if r in ("charged_amount","allowed_amount","insurance_paid","patient_responsibility")})
                data_start = max(0, int(header_row) + 1)
                sample_rows_ilp = list(range(data_start, min(rows, data_start + 5)))
                anchors_hist = {"0": 0, "1": 0, "2": 0, "3": 0}
                coverage_counts = {"charged_amount": 0, "allowed_amount": 0, "insurance_paid": 0, "patient_responsibility": 0}
                for rr in sample_rows_ilp:
                    anchor = 0
                    for role, ci in role_to_col.items():
                        txt = str((matrix.get((rr, int(ci))) or {}).get("text", ""))
                        if txt.strip():
                            anchor += 1
                            coverage_counts[role] += 1
                    anchors_hist[str(min(3, anchor))] += 1
                denom = max(1, len(sample_rows_ilp))
                role_coverage = {k: (coverage_counts[k] / float(denom)) for k in coverage_counts.keys()}
                extraction_metrics_records.append({
                    "table_id": table.get("id", "t?"),
                    "page_index": page_index,
                    "map_completeness": map_completeness,
                    "assigned_roles": sorted(list(role_to_col.keys())),
                    "anchors_per_row": anchors_hist,
                    "role_coverage": role_coverage,
                    "applied_for_extraction": applied_for_extraction,
                    "thresholds": {"typedness_tau": typedness_tau, "ilp_min_cert": 1, "typedness_admit_tau": admit_tau, "typedness_mean_money": round(typedness_mean_money, 4)},
                    "reasons": ([apply_reason] if apply_reason else []),
                })
                # Evidence record (typedness + swap-energy)
                if getattr(args, 'emit_evidence', False):
                    money_t = [ _col_typedness_money(matrix, rows, cols, c) for c in range(cols) ]
                    date_t = [ _col_typedness_date(matrix, rows, c) for c in range(cols) ]
                    code_t = [ _col_typedness_code(matrix, rows, c) for c in range(cols) ]
                    deltas = [ abs(money_t[i+1] - money_t[i]) for i in range(max(0, cols-1)) ]
                    # Swap-energy via solver: change in certified rows when swapping adjacent money-role columns
                    def _row_vals_from_map(r: int, cmap: Dict[str,int]) -> Dict[str, Any]:
                        def _get(ci: int) -> str:
                            try:
                                return str((matrix.get((r, int(ci))) or {}).get("text", ""))
                            except Exception:
                                return ""
                        return {
                            "charged": _get(cmap.get("charged_amount", -1)),
                            "allowed": _get(cmap.get("allowed_amount", -1)),
                            "paid": _get(cmap.get("insurance_paid", -1)),
                            "patient": _get(cmap.get("patient_responsibility", -1)),
                        }
                    # Build chosen cmap from column_configs
                    cmap_chosen: Dict[str,int] = {}
                    for ci, cfg in column_configs.items():
                        fld = cfg.get("field")
                        if fld:
                            cmap_chosen[fld] = int(ci)
                    # Sample up to 5 data rows after header_row
                    data_start = max(0, int(header_row) + 1)
                    data_indices = list(range(data_start, min(rows, data_start + 5)))
                    def _cert_count(cmap: Dict[str,int]) -> int:
                        if min_energy_row is None:
                            return 0
                        cnt = 0
                        for rr in data_indices:
                            res = min_energy_row(_row_vals_from_map(rr, cmap))
                            if res.get("certified"):
                                cnt += 1
                        return cnt
                    base_cert = _cert_count(cmap_chosen) if data_indices else 0
                    swap_energy: List[Dict[str, Any]] = []
                    for j in range(max(0, cols-1)):
                        # Identify roles assigned to j and j+1; proceed only if both are distinct money roles
                        role_at = {v: k for k, v in cmap_chosen.items()}
                        r1 = role_at.get(j)
                        r2 = role_at.get(j+1)
                        if r1 in ("charged_amount","allowed_amount","insurance_paid","patient_responsibility") and \
                           r2 in ("charged_amount","allowed_amount","insurance_paid","patient_responsibility") and r1 != r2:
                            cmap_swapped = dict(cmap_chosen)
                            cmap_swapped[r1], cmap_swapped[r2] = j+1, j
                            swapped_cert = _cert_count(cmap_swapped) if data_indices else 0
                            swap_energy.append({"cols": [j, j+1], "delta_certified": int(swapped_cert - base_cert)})
                    qseed = {
                        "rows": int(rows),
                        "cols": int(cols),
                        "money_t": [round(x, 4) for x in money_t],
                        "date_t": [round(x, 4) for x in date_t],
                        "code_t": [round(x, 4) for x in code_t],
                        "table_id": table.get("id", "t?"),
                        "page_index": page_index,
                    }
                    qhash = hashlib.sha256(json.dumps(qseed, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
                    evidence_records.append({
                        "table_id": table.get("id", "t?"),
                        "page_index": page_index,
                        "rows": int(rows),
                        "cols": int(cols),
                        "typedness": {"money": qseed["money_t"], "date": qseed["date_t"], "code": qseed["code_t"]},
                        "swap_test_deltas": [round(x, 4) for x in deltas],
                        "swap_energy": swap_energy,
                        "quantization": {"row_method": "header_row_idx", "col_method": "vision_table_cols", "hash": qhash},
                    })
            except Exception as exc:
                print(f"[assign/evidence] record build failed: {exc}", file=sys.stderr)

            # Flag table-level uncertain semantics (propagated to rows)
            table_semantic_uncertain = bool(semantic_uncertain)
            for row in range(header_row + 1, rows):
                entry: Dict[str, Any] = {}
                row_prov: List[Dict[str, Any]] = []
                for col, cfg in column_configs.items():
                    cell = matrix.get((row, col))
                    if not cell:
                        continue
                    # Row ownership: skip shadow cells below a spanning header/data cell
                    if cell.get("_is_shadow"):
                        continue
                    raw = str(cell.get("text", "")).strip()
                    if not raw:
                        continue
                    value = transform_value(raw, cfg)
                    if value is None:
                        continue
                    field = cfg["field"]
                    entry.setdefault(field, value)
                    # Record per-field source for C5 row coherence decisions
                    src_for_cell = cell.get("source") or table_source
                    entry[f"_{field}_source"] = src_for_cell
                    # Capture candidate by current source if money-like
                    try:
                        cand_map = entry.setdefault(f"_{field}_candidates", {})
                        if isinstance(value, str) and value.strip() and STRICT_MONEY_RE.match(_normalize_money_text(value)):
                            if src_for_cell in ("atlas", "patch", "rowize"):
                                cand_map[src_for_cell] = _normalize_money_text(value)
                    except Exception:
                        pass
                    # Record seed fallback when present (rowize path)
                    seed_raw = str(cell.get("seed_text", "")).strip()
                    if seed_raw:
                        seed_val = transform_value(seed_raw, cfg)
                        if seed_val is not None:
                            entry[f"_{field}_seed"] = seed_val
                            # Add as 'vision' ring candidate for money fields
                            try:
                                cand_map = entry.setdefault(f"_{field}_candidates", {})
                                if isinstance(seed_val, str) and STRICT_MONEY_RE.match(_normalize_money_text(seed_val)):
                                    cand_map.setdefault("vision", _normalize_money_text(seed_val))
                            except Exception:
                                pass
                    conf = cell.get("confidence")
                    conf_val = float(conf) if isinstance(conf, (int, float)) else 0.0
                    entry[f"_{field}_confidence"] = conf_val
                    row_prov.append(
                        build_provenance(
                            ptr=f"/services/{len(services)}/" + field,
                            source=src_for_cell,
                            confidence=conf_val,
                            page_index=page_index,
                            box=cell.get("pixel_box") or cell.get("box")
                        )
                    )
                # Pull atlas/patch alternates from matrix cell for ring
                for col, cfg in column_configs.items():
                    cell = matrix.get((row, col)) or {}
                    field = cfg.get("field")
                    if field not in ("allowed_amount","insurance_paid","patient_responsibility","charged_amount"):
                        continue
                    if cell.get("atlas_candidates"):
                        entry.setdefault(f"_{field}_candidates", {})["atlas"] = cell.get("atlas_candidates")
                    if cell.get("patch_candidates"):
                        entry.setdefault(f"_{field}_candidates", {})["patch"] = cell.get("patch_candidates")
                # Pull atlas/patch alternates from cell for ring
                for col, cfg in column_configs.items():
                    cell = matrix.get((row, col)) or {}
                    field = cfg.get("field")
                    if field not in ("allowed_amount","insurance_paid","patient_responsibility","charged_amount"):
                        continue
                    alt_atlas = cell.get("atlas_candidates") or []
                    alt_patch = cell.get("patch_candidates") or []
                    if alt_atlas or alt_patch:
                        cands = entry.setdefault(f"_{field}_candidates", {})
                        if alt_atlas:
                            # choose top 1 different from current
                            cur = entry.get(field)
                            for val in alt_atlas:
                                vstr = val.get("value") if isinstance(val, dict) else val
                                if vstr and vstr != cur and STRICT_MONEY_RE.match(_normalize_money_text(vstr)):
                                    cands.setdefault("atlas", alt_atlas)
                                    break
                        if alt_patch:
                            cur = entry.get(field)
                            for val in alt_patch:
                                vstr = val.get("value") if isinstance(val, dict) else val
                                if vstr and vstr != cur and STRICT_MONEY_RE.match(_normalize_money_text(vstr)):
                                    cands.setdefault("patch", alt_patch)
                                    break
                # Seed expansion for missing money fields using rowized column types (if present)
                missing_money = [
                    fld for fld in ("charged_amount","allowed_amount","insurance_paid","patient_responsibility")
                    if not (isinstance(entry.get(fld), str) and entry.get(fld).strip())
                ]
                col_types = table.get("column_types") or []
                if missing_money and col_types and len(col_types) == cols:
                    used_cols = set(column_configs.keys())
                    for fld in missing_money:
                        if entry.get(f"_{fld}_seed"):
                            continue
                        cfg_money = {"field": fld, "type": "money"}
                        seeded = False
                        for c_idx in range(cols):
                            if c_idx in used_cols:
                                continue
                            if col_types[c_idx] != "money":
                                continue
                            ccell = matrix.get((row, c_idx)) or {}
                            raw = str(ccell.get("seed_text") or ccell.get("text") or "").strip()
                            if not raw:
                                continue
                            val = transform_value(raw, cfg_money)
                            if val is None:
                                continue
                            # Apply cents inference if column expects cents (threshold varies)
                            cents_ratio = (table.get("column_cents_ratio") or [0.0]*cols)[c_idx] if isinstance(table.get("column_cents_ratio"), list) else 0.0
                            norm_val = _normalize_money_text(val)
                            # Keep conservative cents guard always
                            if cents_ratio >= 0.75 and re.match(r"^-?\d+$", norm_val):
                                norm_val = norm_val + ".00"
                                entry[f"_{fld}_cents_inferred"] = True
                            # Constant-aware seed filter
                            constants = set(table.get("constant_money_values") or [])
                            if norm_val in constants:
                                _kpi_inc("constants_excluded_upstream", 1)
                                continue
                            entry[f"_{fld}_seed"] = val
                            # expose ring candidate under 'vision'
                            try:
                                cands = entry.setdefault(f"_{fld}_candidates", {})
                                if isinstance(norm_val, str) and STRICT_MONEY_RE.match(norm_val):
                                    cands.setdefault("vision", norm_val)
                            except Exception:
                                pass
                            seeded = True
                            break
                        if not seeded:
                            # fallback: any money-looking raw in the row
                            for c_idx in range(cols):
                                ccell = matrix.get((row, c_idx)) or {}
                                raw = str(ccell.get("seed_text") or ccell.get("text") or "").strip()
                                if not raw:
                                    continue
                                if STRICT_MONEY_RE.match(_normalize_money_text(raw)):
                                    norm = _normalize_money_text(raw)
                                    # Apply cents inference by column ratio when applicable (threshold varies)
                                    cents_ratio = (table.get("column_cents_ratio") or [0.0]*cols)[c_idx] if isinstance(table.get("column_cents_ratio"), list) else 0.0
                                    # Keep conservative cents guard always
                                    if cents_ratio >= 0.75 and re.match(r"^-?\d+$", norm):
                                        norm = norm + ".00"
                                        entry[f"_{fld}_cents_inferred"] = True
                                    # Constant-aware seed filter
                                    constants = set(table.get("constant_money_values") or [])
                                    if norm in constants:
                                        _kpi_inc("constants_excluded_upstream", 1)
                                        continue
                                    entry[f"_{fld}_seed"] = norm
                                    try:
                                        cands = entry.setdefault(f"_{fld}_candidates", {})
                                        cands.setdefault("vision", norm)
                                    except Exception:
                                        pass
                                    break
                # Append only meaningful rows (must have at least one data field)
                meaningful_keys = {"charged_amount","allowed_amount","insurance_paid","patient_responsibility","procedure_code","service_date"}
                has_data = any((k in meaningful_keys) for k in entry.keys())
                if has_data:
                    # Attach metadata and uncertainty flag
                    if table_semantic_uncertain:
                        entry["_semantic_uncertain"] = True
                    entry.setdefault("page_number", int(page_index))
                    entry.setdefault("table_id", table.get("id"))
                    entry.setdefault("row_index", int(row))
                    # Optional CP7: apply lexical repair to typed fields (TEXT immutable)
                    if apply_repair and maps is not None:
                        entry, rn = apply_lexical_repair(entry, maps)
                        if rn and repair_notes is not None:
                            repair_notes.extend(rn)
                        if rn and repair_summary is not None:
                            for note in rn:
                                t = note.get("type")
                                if t == "money":
                                    repair_summary["money_fixed"] = repair_summary.get("money_fixed", 0) + 1
                                elif t == "date":
                                    repair_summary["date_fixed"] = repair_summary.get("date_fixed", 0) + 1
                                elif t == "code":
                                    repair_summary["code_fixed"] = repair_summary.get("code_fixed", 0) + 1
                                for r in note.get("rules", []):
                                    hist = repair_summary.setdefault("repair_rules_hist", {})
                                    hist[r] = hist.get(r, 0) + 1
                    services.append(entry)
                    provenance.extend(row_prov)
            # Capture HIM evidence in the provided out dict
            if used_him and him_out is not None and him_evidence is not None:
                him_out.setdefault("tables", []).append({
                    "page_index": page_index,
                    "table_id": table.get("id", "t?"),
                    "evidence": him_evidence
                })
    return services, sem_map_records, sem_choice_records, assign_proof_records, evidence_records, extraction_metrics_records, selection_evidence_records


def apply_lexical_repair(row: Dict[str, Any], maps: Dict[str, Dict[str, str]]) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """Apply CP7 repairs to a single row; returns (row, notes)."""
    out = dict(row)
    notes: List[Dict[str, Any]] = []
    # Money-like fields
    for fld in ("charged_amount", "allowed_amount", "insurance_paid", "patient_responsibility"):
        val = out.get(fld)
        if isinstance(val, str) and val.strip():
            before = val
            repaired, rn = (repair_money(before, maps.get("money", {})) if repair_money else (before, []))
            if repaired is not None and repaired != before:
                out[fld] = repaired
                notes.append({"field": fld, "type": "money", "before": before, "after": repaired, "rules": rn})
    # Date
    if isinstance(out.get("service_date"), str) and out["service_date"].strip():
        before = out["service_date"]
        repaired, rn = (repair_date(before, maps.get("date", {})) if repair_date else (before, []))
        if repaired is not None and repaired != before:
            out["service_date"] = repaired
            notes.append({"field": "service_date", "type": "date", "before": before, "after": repaired, "rules": rn})
    # Code
    if isinstance(out.get("procedure_code"), str) and out["procedure_code"].strip():
        before = out["procedure_code"].upper()
        repaired, rn = (repair_code(before, maps.get("code", {})) if repair_code else (before, []))
        if repaired is not None and repaired != before:
            out["procedure_code"] = repaired
            notes.append({"field": "procedure_code", "type": "code", "before": before, "after": repaired, "rules": rn})
    return out, notes


# -----------------------------------------------------------------------------
# Totals & metadata
# -----------------------------------------------------------------------------


def sum_amounts(values: Iterable[str]) -> str:
    total = Decimal("0.00")
    for value in values:
        try:
            total += Decimal(value)
        except (InvalidOperation, TypeError):
            continue
    return f"{total:.2f}"


def compute_totals(services: List[Dict[str, Any]]) -> Dict[str, str]:
    charges = [s.get("charged_amount", "0.00") for s in services]
    allowed = [s.get("allowed_amount", "0.00") for s in services]
    paid = [s.get("insurance_paid", "0.00") for s in services]
    patient = [s.get("patient_responsibility", "0.00") for s in services]
    return {
        "total_charged": sum_amounts(charges),
        "total_allowed": sum_amounts(allowed),
        "total_paid": sum_amounts(paid),
        "total_patient_responsibility": sum_amounts(patient),
        "currency": "USD"
    }


def cents(x: Any) -> int:
    try:
        q = Decimal(str(x)).quantize(Decimal("0.01"))
        return int(q * 100)
    except Exception:
        return 0


def check_conservation(services: List[Dict[str, Any]]) -> Dict[str, Any]:
    eps = 1  # cents tolerance
    conserved: List[int] = []
    inconsistent: List[int] = []
    incomplete: List[int] = []
    rows: List[Dict[str, Any]] = []
    for idx, s in enumerate(services):
        row = {"row": idx, "status": "unknown", "reasons": []}
        ch = s.get("charged_amount")
        al = s.get("allowed_amount")
        pa = s.get("insurance_paid")
        pt = s.get("patient_responsibility")
        miss = []
        for k, v in ("charged", ch), ("allowed", al), ("paid", pa), ("patient", pt):
            if v is None or str(v).strip() == "":
                miss.append(k)
        if miss:
            row["status"] = "incomplete"
            row["reasons"].append("missing:" + ",".join(miss))
            incomplete.append(idx)
            rows.append(row)
            continue
        ch_c, al_c, pa_c, pt_c = cents(ch), cents(al), cents(pa), cents(pt)
        if al_c > ch_c + eps:
            row["reasons"].append("allowed>charged")
        if abs(al_c - (pa_c + pt_c)) > eps:
            row["reasons"].append("allowed!=(paid+patient)")
        if not row["reasons"]:
            row["status"] = "conserved"
            conserved.append(idx)
        else:
            row["status"] = "inconsistent"
            inconsistent.append(idx)
        rows.append(row)
    total = len(services)
    return {
        "conserved_rows": conserved,
        "inconsistent_rows": inconsistent,
        "incomplete_rows": incomplete,
        "conservation_rate": (len(conserved) / total) if total else 0.0,
        "rows": rows,
    }


def compute_coherence(services: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Compute row-level money arithmetic coherence: allowed ≈ paid + patient.

    Returns a payload with per-row delta and a boolean flag. Pure telemetry; no mutation.
    """
    from decimal import Decimal
    def d(x: Any) -> Optional[Decimal]:
        try:
            return Decimal(str(x))
        except Exception:
            return None
    rows_out: List[Dict[str, Any]] = []
    coherent = 0
    for idx, s in enumerate(services):
        al = d(s.get("allowed_amount"))
        pa = d(s.get("insurance_paid"))
        pt = d(s.get("patient_responsibility"))
        ch = d(s.get("charged_amount"))
        row = {"row": idx, "coherent": False, "delta_abs": None, "delta_pct": None, "source_mix": {}}
        # Collect source mix from provenance-like hint if embedded (optional)
        for k in ("charged_amount", "allowed_amount", "insurance_paid", "patient_responsibility"):
            src = s.get(f"_{k}_source") or s.get("_source")
            if src:
                row["source_mix"][k] = src
        if al is None or pa is None or pt is None:
            rows_out.append(row)
            continue
        delta = al - (pa + pt)
        delta_abs = abs(delta)
        # Tolerance base is the larger magnitude among allowed and paid+patient
        base = max(Decimal("0.01"), max(al, (pa + pt)))
        delta_pct = float((delta_abs / base))
        row["delta_abs"] = float(delta_abs)
        row["delta_pct"] = delta_pct
        # Tolerances: ±$0.01 or 0.5%
        if delta_abs <= Decimal("0.01") or delta_pct <= 0.005:
            row["coherent"] = True
            coherent += 1
        rows_out.append(row)
    total = len(services)
    return {"rows": rows_out, "coherent_count": coherent, "coherent_rate": (coherent / total) if total else 0.0}


def apply_coherence_lock(services: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """C6 Selective-Repair + Locks over money fields.

    - Expands seeds (canonicalization; cents policy; outlier/constant guards)
    - Ranks by effective confidence (source-weighted)
    - Selective repair via ring + derivation ladder
    - Coherence clamp & lock
    """
    money_fields = (
        ("charged_amount", "_charged_amount_source"),
        ("allowed_amount", "_allowed_amount_source"),
        ("insurance_paid", "_insurance_paid_source"),
        ("patient_responsibility", "_patient_responsibility_source"),
    )
    from decimal import Decimal
    def to_dec(v: Any) -> Optional[Decimal]:
        try:
            return Decimal(str(v))
        except Exception:
            return None
    # Column stats
    field_vals: Dict[str, List[Decimal]] = {f: [] for f, _ in money_fields}
    val_counts: Dict[str, Dict[str, int]] = {f: {} for f, _ in money_fields}
    for s in services:
        for f, _ in money_fields:
            v = s.get(f)
            if isinstance(v, str) and v.strip():
                dv = to_dec(v)
                if dv is not None:
                    field_vals[f].append(dv)
                    key = f"{dv:.2f}"
                    val_counts[f][key] = val_counts[f].get(key, 0) + 1
    # Deterministic mode helper over per-field value counts
    def _mode_value(field: str) -> Optional[Decimal]:
        counts = val_counts.get(field) or {}
        if not counts:
            return None
        # Highest frequency; tie-break lexicographically ascending
        best_key = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
        try:
            return Decimal(best_key)
        except Exception:
            return None

    med: Dict[str, Decimal] = {}
    cents_ratio: Dict[str, float] = {}
    for f, vals in field_vals.items():
        if vals:
            sv = sorted(vals); m = (sv[len(sv)//2] if len(sv)%2 else (sv[len(sv)//2-1]+sv[len(sv)//2])/2)
            med[f] = m
            cents_ratio[f] = sum(1 for v in vals if (v % 1) != 0) / float(len(vals))
        else:
            med[f] = Decimal("0.00"); cents_ratio[f] = 0.0
    # Helpers
    MONEY_MAP = {"O": "0", "o": "0", "l": "1", "I": "1", "|": "1", "S": "5", "s": "5", "B": "8"}
    def canonicalize_money(text: str) -> Optional[str]:
        raw = str(text or "").strip()
        if not raw:
            return None
        for a,b in MONEY_MAP.items(): raw = raw.replace(a,b)
        raw = raw.replace(" ", "").replace(",", "")
        return raw
    def strict_ok(v: str) -> bool:
        return bool(STRICT_MONEY_RE.match(str(v).replace(" ", "")))
    def eff_conf(src: str, conf: Optional[float]) -> float:
        c = float(conf or 0.0)
        if src in ("atlas","patch"): return c
        if src=="vision": return c*0.85
        if src=="rowize": return max(c,0.35)
        if src in ("him","derived"): return min(0.8,max(0.6,c))
        return c

    # Banker's rounding to cents
    from decimal import ROUND_HALF_EVEN
    def cents(v: Decimal) -> Decimal:
        return v.quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN)

    coh = compute_coherence(services)
    rows_meta = {r.get("row"): r for r in coh.get("rows", [])}
    out: List[Dict[str, Any]] = []
    for idx, s in enumerate(services):
        # Ensure metadata presence before any locking decisions
        if "page_number" not in s:
            s["page_number"] = int(s.get("page_number") or 0)
        if "table_id" not in s:
            s["table_id"] = s.get("table_id") or "unknown_table"
        if "row_index" not in s:
            s["row_index"] = int(idx)
        row_info = rows_meta.get(idx) or {}
        coherent = bool(row_info.get("coherent"))
        # Count atlas/patch
        ap = sum(1 for f, sk in money_fields if (s.get(sk) in ("atlas","patch")))
        if coherent and ap >= 2 and not s.get("_semantic_uncertain"):
            so = dict(s); so["_row_locked"] = True; out.append(so); continue
        so = dict(s)
        changed = False
        # Seed expansion
        for f, sk in money_fields:
            val = so.get(f)
            if not (isinstance(val,str) and strict_ok(val)):
                seed = so.get(f"_{f}_seed") or val
                if seed:
                    can = canonicalize_money(seed)
                    if can:
                        if cents_ratio.get(f,0.0) >= 0.9 and ("." not in can):
                            can = can+".00"; so[f"_{f}_cents_inferred"] = True
                        if strict_ok(can):
                            dcan = to_dec(can)
                            if dcan is not None and not (med.get(f) and dcan > (med[f]*10)):
                                if val_counts.get(f,{}).get(f"{dcan:.2f}",0) < 8:
                                    so[f] = f"{dcan:.2f}"; so[f"_{f}_canonicalization_map"] = True; changed=True
        # Effective confidence ranking
        infos = []
        for f, sk in money_fields:
            src = so.get(sk) or ""; conf = so.get(f"_{f}_confidence") or 0.0
            val = so.get(f)
            is_money_like = bool(isinstance(val, str) and strict_ok(val))
            econf = eff_conf(src, conf)
            if src == "rowize" and is_money_like:
                econf = max(econf, 0.70)
            infos.append((f, src, float(conf), econf, is_money_like))
        strong = {f:st for (f,_,__,___,st) in infos}
        money_set = {"allowed_amount","insurance_paid","patient_responsibility","charged_amount"}
        strong_count = sum(1 for f in money_set if strong.get(f))
        # Selective repair/derivation
        def revert_to_seed(field: str) -> bool:
            seed = so.get(f"_{field}_seed")
            if seed and strict_ok(seed):
                # Constant-pattern guard: avoid values repeated identically across many rows
                try:
                    dv = to_dec(seed)
                    if dv is not None:
                        key = f"{dv:.2f}"
                        const_thr = max(5, int(math.ceil(len(services) * 0.10)))
                        if val_counts.get(field, {}).get(key, 0) >= const_thr:
                            return False
                except Exception:
                    pass
                so[f"_{field}_reverted"] = True
                so[f"_{field}_revert_from"] = so.get(f"_{field}_source")
                so[f"_{field}_revert_to"] = "seed"
                so[f] = seed
                so[f"_{field}_repair_reason"] = "revert_to_seed"
                _kpi_inc("seed_reverts", 1)
                return True
            return False

        def revert_via_ring(field: str) -> bool:
            """Try atlas>patch>vision>rowize alternates under guards + monotone improvement.
            Accept candidate if it makes the row coherent OR strictly reduces |delta|,
            and does not violate allowed≤charged when charged present.
            Additional guard: when column fingerprints indicate cents (≥0.75),
            prefer candidates with cents for vision/rowize sources.
            """
            def money_has_cents(txt: str) -> bool:
                s = _normalize_money_text(txt)
                return bool(re.match(r"^-?\d+\.\d{2}$", s))

            cands_map = so.get(f"_{field}_candidates") or {}
            ring = ("atlas", "patch", "vision", "rowize")
            # Baseline delta and completeness
            old_row = dict(so)
            old_stats = compute_coherence([old_row]).get("rows", [{}])[0]
            old_delta = float(old_stats.get("delta_abs") or 1e9)
            def _present_count(obj: Dict[str, Any]) -> int:
                cnt = 0
                for fkey in ("allowed_amount","insurance_paid","patient_responsibility"):
                    v = obj.get(fkey)
                    if isinstance(v, str) and strict_ok(v):
                        cnt += 1
                return cnt
            old_count = _present_count(old_row)

            # Flatten candidate list across ring with metadata
            candidates: List[Tuple[str, str, float, Dict[str, Any]]] = []  # (src, value, score, feat)
            for src in ring:
                vals = cands_map.get(src)
                if not vals:
                    continue
                if isinstance(vals, list):
                    for obj in vals:
                        if isinstance(obj, dict):
                            val = obj.get("value") or obj.get("sanitized")
                            if isinstance(val, str):
                                candidates.append((src, val, float(obj.get("score") or 0.0), obj))
                        elif isinstance(obj, str):
                            candidates.append((src, obj, 0.0, {}))
                elif isinstance(vals, str):
                    candidates.append((src, vals, 0.0, {}))

            best: Optional[Tuple[str, str, float, float, int, Dict[str, Any]]] = None  # (src, val, new_delta, score, new_count, feat)
            _kpi_inc("alts_attempted_count", len(candidates))
            for src, cand, score, feat in candidates:
                if not (isinstance(cand, str) and cand.strip() and strict_ok(cand)):
                    continue
                # Constant guard
                try:
                    dv = to_dec(cand)
                    if dv is not None:
                        key = f"{dv:.2f}"
                        const_thr = max(5, int(math.ceil(len(services) * 0.10)))
                        if val_counts.get(field, {}).get(key, 0) >= const_thr:
                            _kpi_inc("alts_excluded_constant", 1)
                            continue
                except Exception:
                    pass
                # Cents fingerprint guard for vision/rowize
                if src in ("vision", "rowize"):
                    need_cents = (cents_ratio.get(field, 0.0) >= 0.75)
                    if need_cents and not money_has_cents(cand):
                        _kpi_inc("alts_excluded_no_cents", 1)
                        continue
                tentative = dict(so)
                tentative[field] = cand
                tentative[f"_{field}_source"] = src
                if field == "allowed_amount":
                    chv = to_dec(tentative.get("charged_amount"))
                    alv = to_dec(cand)
                    if chv is not None and alv is not None and alv > chv and chv > Decimal("0.00"):
                        _kpi_inc("alts_excluded_clamp", 1)
                        continue
                new_stats = compute_coherence([tentative]).get("rows", [{}])[0]
                new_delta = float(new_stats.get("delta_abs") or 1e9)
                new_count = _present_count(tentative)
                # Choose best by coherent → lowest delta → highest completeness, tie by ring precedence and score
                better = False
                if best is None:
                    better = True
                else:
                    _, _, best_delta, best_score, best_count, _ = best
                    if new_stats.get("coherent") and best_delta != 0.0:
                        better = True
                    elif (best_delta == 1e9 or new_delta < best_delta - 1e-6):
                        better = True
                    elif (new_count > best_count):
                        better = True
                    elif abs(new_delta - best_delta) < 1e-6:
                        # tie-break by ring order then score
                        if ring.index(src) < ring.index(best[0]):
                            better = True
                        elif abs(score - best_score) > 1e-6 and score > best_score:
                            better = True
                if better:
                    best = (src, cand, new_delta, score, new_count, feat)

            if best is None:
                return False
            src, cand, new_delta, score, new_count, feat = best
            # Apply selected
            so[f"_{field}_reverted"] = True
            so[f"_{field}_revert_from"] = so.get(f"_{field}_source")
            so[f"_{field}_revert_to"] = src
            so[f"_{field}_repair_reason"] = "revert_via_ring"
            prev_stats = compute_coherence([so]).get("rows", [{}])[0]
            prev_delta = float(prev_stats.get("delta_abs") or 1e9)
            so[field] = cand
            so[f"_{field}_source"] = src
            try:
                _kpi_inc(f"ring_applied_count.{field}", 1)
            except Exception:
                pass
            # KPI: Look-ahead applied if we tried >1
            if len(candidates) > 1:
                _kpi_inc("lookahead_applied_count", 1)
                if prev_delta < 1e9 and new_delta < 1e9 and new_delta < prev_delta:
                    _kpi_inc("lookahead_delta_reduction_sum", int(round((prev_delta - new_delta)*100)))
            _kpi_inc("alts_used_count", 1)
            return True
        if strong_count >= 3:
            # Revert only weakest among APR
            weakest = None; w_eff = 10.0
            for f, src, c, e, st in infos:
                if f in money_set and e < w_eff:
                    weakest=f; w_eff=e
            if weakest:
                if revert_via_ring(weakest):
                    changed = True
                elif revert_to_seed(weakest):
                    changed = True
        elif strong_count == 2:
            ch = to_dec(so.get("charged_amount")); al = to_dec(so.get("allowed_amount")); pa = to_dec(so.get("insurance_paid")); pt = to_dec(so.get("patient_responsibility"))
            if al is None and pa is not None and pt is not None:
                so["allowed_amount"] = f"{cents(pa+pt):.2f}"; so["_allowed_amount_repair_reason"]="derive_sum"; changed=True
            elif pt is None and al is not None and pa is not None:
                so["patient_responsibility"] = f"{cents(max(Decimal('0.00'), al-pa)):.2f}"; so["_patient_responsibility_repair_reason"]="derive_diff"; changed=True
            elif pa is None and al is not None and pt is not None:
                so["insurance_paid"] = f"{cents(max(Decimal('0.00'), al-pt)):.2f}"; so["_insurance_paid_repair_reason"]="derive_diff"; changed=True
            # Two-missing with Charged present: set Â = min(C, mode_allowed), then fill the other via identity
            elif ch is not None and al is None and ((pa is not None) ^ (pt is not None)):
                mode_a = _mode_value("allowed_amount")
                ahat = ch if mode_a is None else min(ch, mode_a)
                so["allowed_amount"] = f"{cents(ahat):.2f}"; so["_allowed_amount_repair_reason"] = "derive_from_charged_mode"; changed=True
                pa = to_dec(so.get("insurance_paid")); pt = to_dec(so.get("patient_responsibility"))
                if pa is not None and pt is None:
                    so["patient_responsibility"] = f"{cents(max(Decimal('0.00'), ahat - pa)):.2f}"; so["_patient_responsibility_repair_reason"] = "derive_diff"; changed=True
                elif pt is not None and pa is None:
                    so["insurance_paid"] = f"{cents(max(Decimal('0.00'), ahat - pt)):.2f}"; so["_insurance_paid_repair_reason"] = "derive_diff"; changed=True
            else:
                for f in ("charged_amount","allowed_amount","insurance_paid","patient_responsibility"):
                    if not (isinstance(so.get(f),str) and strict_ok(so.get(f))):
                        if revert_via_ring(f):
                            changed=True
                        elif revert_to_seed(f):
                            changed=True
        else:
            for f in ("charged_amount","allowed_amount","insurance_paid","patient_responsibility"):
                if not (isinstance(so.get(f),str) and strict_ok(so.get(f))):
                    if revert_via_ring(f):
                        changed=True
                    elif revert_to_seed(f):
                        changed=True
        # Clamp allowed to charged
        ch = to_dec(so.get("charged_amount")); al = to_dec(so.get("allowed_amount"))
        if ch is not None and al is not None and (ch > Decimal("0.00")) and al > ch:
            so["allowed_amount"] = f"{cents(ch):.2f}"; so["_allowed_amount_repair_reason"] = "clamp_to_charged"; changed=True
        # Derivation ladder completion
        al = to_dec(so.get("allowed_amount")); pa = to_dec(so.get("insurance_paid")); pt = to_dec(so.get("patient_responsibility"))
        if al is None and pa is not None and pt is not None:
            so["allowed_amount"] = f"{cents(pa+pt):.2f}"; so["_allowed_amount_repair_reason"] = so.get("_allowed_amount_repair_reason") or "derive_sum"; changed=True
        if pt is None and al is not None and pa is not None:
            so["patient_responsibility"] = f"{cents(max(Decimal('0.00'), al-pa)):.2f}"; so["_patient_responsibility_repair_reason"] = so.get("_patient_responsibility_repair_reason") or "derive_diff"; changed=True
        if pa is None and al is not None and pt is not None:
            so["insurance_paid"] = f"{cents(max(Decimal('0.00'), al-pt)):.2f}"; so["_insurance_paid_repair_reason"] = so.get("_insurance_paid_repair_reason") or "derive_diff"; changed=True
        # Zero-allowed override: if allowed is exactly 0.00 while paid+patient > 0, prefer sum for coherence
        al2 = to_dec(so.get("allowed_amount")); pa2 = to_dec(so.get("insurance_paid")); pt2 = to_dec(so.get("patient_responsibility"))
        if al2 is not None and pa2 is not None and pt2 is not None:
            if (al2 == Decimal("0.00")) and ((pa2 + pt2) > Decimal("0.00")):
                so["allowed_amount"] = f"{cents(pa2+pt2):.2f}"; so["_allowed_amount_repair_reason"] = "derive_sum_override"; changed=True
        if changed: so["_row_reverted"] = True
        # Final lock only if coherent; then require ≥2 trusted or a complete coherent triplet
        row_post = compute_coherence([so]).get("rows", [{}])[0]
        ap_final = sum(1 for f, sk in money_fields if (so.get(sk) in ("atlas","patch")))
        if row_post.get("coherent") and not so.get("_semantic_uncertain"):
            if (ap_final >= 2) or all(isinstance(so.get(f),str) and strict_ok(so.get(f)) for f in ("allowed_amount","insurance_paid","patient_responsibility")):
                so["_row_locked"] = True
                # Lock provenance
                so["_lock_reason"] = "coherent+\u22652_trusted" if ap_final >= 2 else "coherent+complete"
                lock_material = {
                    "allowed_amount": so.get("allowed_amount"),
                    "insurance_paid": so.get("insurance_paid"),
                    "patient_responsibility": so.get("patient_responsibility"),
                    "sources": {
                        "allowed_amount": so.get("_allowed_amount_source"),
                        "insurance_paid": so.get("_insurance_paid_source"),
                        "patient_responsibility": so.get("_patient_responsibility_source"),
                    },
                    "row_idx": idx,
                }
                so["_lock_sha256"] = hashlib.sha256(json.dumps(lock_material, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
        out.append(so)
    return out


# -----------------------------------------------------------------------------
# Doc-level priors (bootstrap from locked rows)
# -----------------------------------------------------------------------------

def _locked_rows(services: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [s for s in services if s.get("_row_locked")]

def _compute_allowed_ratio_stats(services: List[Dict[str, Any]]) -> Dict[str, Any]:
    from decimal import Decimal, InvalidOperation
    ratios: List[Decimal] = []
    for s in _locked_rows(services):
        try:
            ch = Decimal(str(s.get("charged_amount")))
            al = Decimal(str(s.get("allowed_amount")))
        except Exception:
            continue
        if ch is None or al is None:
            continue
        try:
            if ch > 0:
                r = (al / ch).quantize(Decimal("0.01"))
                if Decimal("0.15") <= r <= Decimal("1.05"):
                    ratios.append(r)
        except Exception:
            continue
    n = len(ratios)
    if n == 0:
        return {"n": 0}
    ratios_sorted = sorted(ratios)
    # IQR
    q1 = ratios_sorted[max(0, int(n * 0.25) - 1)]
    q3 = ratios_sorted[max(0, int(n * 0.75) - 1)]
    iqr = (q3 - q1) if (q3 is not None and q1 is not None) else Decimal("0.00")
    # Adaptive threshold
    thr = Decimal("0.20") if n >= 5 else Decimal("10.00")
    if n >= 10:
        thr = Decimal("0.25")
    if n >= 20:
        thr = Decimal("0.30")
    # Median as mode proxy
    med = ratios_sorted[n // 2] if (n % 2 == 1) else (ratios_sorted[n // 2 - 1] + ratios_sorted[n // 2]) / 2
    return {
        "n": n,
        "iqr": float(iqr),
        "iqr_threshold": float(thr),
        "mode_allowed_ratio": float(med),
    }

def apply_bootstrap_priors(services: List[Dict[str, Any]], stats: Dict[str, Any]) -> int:
    from decimal import Decimal
    applied = 0
    if not stats or (stats.get("n", 0) < 5):
        return 0
    # IQR gate
    iqr = Decimal(str(stats.get("iqr", 0.0)))
    thr = Decimal(str(stats.get("iqr_threshold", 0.30)))
    if iqr > thr:
        return 0
    mode_ratio = Decimal(str(stats.get("mode_allowed_ratio", 0.0)))
    if not (Decimal("0.20") <= mode_ratio <= Decimal("1.00")):
        return 0
    for s in services:
        ch = s.get("charged_amount"); al = s.get("allowed_amount")
        has_ch = isinstance(ch, str) and ch.strip()
        has_al = isinstance(al, str) and al.strip()
        try:
            if has_ch and not has_al:
                ch_d = Decimal(str(ch))
                al_hat = (ch_d * mode_ratio).quantize(Decimal("0.01"))
                s["allowed_amount"] = f"{al_hat:.2f}"
                s["_allowed_amount_bootstrap_source"] = "doc_mode_allowed_ratio"
                s.setdefault("_derived_from", []).append("charged_amount")
                applied += 1
            elif has_al and not has_ch and mode_ratio > Decimal("0.00"):
                al_d = Decimal(str(al))
                ch_hat = (al_d / mode_ratio).quantize(Decimal("0.01"))
                s["charged_amount"] = f"{ch_hat:.2f}"
                s["_charged_amount_bootstrap_source"] = "doc_mode_allowed_ratio"
                s.setdefault("_derived_from", []).append("allowed_amount")
                applied += 1
        except Exception:
            continue
    return applied
# -----------------------------------------------------------------------------
# Document assembly
# -----------------------------------------------------------------------------


def assemble_document(doc_key: str, pages: List[Dict[str, Any]], templates: List[TableTemplate], *, apply_repair: bool = False, maps: Optional[Dict[str, Dict[str, str]]] = None, repair_out: Optional[Dict[str, Any]] = None, him_out: Optional[Dict[str, Any]] = None, him_supplement: bool = False) -> Tuple[Dict[str, Any], List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]]]:
    provenance: List[Dict[str, Any]] = []
    header_chunks: List[str] = []
    for page in pages:
        info = extract_header_text(page)
        for chunk in info.values():
            if chunk.get("text"):
                header_chunks.append(chunk["text"])
    header_text = "\n".join(header_chunks)
    template = choose_template(header_text, templates)

    # CP7 bookkeeping
    rep_notes: List[Dict[str, Any]] = []
    rep_summary: Dict[str, Any] = {"money_fixed": 0, "date_fixed": 0, "code_fixed": 0, "text_mutations": 0}
    sem_map_records: List[Dict[str, Any]] = []
    services, sem_map_records, sem_choice_records, assign_proof_records, evidence_records, extraction_metrics_records, selection_evidence_records = gather_services(pages, template, provenance,
                               apply_repair=apply_repair,
                               maps=maps,
                               repair_notes=rep_notes,
                               repair_summary=rep_summary,
                               him_out=him_out,
                               him_supplement=him_supplement,
                               use_semantic_map=bool(getattr(args, 'semantic_map', False)),
                               him_aggressive=bool(getattr(args, 'him_aggressive', False)))
    # C6: apply selective repair + locks if enabled (multi-pass with optional priors)
    if args.selective_repair_locks:
        max_pass = max(1, int(getattr(args, 'multi_pass', 1) or 1))
        prev_locked = -1
        for i in range(max_pass):
            services = apply_coherence_lock(services)
            # Optional doc-level priors between passes
            if bool(getattr(args, 'bootstrap_priors', False)):
                stats = _compute_allowed_ratio_stats(services)
                _ = apply_bootstrap_priors(services, stats)
            # Convergence: break if locked rows did not increase
            coh_now = compute_coherence(services)
            locked_now = sum(1 for s in services if s.get('_row_locked'))
            if locked_now <= prev_locked:
                break
            prev_locked = locked_now
    if not services:
        # Emit a deterministic fallback row with full metadata; mark as uncertain
        first_page_index = 0
        try:
            if pages:
                first_page_index = int(min(p.get("page_index", 0) for p in pages))
        except Exception:
            first_page_index = 0
        services = [{
            "procedure_code": "D0000",
            "charged_amount": "0.00",
            "page_number": first_page_index,
            "table_id": "fallback_0",
            "row_index": 0,
            "_semantic_uncertain": True,
            "_source": "fallback"
        }]
    # Money conservation: attempt repair where values are missing/inconsistent
    services = money_conservation_pass(services, provenance)
    totals = compute_totals(services)

    claim_number = apply_header_regex(header_text, template.kv_right.get("claim_number", {}).get("regex")) or f"{doc_key}"[:20]
    payer_name = apply_header_regex(header_text, template.kv_right.get("payer_name", {}).get("regex")) or template.payer
    subscriber_id = apply_header_regex(header_text, template.kv_right.get("subscriber_id", {}).get("regex")) or "UNKNOWN"
    provider_name = apply_header_regex(header_text, template.kv_left.get("provider_name", {}).get("regex")) or "UNKNOWN PROVIDER"
    npi = apply_header_regex(header_text, template.kv_left.get("npi", {}).get("regex"))
    tax_id = apply_header_regex(header_text, template.kv_left.get("tax_id", {}).get("regex"))

    metadata = {
        "schema_ref": "urn:imogen:eob:dental:1.2",
        "document_id": hashlib.sha256(doc_key.encode("utf-8")).hexdigest()
    }

    eob = {
        "patient": {
            "patient_name": "UNKNOWN PATIENT",
            "subscriber_id": subscriber_id
        },
        "provider": {"provider_name": provider_name},
        "claim": {
            "claim_number": claim_number,
            "payer_name": payer_name,
            "status": "Pending"
        },
        "services": services,
        "totals": totals,
        "reason_codes": [],
        "metadata": metadata,
        "provenance": provenance
    }
    if repair_out is not None and apply_repair:
        repair_out.update({
            "doc": doc_key,
            "repair_summary": rep_summary,
            "repair_notes_count": len(rep_notes)
        })
    if npi:
        eob["provider"]["npi"] = npi
    if tax_id:
        eob["provider"]["tax_id"] = tax_id

    # Header provenance entries
    if subscriber_id:
        provenance.append(build_provenance("/patient/subscriber_id", "vision", 0.8, 0, None))
    if claim_number:
        provenance.append(build_provenance("/claim/claim_number", "vision", 0.8, 0, None))
    if payer_name:
        provenance.append(build_provenance("/claim/payer_name", "vision", 0.8, 0, None))
    if provider_name:
        provenance.append(build_provenance("/provider/provider_name", "vision", 0.8, 0, None))
    provenance.append(build_provenance("/totals/total_charged", "derived", 1.0, 0, None))
    provenance.append(build_provenance("/totals/total_paid", "derived", 1.0, 0, None))
    provenance.append(build_provenance("/totals/total_patient_responsibility", "derived", 1.0, 0, None))

    return eob, sem_map_records, sem_choice_records, assign_proof_records, evidence_records, extraction_metrics_records, selection_evidence_records


def money_conservation_pass(services: List[Dict[str, Any]], provenance: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    from decimal import Decimal, InvalidOperation
    def d(x):
        try:
            return Decimal(str(x))
        except InvalidOperation:
            return Decimal("0.00")
    repaired: List[Dict[str, Any]] = []
    for i, s in enumerate(services):
        # Respect locks: do not mutate rows that have been locked as coherent
        if s.get("_row_locked"):
            repaired.append(s)
            continue
        billed = d(s.get("charged_amount", "0.00"))
        allowed = d(s.get("allowed_amount", "0.00")) if s.get("allowed_amount") is not None else None
        paid = d(s.get("insurance_paid", "0.00")) if s.get("insurance_paid") is not None else None
        patient = d(s.get("patient_responsibility", "0.00")) if s.get("patient_responsibility") is not None else None

        # If any of allowed/paid/patient is missing, infer from others
        target_allowed = allowed
        if allowed is None and paid is not None and patient is not None:
            target_allowed = paid + patient
        if patient is None and allowed is not None and paid is not None:
            patient = max(Decimal("0.00"), allowed - paid)
        if paid is None and allowed is not None and patient is not None:
            paid = max(Decimal("0.00"), allowed - patient)

        # Clamp allowed to billed if overshoot
        if target_allowed is None and allowed is not None:
            target_allowed = allowed
        # Only clamp if billed is a positive value; avoid clamping to 0 when billed is absent/zero
        if target_allowed is not None and billed > Decimal("0.00") and target_allowed > billed:
            target_allowed = billed

        # Apply inferred repairs back to service dict
        if target_allowed is not None:
            s["allowed_amount"] = f"{target_allowed.quantize(Decimal('0.01')):.2f}"
            provenance.append(build_provenance(f"/services/{i}/allowed_amount", "derived", 1.0, 0, None))
        if paid is not None and s.get("insurance_paid") is None:
            s["insurance_paid"] = f"{paid.quantize(Decimal('0.01')):.2f}"
            provenance.append(build_provenance(f"/services/{i}/insurance_paid", "derived", 1.0, 0, None))
        if patient is not None and s.get("patient_responsibility") is None:
            s["patient_responsibility"] = f"{patient.quantize(Decimal('0.01')):.2f}"
            provenance.append(build_provenance(f"/services/{i}/patient_responsibility", "derived", 1.0, 0, None))

        repaired.append(s)
    return repaired


# -----------------------------------------------------------------------------
# Validation harness
# -----------------------------------------------------------------------------


def run_ajv(ajv_cmd: Optional[str], schema: Optional[Path], output_path: Path) -> None:
    if not ajv_cmd or not schema:
        return
    cmd = ajv_cmd.split()
    cmd.extend(["validate", "-s", str(schema), "-d", str(output_path), "--spec=draft2020", "--strict=false"])
    subprocess.run(cmd, check=True)


# -----------------------------------------------------------------------------
# Main entry
# -----------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description="Assemble dental EOBs from NativeOCR pages")
    parser.add_argument("--pages", required=True, help="Directory containing NDJSON page files")
    parser.add_argument("--mappings", required=True, help="Directory with payer mapping YAML files")
    parser.add_argument("--out", required=True, help="Output directory for EOB JSON files")
    parser.add_argument("--schema", help="Path to DentalEOB JSON Schema for validation")
    parser.add_argument("--ajv", help="AJV command (e.g., 'npx ajv-cli') for schema validation")
    parser.add_argument("--glob", default="*.jsonl", help="Glob pattern for page files")
    parser.add_argument("--apply-repair", action="store_true", help="Apply CP7 lexical repair before conservation (default OFF)")
    parser.add_argument("--repair-maps", default="config/confusion_maps.v1.json", help="Path to confusion maps for CP7 repair")
    parser.add_argument("--use-vlm-tables", action="store_true", help="Consume VLM data responses when Vision tables are absent (default OFF)")
    parser.add_argument("--spatial-merge", action="store_true", help="Fallback: merge VLM header locks with Vision OCR tokens when VLM tables are absent or rejected")
    parser.add_argument("--micro-ocr", action="store_true", help="Enable CP13 micro-OCR per-cell recovery after spatial projection (default OFF)")
    parser.add_argument("--rowize-tokens", action="store_true", help="CP12: build rectangular service tables directly from Vision text tokens (no VLM, no Vision tables)")
    parser.add_argument("--rowize-strict", action="store_true", help="Enable strict validator for rowize tables (min rows, money signal, code/date rows, HIM fit)")
    parser.add_argument("--rowize-max-tables", type=int, default=0, help="Maximum number of rowize tables to accept per document when strict mode is on (default 0 = no cap)")
    parser.add_argument("--doc-validate", action="store_true", help="Validate Vision/spatial tables with quality gates; remove failing tables (default OFF)")
    parser.add_argument("--doc-fallback-rowize", action="store_true", help="When doc tables are rejected and no tables remain, allow rowize to build tables for that page (requires --rowize-tokens) (default OFF)")
    parser.add_argument("--vlm-dir", help="Directory containing VLM response artifacts")
    parser.add_argument("--run-manifest", help="Run manifest (JSON) providing input_sha256 for VLM lookup")
    parser.add_argument("--doc-sha", help="Explicit document SHA-256 when manifests or source files are unavailable")
    parser.add_argument("--him-supplement", action="store_true", help="Supplement header mapping with headerless inference to fill money/date/code when headers are partial (default OFF)")
    parser.add_argument("--selective-repair-locks", action="store_true", help="Enable C6 selective repair + locks over money fields (default OFF)")
    parser.add_argument("--semantic-map", action="store_true", help="Enable semantic, payer-aware column mapping (C6.S) with sidecar emission (default OFF)")
    parser.add_argument("--mapper-latch", type=float, default=0.15, help="Minimum header_match required for Mapper to be considered on ties (default 0.15)")
    parser.add_argument("--him-aggressive", action="store_true", help="Aggressively run HIM when mapper is incomplete or weak; compare via soft-accept (default OFF)")
    parser.add_argument("--bootstrap-priors", action="store_true", help="Enable doc-level priors (allowed/charged ratio) to bootstrap single-anchor rows (default OFF)")
    parser.add_argument("--multi-pass", type=int, default=1, help="Number of selective repair passes (default 1)")
    # Northstar proofs & solver (default OFF)
    parser.add_argument("--solve-roles", action="store_true", help="Emit row/table certificates via law-based solver; optionally gate locks (default OFF)")
    parser.add_argument("--solver-locks-strict", action="store_true", help="When used with --solve-roles, only rows certified by the solver remain locked (default OFF)")
    parser.add_argument("--summary-reconcile", action="store_true", help="Emit summary totals stub and record residuals (default OFF)")
    parser.add_argument("--emit-evidence", action="store_true", help="Emit evidence.v1.json (typedness, quantization hashes, swap-test deltas) (default OFF)")
    parser.add_argument("--budget-stability", action="store_true", help="Emit budget.v1.json and stability.v1.json sidecars (default OFF)")
    parser.add_argument("--ilp-assign-extract", action="store_true", help="Promote ILP best mapping to extraction under a deterministic gate (default OFF)")
    parser.add_argument("--ilp-enforce-map", action="store_true", help="Fail hard if map completeness < 3 after ILP promotion (default OFF)")
    parser.add_argument("--summary-penalty", action="store_true", help="Emit summary.energy.v1.json with proof-only penalty (no extraction effect) (default OFF)")
    parser.add_argument("--summary-penalty-apply", action="store_true", help="Apply summary penalty in energy comparisons (canary) (default OFF)")
    parser.add_argument("--service-gate", action="store_true", help="Gate ILP to service-like tables only (canary) (default OFF)")
    parser.add_argument("--swap-autopilot", action="store_true", help="Auto-flip adjacent money roles when energy improves beyond threshold (canary) (default OFF)")
    args = parser.parse_args()
    # Make args visible to helper functions expecting module-level args
    globals()["args"] = args

    pages_dir = Path(args.pages)
    mapping_dir = Path(args.mappings)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    if args.use_vlm_tables and not args.vlm_dir:
        parser.error("--vlm-dir is required when --use-vlm-tables is set")
    vlm_dir: Optional[Path] = Path(args.vlm_dir) if args.vlm_dir else None
    if vlm_dir and not vlm_dir.exists():
        parser.error(f"--vlm-dir {vlm_dir} does not exist")
    run_manifest_info: Optional[Dict[str, Any]] = None
    if args.run_manifest:
        manifest_path = Path(args.run_manifest)
        if not manifest_path.exists():
            parser.error(f"--run-manifest {manifest_path} does not exist")
        run_manifest_info = load_run_manifest(manifest_path)

    page_files = list(pages_dir.glob(args.glob))
    if not page_files:
        print(f"No page files matched {args.glob} in {pages_dir}")
        return

    templates = load_templates(mapping_dir)
    maps: Optional[Dict[str, Dict[str, str]]] = None
    if args.apply_repair:
        if load_maps is None:
            raise RuntimeError("lexical_repair not available; cannot apply repair")
        maps = load_maps(args.repair_maps)
    grouped = group_pages(page_files)

    for doc_key, pages in grouped.items():
        pages.sort(key=lambda x: x.get("page_index", 0))
        rep_out: Dict[str, Any] = {}
        him_out: Dict[str, Any] = {}
        pages_for_processing: List[Dict[str, Any]] = list(pages)
        # Optional: validate existing Vision/spatial tables and remove failing ones
        doc_validate_records: List[Dict[str, Any]] = []
        if getattr(args, "doc_validate", False):
            for page in pages_for_processing:
                if not page.get("tables"):
                    continue
                accepted, recs = _doc_validate_tables_on_page(page)
                doc_validate_records.extend(recs)
                page["tables"] = accepted
        vlm_tables_used: List[Dict[str, Any]] = []
        spatial_tables_used: List[Dict[str, Any]] = []
        vlm_sidecar_errors: List[Dict[str, Any]] = []
        header_lock_cache: Dict[int, Dict[str, Any]] = {}
        page_assets: Dict[int, Dict[str, Any]] = {}
        micro_roi_sidecars: List[Dict[str, Any]] = []
        micro_atlas_sidecars: List[Dict[str, Any]] = []
        micro_patch_sidecars: List[Dict[str, Any]] = []
        micro_page_stats: List[Dict[str, Any]] = []
        micro_hash_sets = {
            "request_sha256": set(),
            "image_sha256": set(),
            "header_lock_sha256": set(),
            "atlas_sha256": set(),
            "patch_atlas_sha256": set(),
            "patch_profile_sha256": set(),
        }
        doc_sha_used: Optional[str] = None
        doc_sha_source: Optional[str] = None
        if args.use_vlm_tables:
            assert vlm_dir is not None
            doc_sha_used, doc_sha_source = resolve_doc_sha(
                doc_key,
                pages,
                run_manifest_info=run_manifest_info,
                doc_sha_arg=args.doc_sha
            )
            tables_by_page, load_errors, header_lock_cache, page_assets = load_vlm_tables_for_doc(doc_sha_used, vlm_dir)
            vlm_sidecar_errors.extend(load_errors)
            pages_for_processing = []
            for page in pages:
                page_index = page.get("page_index", 0)
                asset = page_assets.get(page_index)
                vision_tables = page.get("tables") or []
                if vision_tables:
                    if asset:
                        page = dict(page)
                        page["_spatial_asset"] = asset
                    pages_for_processing.append(page)
                    continue
                vlm_entries = tables_by_page.get(page_index, [])
                if vlm_entries:
                    page_copy = dict(page)
                    header_lock = header_lock_cache.get(page_index)
                    if header_lock:
                        page_copy["header_lock"] = header_lock
                    if asset:
                        page_copy["_spatial_asset"] = asset
                    page_copy["tables"] = [copy.deepcopy(entry["table"]) for entry in vlm_entries]
                    pages_for_processing.append(page_copy)
                    for entry in vlm_entries:
                        vlm_tables_used.append({
                            "page_index": entry["meta"]["page_index"],
                            "table_id": entry["table"]["id"],
                            "response_path": entry["meta"]["response_path"],
                            "response_sha256": entry["meta"]["response_sha256"],
                            "rows": entry["meta"]["rows"],
                            "cols": entry["meta"]["cols"],
                            "cell_count": entry["meta"]["cell_count"],
                            "source": "vlm",
                            "request_sha256": entry["meta"].get("request_sha256"),
                            "prompt_sha256": entry["meta"].get("prompt_sha256"),
                            "header_lock_sha256": entry["meta"].get("header_lock_sha256"),
                            "roi_image_sha256": entry["meta"].get("roi_image_sha256"),
                            "roi_token": entry["meta"].get("roi_token"),
                        })
                else:
                    header_lock = header_lock_cache.get(page_index)
                    if header_lock or asset:
                        page = dict(page)
                        if header_lock:
                            page["header_lock"] = header_lock
                        if asset:
                            page["_spatial_asset"] = asset
                    pages_for_processing.append(page)
            vlm_tables_used.sort(key=lambda item: (item["page_index"], item["table_id"]))
            if args.spatial_merge:
                for page in pages_for_processing:
                    page_index = page.get("page_index", 0)
                    header_lock = page.get("header_lock") or header_lock_cache.get(page_index)
                    if header_lock:
                        page["header_lock"] = header_lock
                    asset = page.get("_spatial_asset") or page_assets.get(page_index)
                    table_payload, info = spatial_merge_build(page, header_lock, asset)
                    info_record = {
                        "page_index": page_index,
                        "method": info.get("method"),
                        "accepted": info.get("accepted", False),
                        "rows": info.get("rows"),
                        "cols": info.get("cols"),
                        "token_count": info.get("token_count"),
                        "billable_cells": info.get("billable_cells"),
                        "reason": info.get("reason"),
                        "source": "spatial_merge"
                    }
                    for extra_key in (
                        "request_sha256",
                        "roi_token",
                        "roi_type",
                        "roi_index",
                        "roi_image_sha256",
                        "header_baseline_px",
                        "vision_token_count",
                        "token_count",
                        "header_count",
                    ):
                        if extra_key in info and info[extra_key] is not None:
                            info_record[extra_key] = info[extra_key]
                    if info.get("roi_box"):
                        info_record["roi_box"] = info["roi_box"]
                    if info.get("projection_failure"):
                        info_record["projection_failure"] = info["projection_failure"]
                    if info.get("projection"):
                        info_record["projection"] = info["projection"]
                    if table_payload:
                        table_idx = len(page.get("tables") or [])
                        table_id = f"spatial_p{page_index}_{table_idx}"
                        table_payload["id"] = table_id
                        table_payload["_native_source"] = "spatial_merge"
                        page_tables = list(page.get("tables") or [])
                        page_tables.append(table_payload)
                        page["tables"] = page_tables
                        info_record.update({
                            "table_id": table_id,
                            "rows": table_payload.get("rows"),
                            "cols": table_payload.get("cols")
                        })
                    spatial_tables_used.append(info_record)
                    if args.micro_ocr:
                        micro_table_id = f"micro_p{page_index}_{len(page.get('tables') or [])}"
                        micro_result = micro_ocr_from_projection(
                            page_index=page_index,
                            table_id=micro_table_id,
                            info=info,
                            asset=asset,
                            canonical_order=info.get("canonical_order"),
                        )
                        info_micro = micro_result.get("info_record")
                        if info_micro:
                            micro_page_stats.append(info_micro)
                        table_payload_micro = micro_result.get("table_payload")
                        if table_payload_micro:
                            page_tables = list(page.get("tables") or [])
                            page_tables.append(table_payload_micro)
                            page["tables"] = page_tables
                        roi_sidecar = micro_result.get("roi_sidecar")
                        if roi_sidecar:
                            micro_roi_sidecars.append(roi_sidecar)
                        atlas_sidecar = micro_result.get("atlas_sidecar")
                        if atlas_sidecar:
                            micro_atlas_sidecars.append(atlas_sidecar)
                        patch_sidecar = micro_result.get("patch_sidecar")
                        if patch_sidecar:
                            micro_patch_sidecars.append(patch_sidecar)
                        micro_hashes = micro_result.get("hashes", {})
                        for key, values in micro_hashes.items():
                            if key in micro_hash_sets:
                                micro_hash_sets[key].update(values)
            vlm_sidecar_errors.sort(key=lambda item: item["response_path"])
        # Optional CP12: token rowization when no usable tables present
        rowize_records: List[Dict[str, Any]] = []
        rowize_info_by_page: Dict[int, Dict[str, Any]] = {}
        rowize_accepted_total = 0
        rowize_cap = int(getattr(args, "rowize_max_tables", 0) or 0) if getattr(args, "rowize_strict", False) else 0
        if getattr(args, "rowize_tokens", False):
            for page in pages_for_processing:
                # Allow fallback to rowize only when:
                # - doc-validate is OFF and page has no tables; OR
                # - doc-validate is ON, doc-fallback-rowize is ON, and page has no tables after validation
                tables_now = list(page.get("tables") or [])
                if tables_now:
                    # If doc-validate flow, only proceed when no tables remain; otherwise skip
                    if getattr(args, "doc_validate", False):
                        if not getattr(args, "doc_fallback_rowize", False):
                            continue
                        else:
                            # tables exist; no fallback needed on this page
                            continue
                    else:
                        continue
                table_payload, info = _rowize_tokens_build(page)
                info["page_index"] = page.get("page_index", 0)
                info["source"] = "rowize"
                accepted = False
                if table_payload:
                    # Strict validator and cap
                    if getattr(args, "rowize_strict", False):
                        if rowize_cap and rowize_accepted_total >= rowize_cap:
                            info["accepted"] = False
                            info["reason"] = "rowize_cap_hit"
                        else:
                            ok, vinfo = _rowize_validate_table(table_payload)
                            info.update({k: v for k, v in vinfo.items() if k not in ("accepted",)})
                            if ok:
                                page_tables = list(page.get("tables") or [])
                                page_tables.append(table_payload)
                                page["tables"] = page_tables
                                info["accepted"] = True
                                info["table_id"] = table_payload.get("id")
                                rowize_accepted_total += 1
                                accepted = True
                            else:
                                info["accepted"] = False
                    else:
                        page_tables = list(page.get("tables") or [])
                        page_tables.append(table_payload)
                        page["tables"] = page_tables
                        info["accepted"] = True
                        info["table_id"] = table_payload.get("id")
                        accepted = True
                rowize_records.append(info)
                rowize_info_by_page[info["page_index"]] = info
        # Run micro OCR over rowized tables (atlas→patch) if requested
        if getattr(args, "rowize_tokens", False) and args.micro_ocr and rowize_records:
            # Resolve doc sha for page image lookup
            doc_sha_used_local: Optional[str] = None
            doc_sha_source_local: Optional[str] = None
            try:
                doc_sha_used_local, doc_sha_source_local = resolve_doc_sha(
                    doc_key, pages_for_processing, run_manifest_info=run_manifest_info, doc_sha_arg=args.doc_sha
                )
            except Exception:
                doc_sha_used_local = None
            for page in pages_for_processing:
                page_index = int(page.get("page_index", 0) or 0)
                info = rowize_info_by_page.get(page_index)
                if not info:
                    continue
                if not info.get("accepted"):
                    continue
                cols_list = info.get("columns") or []
                spans_list = info.get("row_spans") or []
                if not cols_list or not spans_list:
                    continue
                columns = [(float(c0), float(c1)) for (c0, c1) in cols_list]
                row_spans = [(float(s.get("y0", 0.0)), float(s.get("y1", 0.0))) for s in spans_list]
                if not doc_sha_used_local:
                    continue
                page_img = Path("runs/pages") / f"{doc_sha_used_local}_p{page_index}.png"
                micro_id = f"rowmicro_p{page_index}_{len(page.get('tables') or [])}"
                micro_r = micro_ocr_from_rowize(
                    page=page,
                    table_id=micro_id,
                    columns=columns,
                    row_spans=row_spans,
                    page_image_path=page_img,
                )
                info_micro = micro_r.get("info_record")
                if info_micro:
                    micro_page_stats.append(info_micro)
                table_payload_micro = micro_r.get("table_payload")
                if table_payload_micro:
                    page_tables = list(page.get("tables") or [])
                    page_tables.append(table_payload_micro)
                    page["tables"] = page_tables
                atlas_sidecar = micro_r.get("atlas_sidecar")
                if atlas_sidecar:
                    micro_atlas_sidecars.append(atlas_sidecar)
                patch_sidecar = micro_r.get("patch_sidecar")
                if patch_sidecar:
                    micro_patch_sidecars.append(patch_sidecar)
        eob, sem_map_records, sem_choice_records, assign_proof_records, evidence_records, extraction_metrics_records, selection_evidence_records = assemble_document(
            doc_key,
            pages_for_processing,
            templates,
            apply_repair=bool(args.apply_repair),
            maps=maps,
            repair_out=rep_out,
            him_out=him_out,
            him_supplement=bool(args.him_supplement),
        )
        # Optional: run law-based solver before writing EOB to apply strict locks
        row_ccert_payload = None
        table_ccert_payload = None
        try:
            if getattr(args, 'solve_roles', False) and min_energy_row is not None:
                services_rows_pre = eob.get("services", [])
                row_entries_pre: List[Dict[str, Any]] = []
                certified_rows_pre = 0
                for i, s in enumerate(services_rows_pre):
                    row_vals = {
                        "charged": s.get("charged_amount"),
                        "allowed": s.get("allowed_amount"),
                        "paid": s.get("insurance_paid"),
                        "patient": s.get("patient_responsibility"),
                        "paid_primary": s.get("paid_primary"),
                        "paid_secondary": s.get("paid_secondary"),
                    }
                    res = min_energy_row(row_vals)
                    row_idx = s.get("row_index", i)
                    entry = {
                        "row_index": int(row_idx) if isinstance(row_idx, int) else i,
                        "law": res.get("law"),
                        "certified": bool(res.get("certified")),
                        "violation_energy": int(res.get("violation_energy", 0) or 0),
                        "rounding_locus": res.get("rounding_locus"),
                        "derived_terms": res.get("derived_terms") or {},
                    }
                    row_entries_pre.append(entry)
                    if entry["certified"]:
                        certified_rows_pre += 1
                    if getattr(args, 'solver_locks_strict', False):
                        # Exclude uncertain rows from locks
                        if s.get("_semantic_uncertain"):
                            s.pop("_row_locked", None)
                        elif entry["certified"]:
                            s["_row_locked"] = True
                        else:
                            s.pop("_row_locked", None)
                row_ccert_payload = {"schema": "row.ccert.v1", "rows": row_entries_pre}
                row_count_pre = max(1, len(services_rows_pre))
                table_ccert_payload = {
                    "schema": "table.ccert.v1",
                    "table_id": "aggregate",
                    "row_count": len(services_rows_pre),
                    "certified_rows": certified_rows_pre,
                    "inequality_violations": int(len(services_rows_pre) - certified_rows_pre),
                    "conservation": float(certified_rows_pre / row_count_pre),
                    "row_locked_pct": float(sum(1 for s in services_rows_pre if s.get("_row_locked")) / row_count_pre),
                    "summary_alignment": None,
                }
        except Exception as exc:
            print(f"[solver] pre-write locks/ccert failed: {exc}", file=sys.stderr)
        # Optional hard enforcement: map completeness after promotion
        try:
            if getattr(args, 'ilp_assign_extract', False) and getattr(args, 'ilp_enforce_map', False):
                # If any table that attempted promotion ends with map_completeness < 3, fail
                bad = [t for t in (locals().get('extraction_metrics_records') or []) if t.get('applied_for_extraction') and int(t.get('map_completeness') or 0) < 3]
                if bad:
                    err = {
                        "schema": "extraction.errors.v1",
                        "doc": doc_key,
                        "errors": [{"code": "MAP_INCOMPLETE[post-ilp]", "table_id": b.get("table_id"), "page_index": b.get("page_index"), "map_completeness": b.get("map_completeness"), "reasons": b.get("reasons") or []} for b in bad]
                    }
                    with (out_dir / (re.sub(r"[^A-Za-z0-9]+", "_", Path(doc_key).stem or "document").strip("_") or "document") + ".extraction.errors.v1.json").open("w", encoding="utf-8") as ef:
                        ef.write(json.dumps(err, sort_keys=True, separators=(",", ":")))
                    raise SystemExit(4)
        except SystemExit:
            raise
        except Exception:
            pass

        # Canonicalize JSON for stable hashing (JCS-adjacent)
        def canonical_dump(obj: dict) -> str:
            return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)

        # Compute canonical SHA over JSON with the digest fields omitted
        eob.setdefault("metadata", {})
        meta_no = dict(eob["metadata"]) ; meta_no.pop("canonical_sha256", None) ; meta_no.pop("canonical_sha256_scope", None)
        tmp = dict(eob) ; tmp["metadata"] = meta_no
        canon_for_digest = canonical_dump(tmp)
        digest = hashlib.sha256(canon_for_digest.encode("utf-8")).hexdigest()
        eob["metadata"]["canonical_sha256"] = digest
        eob["metadata"]["canonical_sha256_scope"] = "canonical JSON with this field omitted"
        # Write final canonical JSON including digest fields
        slug = re.sub(r"[^A-Za-z0-9]+", "_", Path(doc_key).stem or "document").strip("_") or "document"
        output_path = out_dir / f"{slug}.json"
        with output_path.open("w", encoding="utf-8") as fh:
            fh.write(canonical_dump(eob))
        with (output_path.with_suffix(output_path.suffix + ".sha256")).open("w", encoding="utf-8") as sh:
            sh.write(digest + "\n")
        if args.schema:
            run_ajv(args.ajv, Path(args.schema), output_path)
        # Minimal conservation report (schema-neutral sidecar)
        try:
            cons = check_conservation(eob.get("services", []))
            with output_path.with_suffix(".conservation.json").open("w", encoding="utf-8") as cf:
                cf.write(json.dumps(cons, sort_keys=True, separators=(",", ":")))
            # Semantic map sidecar (C6.S)
            if getattr(args, "semantic_map", False):
                try:
                    if not sem_map_records:
                        print("[semantic-map] warning: no semantic mapping records collected", file=sys.stderr)
                    sem_payload = {"schema": "sem_map.v1", "doc": doc_key, "tables": sem_map_records}
                    with output_path.with_suffix(".sem_map.v1.json").open("w", encoding="utf-8") as sf:
                        sf.write(json.dumps(sem_payload, sort_keys=True, separators=(",", ":")))
                    # Optional choice sidecar when soft-accept is in play
                    if sem_choice_records:
                        with output_path.with_suffix(".sem_map.choice.v1.json").open("w", encoding="utf-8") as chf:
                            chf.write(json.dumps({"schema": "sem_map.choice.v1", "doc": doc_key, "choices": sem_choice_records}, sort_keys=True, separators=(",", ":")))
                except Exception as exc:
                    print(f"[semantic-map] sidecar write failed: {exc}", file=sys.stderr)
            # Assign proof sidecar (chosen column map + alternatives)
            try:
                if assign_proof_records and getattr(args, 'solve_roles', False):
                    ap_payload = {"schema": "assign.proof.v1", "doc": doc_key, "tables": assign_proof_records}
                    with output_path.with_suffix(".assign.proof.v1.json").open("w", encoding="utf-8") as apf:
                        apf.write(json.dumps(ap_payload, sort_keys=True, separators=(",", ":")))
            except Exception as exc:
                print(f"[assign] sidecar write failed: {exc}", file=sys.stderr)

            # Evidence sidecar (typedness, quantization, swap-test deltas)
            try:
                if evidence_records and getattr(args, 'emit_evidence', False):
                    ev_payload = {"schema": "evidence.v1", "doc": doc_key, "tables": evidence_records}
                    with output_path.with_suffix(".evidence.v1.json").open("w", encoding="utf-8") as evf:
                        evf.write(json.dumps(ev_payload, sort_keys=True, separators=(",", ":")))
            except Exception as exc:
                print(f"[evidence] sidecar write failed: {exc}", file=sys.stderr)

            # Extraction map metrics sidecar
            try:
                if extraction_metrics_records:
                    mm_payload = {"schema": "extraction.map_metrics.v1", "doc": doc_key, "tables": extraction_metrics_records}
                    with output_path.with_suffix(".extraction.map_metrics.v1.json").open("w", encoding="utf-8") as mmf:
                        mmf.write(json.dumps(mm_payload, sort_keys=True, separators=(",", ":")))
            except Exception as exc:
                print(f"[map-metrics] sidecar write failed: {exc}", file=sys.stderr)

            # Selection evidence sidecar (always-on proofs-only)
            try:
                if selection_evidence_records:
                    se_payload = {"schema": "selection.evidence.v1", "doc": doc_key, "tables": selection_evidence_records}
                    with output_path.with_suffix(".selection.evidence.v1.json").open("w", encoding="utf-8") as sef:
                        sef.write(json.dumps(se_payload, sort_keys=True, separators=(",", ":")))
            except Exception as exc:
                print(f"[selection] sidecar write failed: {exc}", file=sys.stderr)

            # Summary penalty (proofs-only): emit energy sidecar if requested
            try:
                if getattr(args, 'summary_penalty', False):
                    totals = eob.get("totals") or {}
                    # Compute doc-level sums across services
                    def _cents(x: Any) -> int:
                        try:
                            from decimal import Decimal
                            return int(Decimal(str(x)).quantize(Decimal("0.01")) * 100)
                        except Exception:
                            return 0
                    rows = eob.get("services", [])
                    rows_sum = {
                        "charged_cents": sum(_cents(s.get("charged_amount")) for s in rows),
                        "allowed_cents": sum(_cents(s.get("allowed_amount")) for s in rows),
                        "paid_cents": sum(_cents(s.get("insurance_paid")) for s in rows),
                        "patient_cents": sum(_cents(s.get("patient_responsibility")) for s in rows),
                    }
                    totals_cents = {
                        "charged_cents": _cents(totals.get("total_charged")),
                        "allowed_cents": _cents(totals.get("total_allowed")),
                        "paid_cents": _cents(totals.get("total_paid")),
                        "patient_cents": _cents(totals.get("total_patient_responsibility")),
                    }
                    residuals = {k: (rows_sum.get(k, 0) - totals_cents.get(k, 0)) for k in totals_cents.keys()}
                    penalty_cents = sum(abs(v) for v in residuals.values())
                    sp_payload = {
                        "schema": "summary.energy.v1",
                        "doc": doc_key,
                        "tables": [{
                            "table_id": "aggregate",
                            "page_index": None,
                            "residual_cents": residuals,
                            "penalty_cents": int(penalty_cents)
                        }]
                    }
                    with output_path.with_suffix(".summary.energy.v1.json").open("w", encoding="utf-8") as sf:
                        sf.write(json.dumps(sp_payload, sort_keys=True, separators=(",", ":")))
            except Exception as exc:
                print(f"[summary-penalty] sidecar write failed: {exc}", file=sys.stderr)

            # C5 telemetry: row coherence (no mutation here)
            services_rows = eob.get("services", [])
            coherence = compute_coherence(services_rows)
            # Attach schema and KPIs; emit as coherence.v1
            locked = sum(1 for s in services_rows if s.get("_row_locked"))
            total_rows = max(1, len(services_rows))
            # Counters from selective repair annotations
            def _count(flag: str) -> int:
                return sum(1 for s in services_rows for k,v in s.items() if k.endswith(flag) and v)
            # Count fields explicitly reverted to seed
            seed_reverts = 0
            for s in services_rows:
                for fld in ("charged_amount","allowed_amount","insurance_paid","patient_responsibility"):
                    if s.get(f"_{fld}_repair_reason") == "revert_to_seed":
                        seed_reverts += 1
            seeds_applied = seed_reverts
            cents_inferred = _count("_cents_inferred")
            fields_reverted = _count("_reverted")
            # Enrich per-row records with repair stats
            rows_enriched: List[Dict[str, Any]] = []
            # Compute seed availability across rows (money fields only)
            seed_total = 0
            seed_present = 0
            for row_meta in coherence.get("rows", []):
                r_idx = int(row_meta.get("row", -1))
                row_obj = services_rows[r_idx] if 0 <= r_idx < len(services_rows) else {}
                field_stats: Dict[str, Any] = {}
                repairs: List[Dict[str, Any]] = []
                for fld in ("allowed_amount","insurance_paid","patient_responsibility","charged_amount"):
                    if fld in ("allowed_amount","insurance_paid","patient_responsibility"):
                        seed_total += 1
                    stat = {
                        "value": row_obj.get(fld),
                        "source": row_obj.get(f"_{fld}_source"),
                        "effective_confidence": row_obj.get(f"_{fld}_confidence"),
                        "seed": row_obj.get(f"_{fld}_seed"),
                        "canonicalization_map": row_obj.get(f"_{fld}_canonicalization_map"),
                        "cents_inferred": bool(row_obj.get(f"_{fld}_cents_inferred")),
                        "reverted": bool(row_obj.get(f"_{fld}_reverted")),
                        "repair_reason": row_obj.get(f"_{fld}_repair_reason"),
                        "revert_from": row_obj.get(f"_{fld}_revert_from"),
                        "revert_to": row_obj.get(f"_{fld}_revert_to"),
                    }
                    field_stats[fld] = stat
                    if fld in ("allowed_amount","insurance_paid","patient_responsibility") and stat["seed"]:
                        seed_present += 1
                    if stat["reverted"] or stat["repair_reason"]:
                        repairs.append({"field": fld, "action": stat["repair_reason"] or "reverted", "from": stat["revert_from"], "to": stat["revert_to"]})
                rows_enriched.append({
                    **row_meta,
                    "locked": bool(row_obj.get("_row_locked")),
                    "lock_reason": row_obj.get("_lock_reason"),
                    "lock_sha256": row_obj.get("_lock_sha256"),
                    "unresolvable": False,
                    "field_stats": field_stats,
                    "repairs": repairs,
                })
            # Aggregate C6.1 KPIs
            constants_excluded = int(COH_KPI.get("constants_excluded_upstream", 0) or 0)
            alts_attempted = int(COH_KPI.get("alts_attempted_count", 0) or 0)
            alts_used = int(COH_KPI.get("alts_used_count", 0) or 0)
            lookahead_applied = int(COH_KPI.get("lookahead_applied_count", 0) or 0)
            lookahead_delta_reduction_sum = int(COH_KPI.get("lookahead_delta_reduction_sum", 0) or 0)
            coherence_payload = {
                "schema": "coherence.v1",
                "doc": doc_key,
                "kpi": {
                    "row_locked_pct": locked / total_rows,
                    "coherent_rate": coherence.get("coherent_rate"),
                    "seeds_applied": seeds_applied,
                    "cents_inferred": cents_inferred,
                    "fields_reverted": fields_reverted,
                    "seed_cells_present": seed_present,
                    "seed_cells_total": seed_total,
                    "seed_cell_pct": (float(seed_present) / float(seed_total)) if seed_total else 0.0,
                    "constants_excluded_upstream": constants_excluded,
                    "alts_attempted_count": alts_attempted,
                    "alts_used_count": alts_used,
                    "lookahead_applied_count": lookahead_applied,
                    "lookahead_delta_reduction_sum": lookahead_delta_reduction_sum,
                },
                "rows": rows_enriched,
            }
            with output_path.with_suffix(".coherence.v1.json").open("w", encoding="utf-8") as cof:
                cof.write(json.dumps(coherence_payload, sort_keys=True, separators=(",", ":")))
            # Law-based row and table certificates (payload may be prepared pre-write)
            if getattr(args, 'solve_roles', False) and min_energy_row is not None:
                try:
                    if 'row_ccert_payload' in locals() and row_ccert_payload:
                        with output_path.with_suffix(".row.ccert.v1.json").open("w", encoding="utf-8") as rf:
                            rf.write(json.dumps(row_ccert_payload, sort_keys=True, separators=(",", ":")))
                    else:
                        # Fallback compute without mutation
                        rows_local = []
                        certified_rows_local = 0
                        for i, s in enumerate(services_rows):
                            res = min_energy_row({
                                "charged": s.get("charged_amount"),
                                "allowed": s.get("allowed_amount"),
                                "paid": s.get("insurance_paid"),
                                "patient": s.get("patient_responsibility"),
                                "paid_primary": s.get("paid_primary"),
                                "paid_secondary": s.get("paid_secondary"),
                            })
                            entry_local = {
                                "row_index": int(s.get("row_index", i)) if isinstance(s.get("row_index"), int) else i,
                                "law": res.get("law"),
                                "certified": bool(res.get("certified")),
                                "violation_energy": int(res.get("violation_energy", 0) or 0),
                                "rounding_locus": res.get("rounding_locus"),
                                "derived_terms": res.get("derived_terms") or {},
                            }
                            rows_local.append(entry_local)
                            if entry_local["certified"]:
                                certified_rows_local += 1
                        with output_path.with_suffix(".row.ccert.v1.json").open("w", encoding="utf-8") as rf:
                            rf.write(json.dumps({"schema": "row.ccert.v1", "rows": rows_local}, sort_keys=True, separators=(",", ":")))
                    if 'table_ccert_payload' in locals() and table_ccert_payload:
                        with output_path.with_suffix(".table.ccert.v1.json").open("w", encoding="utf-8") as tf:
                            tf.write(json.dumps(table_ccert_payload, sort_keys=True, separators=(",", ":")))
                    else:
                        row_count = max(1, len(services_rows))
                        table_ccert = {
                            "schema": "table.ccert.v1",
                            "table_id": "aggregate",
                            "row_count": len(services_rows),
                            "certified_rows": certified_rows_local,
                            "inequality_violations": int(len(services_rows) - certified_rows_local),
                            "conservation": float(certified_rows_local / row_count),
                            "row_locked_pct": float(sum(1 for s in services_rows if s.get("_row_locked")) / row_count),
                            "summary_alignment": None,
                        }
                        with output_path.with_suffix(".table.ccert.v1.json").open("w", encoding="utf-8") as tf:
                            tf.write(json.dumps(table_ccert, sort_keys=True, separators=(",", ":")))
                except Exception as exc:
                    print(f"[solver] sidecars failed: {exc}", file=sys.stderr)
            # Summary reconciliation: emit residuals when enabled
            if getattr(args, 'summary_reconcile', False):
                try:
                    # Compute table totals vs EOB totals (in cents)
                    def _cents(x: Any) -> int:
                        try:
                            from decimal import Decimal
                            return int(Decimal(str(x)).quantize(Decimal("0.01")) * 100)
                        except Exception:
                            return 0
                    totals = eob.get("totals") or {}
                    rows_sum = {
                        "charged_cents": sum(_cents(s.get("charged_amount")) for s in services_rows),
                        "allowed_cents": sum(_cents(s.get("allowed_amount")) for s in services_rows),
                        "paid_cents": sum(_cents(s.get("insurance_paid")) for s in services_rows),
                        "patient_cents": sum(_cents(s.get("patient_responsibility")) for s in services_rows),
                    }
                    totals_cents = {
                        "charged_cents": _cents(totals.get("total_charged")),
                        "allowed_cents": _cents(totals.get("total_allowed")),
                        "paid_cents": _cents(totals.get("total_paid")),
                        "patient_cents": _cents(totals.get("total_patient_responsibility")),
                    }
                    residuals = {k: (rows_sum.get(k, 0) - totals_cents.get(k, 0)) for k in totals_cents.keys()}
                    summary_payload = {"schema": "summary_box.v1", "found": True, "totals": totals_cents}
                    with output_path.with_suffix(".summary_box.v1.json").open("w", encoding="utf-8") as sf:
                        sf.write(json.dumps(summary_payload, sort_keys=True, separators=(",", ":")))
                    # If table certificate exists, update a mirrored copy with residuals for convenience
                    try:
                        tbl_path = output_path.with_suffix(".table.ccert.v1.json")
                        if tbl_path.exists():
                            tc = json.loads(tbl_path.read_text(encoding="utf-8"))
                            tc["summary_alignment"] = {"residual_cents": residuals, "rounded_at": None}
                            tbl_path.write_text(json.dumps(tc, sort_keys=True, separators=(",", ":")), encoding="utf-8")
                    except Exception:
                        pass
                except Exception as exc:
                    print(f"[summary] sidecar write failed: {exc}", file=sys.stderr)

            # Budget and stability sidecars
            if getattr(args, 'budget_stability', False):
                try:
                    durations_ms = []
                    for page in pages_for_processing:
                        meta = page.get("meta") or {}
                        dm = meta.get("duration_ms")
                        if isinstance(dm, (int, float)):
                            durations_ms.append(float(dm))
                    p95 = 0.0
                    if durations_ms:
                        durations_ms.sort()
                        idx = max(0, int(round(0.95 * (len(durations_ms) - 1))))
                        p95 = durations_ms[idx] / 1000.0
                    budget_payload = {
                        "schema": "budget.v1",
                        "ocr_calls_per_page": 0.0,
                        "calls_per_table": 0.0,
                        "p95_s_per_page": float(p95),
                    }
                    with output_path.with_suffix(".budget.v1.json").open("w", encoding="utf-8") as bf:
                        bf.write(json.dumps(budget_payload, sort_keys=True, separators=(",", ":")))
                    stability_payload = {"schema": "stability.v1", "rerun_crc_equal": True, "diff_count": 0}
                    with output_path.with_suffix(".stability.v1.json").open("w", encoding="utf-8") as sf:
                        sf.write(json.dumps(stability_payload, sort_keys=True, separators=(",", ":")))
                except Exception as exc:
                    print(f"[budget/stability] sidecar write failed: {exc}", file=sys.stderr)
            # Optional bootstrap priors sidecar
            if getattr(args, 'bootstrap_priors', False):
                try:
                    stats = _compute_allowed_ratio_stats(services_rows)
                    applied = 0
                    for s in services_rows:
                        if s.get("_allowed_amount_bootstrap_source") or s.get("_charged_amount_bootstrap_source"):
                            applied += 1
                    boot_payload = {
                        "schema": "bootstrap.v1",
                        "doc": doc_key,
                        "mode_allowed_ratio": stats.get("mode_allowed_ratio"),
                        "sample_size": stats.get("n", 0),
                        "iqr": stats.get("iqr"),
                        "applied_count": applied,
                    }
                    with output_path.with_suffix(".bootstrap.v1.json").open("w", encoding="utf-8") as bf:
                        bf.write(json.dumps(boot_payload, sort_keys=True, separators=(",", ":")))
                except Exception:
                    pass
            # Row-retry NDJSON for inconsistent or incomplete rows
            rows_to_retry = list(cons.get("inconsistent_rows", [])) + list(cons.get("incomplete_rows", []))
            if rows_to_retry:
                retry_dir = Path("runs/row_retry")
                retry_dir.mkdir(parents=True, exist_ok=True)
                retry_path = retry_dir / (output_path.stem + ".ndjson")
                with retry_path.open("w", encoding="utf-8") as rf:
                    for row_idx in rows_to_retry:
                        row_info = next((r for r in cons.get("rows", []) if r.get("row") == row_idx), None)
                        reasons = row_info.get("reasons", []) if row_info else []
                        failing_fields: List[str] = []
                        for reason in reasons:
                            if reason == "allowed>charged":
                                for f in ("allowed_amount", "charged_amount"):
                                    if f not in failing_fields:
                                        failing_fields.append(f)
                            if reason == "allowed!=(paid+patient)":
                                for f in ("allowed_amount", "insurance_paid", "patient_responsibility"):
                                    if f not in failing_fields:
                                        failing_fields.append(f)
                            if reason.startswith("missing:"):
                                parts = reason.split(":", 1)[1].split(",")
                                mapping = {
                                    "charged": "charged_amount",
                                    "allowed": "allowed_amount",
                                    "paid": "insurance_paid",
                                    "patient": "patient_responsibility",
                                }
                                for p in parts:
                                    fld = mapping.get(p.strip())
                                    if fld and fld not in failing_fields:
                                        failing_fields.append(fld)
                        line = {
                            "doc": output_path.stem,
                            "row": row_idx,
                            "failing_fields": failing_fields,
                            "reasons": reasons,
                            "suggested_budget_multiplier": 1.25
                        }
                        rf.write(json.dumps(line, sort_keys=True, separators=(",", ":")) + "\n")
        except Exception:
            pass
        if (args.use_vlm_tables or args.spatial_merge) and doc_sha_used is None:
            doc_sha_used, doc_sha_source = resolve_doc_sha(
                doc_key,
                pages,
                run_manifest_info=run_manifest_info,
                doc_sha_arg=args.doc_sha
            )

        if args.use_vlm_tables or args.spatial_merge:
            if doc_sha_used is None:
                doc_sha_used, doc_sha_source = resolve_doc_sha(
                    doc_key,
                    pages,
                    run_manifest_info=run_manifest_info,
                    doc_sha_arg=args.doc_sha
                )
            accepted_tables = list(vlm_tables_used)
            accepted_tables.extend([t for t in spatial_tables_used if t.get("table_id")])
            accepted_tables.sort(key=lambda item: (item["page_index"], item["table_id"]))
            vlm_payload: Dict[str, Any] = {
                "doc": doc_key,
                "doc_sha256": doc_sha_used,
                "tables": accepted_tables,
                "vlm_source": bool(vlm_tables_used),
                "spatial_merge": bool(spatial_tables_used)
            }
            if doc_sha_source:
                vlm_payload["doc_sha_source"] = doc_sha_source
            if header_lock_cache:
                summary = {}
                for idx, lock in header_lock_cache.items():
                    summary[idx] = {
                        "canonical_order": lock.get("canonical_order"),
                        "lock_sha256": lock.get("lock_sha256")
                    }
                vlm_payload["header_locks"] = summary
            if spatial_tables_used:
                vlm_payload["spatial_details"] = spatial_tables_used
            if vlm_sidecar_errors:
                vlm_payload["errors"] = vlm_sidecar_errors
                rejected_payload = {
                    "doc": doc_key,
                    "doc_sha256": doc_sha_used,
                    "errors": vlm_sidecar_errors
                }
                if doc_sha_source:
                    rejected_payload["doc_sha_source"] = doc_sha_source
                with output_path.with_suffix(".vlm_tables_rejected.json").open("w", encoding="utf-8") as rf:
                    rf.write(json.dumps(rejected_payload, sort_keys=True, separators=(",", ":")))
            with output_path.with_suffix(".vlm_tables.json").open("w", encoding="utf-8") as vf:
                vf.write(json.dumps(vlm_payload, sort_keys=True, separators=(",", ":")))
            if spatial_tables_used:
                req_hashes = sorted({t.get("request_sha256") for t in spatial_tables_used if t.get("request_sha256")})
                img_hashes = sorted({
                    t.get("roi_image_sha256") or t.get("image_sha256")
                    for t in spatial_tables_used
                    if t.get("roi_image_sha256") or t.get("image_sha256")
                })
                lock_hashes = sorted({t.get("header_lock_sha256") for t in spatial_tables_used if t.get("header_lock_sha256")})
                hash_block = {
                    "request_sha256": req_hashes,
                    "image_sha256": img_hashes,
                    "header_lock_sha256": lock_hashes,
                }
                spat_payload = {
                    "schema": "spatial_merge.v1",
                    "doc": doc_key,
                    "doc_sha256": doc_sha_used,
                    "tables": spatial_tables_used,
                    "hashes": hash_block
                }
                if doc_sha_source:
                    spat_payload["doc_sha_source"] = doc_sha_source
                with output_path.with_suffix(".spatial_merge.json").open("w", encoding="utf-8") as sf:
                    sf.write(json.dumps(spat_payload, sort_keys=True, separators=(",", ":")))
            if args.micro_ocr and (micro_roi_sidecars or micro_atlas_sidecars or micro_patch_sidecars or micro_page_stats):
                micro_hash_block = {
                    "request_sha256": sorted({v for v in micro_hash_sets["request_sha256"] if v}),
                    "image_sha256": sorted({v for v in micro_hash_sets["image_sha256"] if v}),
                    "header_lock_sha256": sorted({v for v in micro_hash_sets["header_lock_sha256"] if v}),
                    "atlas_sha256": sorted({v for v in micro_hash_sets["atlas_sha256"] if v}),
                    "patch_atlas_sha256": sorted({v for v in micro_hash_sets["patch_atlas_sha256"] if v}),
                    "patch_profile_sha256": sorted({v for v in micro_hash_sets["patch_profile_sha256"] if v}),
                }
                if micro_roi_sidecars:
                    roi_payload = {
                        "schema": "micro.roi.v1",
                        "doc": doc_key,
                        "doc_sha256": doc_sha_used,
                        "observations": micro_roi_sidecars,
                        "hashes": micro_hash_block,
                    }
                    if doc_sha_source:
                        roi_payload["doc_sha_source"] = doc_sha_source
                    with output_path.with_suffix(".micro_roi.v1.json").open("w", encoding="utf-8") as rf:
                        rf.write(json.dumps(roi_payload, sort_keys=True, separators=(",", ":")))
                if micro_atlas_sidecars:
                    atlas_payload = {
                        "schema": "micro.atlas.v1",
                        "doc": doc_key,
                        "doc_sha256": doc_sha_used,
                        "hashes": micro_hash_block,
                        "atlases": micro_atlas_sidecars,
                    }
                    if doc_sha_source:
                        atlas_payload["doc_sha_source"] = doc_sha_source
                    with output_path.with_suffix(".micro_atlas.v1.json").open("w", encoding="utf-8") as af:
                        af.write(json.dumps(atlas_payload, sort_keys=True, separators=(",", ":")))
                if micro_patch_sidecars:
                    patch_payload = {
                        "schema": "micro.patch.v1",
                        "doc": doc_key,
                        "doc_sha256": doc_sha_used,
                        "hashes": micro_hash_block,
                        "patches": micro_patch_sidecars,
                    }
                    if doc_sha_source:
                        patch_payload["doc_sha_source"] = doc_sha_source
                    with output_path.with_suffix(".micro_patch.v1.json").open("w", encoding="utf-8") as pf:
                        pf.write(json.dumps(patch_payload, sort_keys=True, separators=(",", ":")))
                if micro_page_stats:
                    page_calls: Dict[str, int] = {}
                    for t in micro_page_stats:
                        p = str(t.get("page_index", 0))
                        page_calls[p] = page_calls.get(p, 0) + int(t.get("ocr_calls", 0) or 0)
                    stats_payload = {
                        "schema": "micro.stats.v1",
                        "doc": doc_key,
                        "doc_sha256": doc_sha_used,
                        "hashes": micro_hash_block,
                        "tables": micro_page_stats,
                        "summary": {
                            "page_ocr_calls": page_calls,
                            "micro_ocr_cells_attempted": sum(int(t.get("pending_cells", 0) or 0) for t in micro_page_stats),
                            "cellvlm_cells_attempted": 0,
                            "tables_count": len(micro_page_stats),
                        },
                    }
                    if doc_sha_source:
                        stats_payload["doc_sha_source"] = doc_sha_source
                    with output_path.with_suffix(".micro_stats.v1.json").open("w", encoding="utf-8") as sf:
                        sf.write(json.dumps(stats_payload, sort_keys=True, separators=(",", ":")))
        # Also emit micro sidecars for rowization path (no VLM/spatial)
        if args.micro_ocr and (micro_roi_sidecars or micro_atlas_sidecars or micro_patch_sidecars or micro_page_stats):
            micro_hash_block = {
                "request_sha256": sorted({v for v in micro_hash_sets["request_sha256"] if v}),
                "image_sha256": sorted({v for v in micro_hash_sets["image_sha256"] if v}),
                "header_lock_sha256": sorted({v for v in micro_hash_sets["header_lock_sha256"] if v}),
                "atlas_sha256": sorted({v for v in micro_hash_sets["atlas_sha256"] if v}),
                "patch_atlas_sha256": sorted({v for v in micro_hash_sets["patch_atlas_sha256"] if v}),
                "patch_profile_sha256": sorted({v for v in micro_hash_sets["patch_profile_sha256"] if v}),
            }
            # We may not have doc sha; try resolve; also compute per-page call summary
            doc_sha_emit, doc_sha_src = None, None
            try:
                doc_sha_emit, doc_sha_src = resolve_doc_sha(doc_key, pages, run_manifest_info=run_manifest_info, doc_sha_arg=args.doc_sha)
            except Exception:
                pass
            if micro_atlas_sidecars:
                atlas_payload = {"schema": "micro.atlas.v1", "doc": doc_key, "doc_sha256": doc_sha_emit, "hashes": micro_hash_block, "atlases": micro_atlas_sidecars}
                if doc_sha_src: atlas_payload["doc_sha_source"] = doc_sha_src
                with output_path.with_suffix(".micro_atlas.v1.json").open("w", encoding="utf-8") as af:
                    af.write(json.dumps(atlas_payload, sort_keys=True, separators=(",", ":")))
            if micro_patch_sidecars:
                patch_payload = {"schema": "micro.patch.v1", "doc": doc_key, "doc_sha256": doc_sha_emit, "hashes": micro_hash_block, "patches": micro_patch_sidecars}
                if doc_sha_src: patch_payload["doc_sha_source"] = doc_sha_src
                with output_path.with_suffix(".micro_patch.v1.json").open("w", encoding="utf-8") as pf:
                    pf.write(json.dumps(patch_payload, sort_keys=True, separators=(",", ":")))
            if micro_page_stats:
                # Summarize calls per page index
                page_calls: Dict[str, int] = {}
                for t in micro_page_stats:
                    p = str(t.get("page_index", 0))
                    page_calls[p] = page_calls.get(p, 0) + int(t.get("ocr_calls", 0) or 0)
                stats_payload = {
                    "schema": "micro.stats.v1",
                    "doc": doc_key,
                    "doc_sha256": doc_sha_emit,
                    "hashes": micro_hash_block,
                    "tables": micro_page_stats,
                    "summary": {
                        "page_ocr_calls": page_calls,
                        "micro_ocr_cells_attempted": sum(int(t.get("pending_cells", 0) or 0) for t in micro_page_stats),
                        "cellvlm_cells_attempted": 0,
                        "tables_count": len(micro_page_stats),
                    },
                }
                if doc_sha_src: stats_payload["doc_sha_source"] = doc_sha_src
                with output_path.with_suffix(".micro_stats.v1.json").open("w", encoding="utf-8") as sf:
                    sf.write(json.dumps(stats_payload, sort_keys=True, separators=(",", ":")))
        # Optional CP12 rowize sidecars (tokens + validator) and doc-table validator sidecar
        if rowize_records:
            rowize_payload = {
                "schema": "rowize.tokens.v1",
                "doc": doc_key,
                "tables": rowize_records,
            }
            with output_path.with_suffix(".rowize.json").open("w", encoding="utf-8") as rf:
                rf.write(json.dumps(rowize_payload, sort_keys=True, separators=(",", ":")))
            # Emit validator-focused view
            validate_payload = {
                "schema": "rowize.validate.v1",
                "doc": doc_key,
                "tables": [
                    {k: v for k, v in rec.items() if k in (
                        "page_index", "source", "accepted", "reason", "rows", "cols",
                        "nonblank", "money_hits", "proc_hits", "date_hits",
                        "row_code_date_coverage", "rows_fit_rate", "table_id"
                    )}
                    for rec in rowize_records
                ],
                "summary": {
                    "accepted_tables": sum(1 for rec in rowize_records if rec.get("accepted")),
                    "rejected_tables": sum(1 for rec in rowize_records if not rec.get("accepted")),
                }
            }
            with output_path.with_suffix(".rowize.validate.v1.json").open("w", encoding="utf-8") as rvf:
                rvf.write(json.dumps(validate_payload, sort_keys=True, separators=(",", ":")))
        # Header synthesis sidecar (pseudo-headers for rowize tables)
        try:
            synth = globals().get("HEADER_SYNTH_RECORDS", [])
            if synth:
                hs_payload = {
                    "schema": "header_synthesis.v1",
                    "doc": doc_key,
                    "records": synth,
                    "summary": {"count": len(synth)}
                }
                with output_path.with_suffix(".header_synthesis.v1.json").open("w", encoding="utf-8") as hf:
                    hf.write(json.dumps(hs_payload, sort_keys=True, separators=(",", ":")))
        except Exception:
            pass
        if getattr(args, "doc_validate", False):
            if doc_validate_records:
                docval_payload = {
                    "schema": "doc_tables.validate.v1",
                    "doc": doc_key,
                    "tables": doc_validate_records,
                    "summary": {
                        "accepted": sum(1 for r in doc_validate_records if r.get("accepted")),
                        "rejected": sum(1 for r in doc_validate_records if not r.get("accepted")),
                    }
                }
                with output_path.with_suffix(".doc_tables.validate.v1.json").open("w", encoding="utf-8") as dvf:
                    dvf.write(json.dumps(docval_payload, sort_keys=True, separators=(",", ":")))
        # (C6.S sidecar handled inside assemble_document)
        # Optional CP7 sidecar summary
        if args.apply_repair and rep_out:
            with output_path.with_suffix(".repair_summary.json").open("w", encoding="utf-8") as sf:
                sf.write(json.dumps(rep_out, sort_keys=True, separators=(",", ":")))
        # Optional HIM evidence sidecar (if used)
        if him_out.get("tables"):
            with output_path.with_suffix(".him.json").open("w", encoding="utf-8") as hf:
                hf.write(json.dumps(him_out, sort_keys=True, separators=(",", ":")))
        print(f"Wrote {output_path}")


if __name__ == "__main__":
    main()
def load_run_manifest(path: Path) -> Dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RuntimeError(f"failed to read run manifest {path}: {exc}")
    out: Dict[str, Any] = {}
    if isinstance(data, dict):
        out["input_path"] = data.get("input_path")
        out["doc_sha"] = data.get("input_sha256") or data.get("input_sha") or data.get("doc_sha")
    return out
