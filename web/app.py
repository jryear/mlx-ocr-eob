#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from fastapi import FastAPI, Request, UploadFile, File, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "vision-ocr-cli/.build/release/vision-ocr-cli"

app = FastAPI(title="EOB Demo UI (C6)")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")


def _slug(name: str) -> str:
    return "".join(ch if (ch.isalnum() or ch == "_") else "_" for ch in name).strip("_") or "document"


def _run(cmd: List[str]) -> Tuple[int, str, str]:
    proc = subprocess.run(cmd, text=True, capture_output=True)
    return proc.returncode, proc.stdout, proc.stderr


def _rows_3of4(eob: Dict[str, Any]) -> Tuple[int, int]:
    keys = ("charged_amount", "allowed_amount", "insurance_paid", "patient_responsibility")
    rows = eob.get("services") or []
    good = 0
    for r in rows:
        cnt = sum(1 for k in keys if (isinstance(r.get(k), str) and r.get(k).strip()))
        if cnt >= 3:
            good += 1
    return good, len(rows)


async def run_pipeline(pdf_path: Path) -> Dict[str, Any]:
    """Capture + assemble (C6) deterministically for a single PDF."""
    # Ensure CLI exists
    if not CLI.exists():
        code, out, err = _run(["make", "ensure-release"])
        if code != 0 or not CLI.exists():
            raise RuntimeError(f"vision-ocr-cli not built: {err}")

    slug = _slug(pdf_path.stem)
    bench_dir = ROOT / "bench/webdemo"
    out_dir = ROOT / "out/webdemo"
    runs_dir = ROOT / "runs/webdemo"
    bench_dir.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)
    runs_dir.mkdir(parents=True, exist_ok=True)

    ndjson = bench_dir / f"{slug}.jsonl"
    run_manifest = runs_dir / f"{slug}.run.json"
    if ndjson.exists():
        ndjson.unlink()
    if run_manifest.exists():
        run_manifest.unlink()

    cmd = [
        os.fspath(CLI), os.fspath(pdf_path),
        "--dpi", "150", "--mode", "text", "--lang", "en-US",
        "--ndjson", os.fspath(ndjson),
        "--log-run", os.fspath(run_manifest),
        "--export-images", "runs/pages",
    ]
    t0 = time.perf_counter()
    code, out, err = _run(cmd)
    wall = time.perf_counter() - t0
    if code != 0:
        raise RuntimeError(f"capture failed rc={code}: {err}")

    assemble_cmd = [
        os.fspath(Path(sys.executable) if (sys_exe := os.getenv('PYTHON')) else Path("python3")),
        os.fspath(ROOT / "scripts/assemble_eob.py"),
        "--pages", os.fspath(bench_dir),
        "--glob", f"{slug}.jsonl",
        "--mappings", os.fspath(ROOT / "config/payers"),
        "--out", os.fspath(out_dir),
        "--rowize-tokens", "--him-supplement", "--micro-ocr", "--selective-repair-locks", "--semantic-map",
    ]
    # Use subprocess for determinism
    code2, out2, err2 = _run(assemble_cmd)
    if code2 != 0:
        raise RuntimeError(f"assemble failed rc={code2}: {err2}")

    eob_path = out_dir / f"{slug}.json"
    eob = json.loads(eob_path.read_text()) if eob_path.exists() else {}
    cons_path = eob_path.with_suffix(".conservation.json")
    coh_path = eob_path.with_suffix(".coherence.v1.json")
    cons = json.loads(cons_path.read_text()) if cons_path.exists() else {}
    coh = json.loads(coh_path.read_text()) if coh_path.exists() else {}
    three, total = _rows_3of4(eob)
    return {
        "slug": slug,
        "wall_time_s": round(wall, 3),
        "eob_path": os.fspath(eob_path),
        "conservation": cons,
        "coherence": coh,
        "rows_3of4": [three, total],
    }


def _scan_outputs() -> List[Dict[str, str]]:
    outs: List[Dict[str, str]] = []
    for root, _, files in os.walk(ROOT / "out"):
        for f in files:
            if f.endswith(".json") and not (".coherence" in f or ".micro_" in f or ".conservation" in f):
                p = Path(root) / f
                outs.append({
                    "name": f,
                    "path": os.fspath(p.relative_to(ROOT))
                })
    outs.sort(key=lambda d: d["path"])  # stable listing
    return outs


@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    docs = _scan_outputs()
    return templates.TemplateResponse("index.html", {"request": request, "docs": docs})


@app.post("/run", response_class=HTMLResponse)
async def run_pdf(request: Request, upload: UploadFile = File(...)) -> HTMLResponse:
    up_dir = ROOT / "runs/uploads"
    up_dir.mkdir(parents=True, exist_ok=True)
    dest = up_dir / upload.filename
    with dest.open("wb") as fh:
        shutil.copyfileobj(upload.file, fh)
    try:
        result = await run_pipeline(dest)
    except Exception as exc:
        return templates.TemplateResponse("run.html", {"request": request, "error": str(exc)})
    return RedirectResponse(url=f"/doc?path={result['eob_path'].replace(str(ROOT) + os.sep, '')}", status_code=302)


@app.get("/doc", response_class=HTMLResponse)
async def view_doc(request: Request, path: str) -> HTMLResponse:
    p = ROOT / path
    if not p.exists():
        return templates.TemplateResponse("doc.html", {"request": request, "error": f"Not found: {path}"})
    eob = json.loads(p.read_text())
    cons = {}
    coh = {}
    cons_p = p.with_suffix(".conservation.json")
    coh_p = p.with_suffix(".coherence.v1.json")
    if cons_p.exists():
        cons = json.loads(cons_p.read_text())
    if coh_p.exists():
        coh = json.loads(coh_p.read_text())
    three, total = _rows_3of4(eob)
    return templates.TemplateResponse("doc.html", {
        "request": request,
        "path": path,
        "eob": eob,
        "conservation": cons,
        "coherence": coh,
        "rows_3of4": [three, total],
    })
