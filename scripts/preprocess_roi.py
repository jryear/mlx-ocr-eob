#!/usr/bin/env python3
"""Deterministic ROI preprocessor.

Pipeline (best-effort with graceful fallback):
1) Grayscale master for VLM.
2) CLAHE (clip=3.0, tiles=8x8) if OpenCV available; else global equalize fallback.
3) Optional Sauvola binarization computed for diagnostics only (not saved as output).
4) Deskew estimate (±2–12°) if OpenCV available; else 0.0.

Always writes a single grayscale PNG to OUT and emits a compact JSON ops log to stdout.
No network access; falls back to copies if libraries are missing.
"""
from __future__ import annotations

import io
import json
import sys
from pathlib import Path


def _load_image_pil(path: Path):
    from PIL import Image
    im = Image.open(path).convert("L")  # grayscale master
    return im


def _save_png_pil(img, path: Path):
    from PIL import Image
    if img.mode != "L":
        img = img.convert("L")
    img.save(path, format="PNG")


def _apply_clahe_pil(img):
    """Best-effort CLAHE using OpenCV if available; else global equalize fallback.

    Returns (img_out, ops_tag, meta_dict)
    """
    try:
        import numpy as np
        import cv2  # type: ignore
        arr = np.array(img)
        clahe = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8))
        out = clahe.apply(arr)
        from PIL import Image
        return Image.fromarray(out, mode="L"), "clahe", {"clip": 3.0, "tiles": [8, 8]}
    except Exception:
        try:
            from PIL import ImageOps
            out = ImageOps.equalize(img)
            return out, "equalize_fallback", {"clip": 3.0, "tiles": [8, 8]}
        except Exception:
            return img, "noop", {}


def _estimate_deskew_deg(img) -> float:
    """Estimate skew angle in degrees (±12) using OpenCV if available; fallback 0.0.
    """
    try:
        import numpy as np
        import cv2  # type: ignore
        arr = np.array(img)
        # Edge detect + Hough to estimate dominant text angle
        edges = cv2.Canny(arr, 50, 150)
        lines = cv2.HoughLines(edges, 1, np.pi / 180.0, threshold=150)
        if lines is None:
            return 0.0
        angles = []
        for rho, theta in lines[:, 0, :]:
            deg = (theta * 180.0 / np.pi) - 90.0
            # normalize to [-90, 90]
            while deg <= -90.0:
                deg += 180.0
            while deg > 90.0:
                deg -= 180.0
            angles.append(deg)
        if not angles:
            return 0.0
        import statistics as stats
        est = float(stats.median(angles))
        # Clamp to small deskew band
        if abs(est) < 2.0:
            return 0.0
        if abs(est) > 12.0:
            est = 12.0 if est > 0 else -12.0
        return round(est, 2)
    except Exception:
        return 0.0


def _rotate_if_needed(img, deg: float):
    if abs(deg) < 0.01:
        return img
    from PIL import Image
    # Expand to keep full content; fill with white
    return img.rotate(deg, resample=Image.BICUBIC, expand=True, fillcolor=255)


def main() -> None:
    if len(sys.argv) < 3:
        print(json.dumps({"ops": ["noop"], "error": "usage: preprocess_roi.py IN OUT"}))
        return
    src = Path(sys.argv[1])
    dst = Path(sys.argv[2])
    ops = []
    out: dict = {"ops": ops}
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        # 1) grayscale
        img = _load_image_pil(src)
        ops.append("grayscale")
        # 2) CLAHE (or equalize fallback)
        img, clahe_tag, clahe_meta = _apply_clahe_pil(img)
        ops.append(clahe_tag)
        if clahe_meta:
            out["clahe"] = clahe_meta
        # 3) deskew estimate
        deg = _estimate_deskew_deg(img)
        out["deskew_deg"] = deg
        if abs(deg) > 0.01:
            img = _rotate_if_needed(img, -deg)  # rotate opposite to deskew
            ops.append(f"deskew={deg}")
        else:
            ops.append("deskew=0.0")
        # Save grayscale master only (no binarized output)
        _save_png_pil(img, dst)
        print(json.dumps(out, separators=(",", ":")))
    except Exception as exc:
        out["error"] = str(exc)
        # Best-effort fallback: copy via PIL
        try:
            img = _load_image_pil(src)
            _save_png_pil(img, dst)
        except Exception:
            pass
        print(json.dumps(out, separators=(",", ":")))


if __name__ == "__main__":
    main()
