#!/usr/bin/env python3
"""NativeOCR benchmarking harness.

Runs the Swift CLI across a corpus, collects runtime + confidence stats, and
optionally computes WER/CER against reference transcripts.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

SUPPORTED_EXTS = {".pdf", ".tiff", ".tif", ".png", ".jpg", ".jpeg"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("corpus", type=Path, help="Directory containing input docs")
    parser.add_argument("--cli", type=Path, required=True, help="Path to vision-ocr-cli binary")
    parser.add_argument("--dpi", type=int, default=300, help="Rasterization DPI (default: 300)")
    parser.add_argument("--mode", choices=["text", "document"], default="text", help="CLI mode flag")
    parser.add_argument("--lang", default="en-US", help="Comma-separated language codes")
    parser.add_argument("--timeout", type=int, default=None, help="Optional CLI timeout in milliseconds")
    parser.add_argument("--pages", default=None, help="Optional page slice start:end")
    parser.add_argument("--out", type=Path, default=Path("bench/metrics.csv"), help="Destination CSV (default: bench/metrics.csv)")
    parser.add_argument("--reference-root", type=Path, default=None, help="Directory with reference .txt files (stem.page.txt convention)")
    parser.add_argument("--keep-raw", type=Path, default=None, help="Optional directory to store raw NDJSON outputs")
    return parser.parse_args()


def iter_documents(root: Path) -> Iterable[Path]:
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.suffix.lower() in SUPPORTED_EXTS:
            yield path


def run_cli(cli: Path, doc: Path, args: argparse.Namespace) -> Tuple[List[Dict], str]:
    cmd = [str(cli), str(doc), "--dpi", str(args.dpi), "--mode", args.mode, "--lang", args.lang]
    if args.pages:
        cmd.extend(["--pages", args.pages])
    if args.timeout:
        cmd.extend(["--timeout", str(args.timeout)])

    ndjson_path = None
    if args.keep_raw:
        args.keep_raw.mkdir(parents=True, exist_ok=True)
        ndjson_path = args.keep_raw / f"{doc.stem}.jsonl"
        cmd.extend(["--ndjson", str(ndjson_path)])

    start = time.perf_counter()
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    elapsed = time.perf_counter() - start

    if result.returncode != 0:
        raise RuntimeError(f"CLI failed for {doc}: rc={result.returncode}\n{result.stderr}")

    lines: List[str]
    if ndjson_path:
        raw = ndjson_path.read_text() if ndjson_path and ndjson_path.exists() else ""
        lines = [line for line in raw.splitlines() if line.strip()]
    else:
        lines = [line for line in result.stdout.splitlines() if line.strip()]

    payloads = [json.loads(line) for line in lines]
    return payloads, f"{elapsed:.3f}"


def wer(ref: List[str], hyp: List[str]) -> float:
    if not ref:
        return 0.0 if not hyp else 1.0
    n = len(ref)
    m = len(hyp)
    dp = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(n + 1):
        dp[i][0] = i
    for j in range(m + 1):
        dp[0][j] = j
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            cost = 0 if ref[i - 1] == hyp[j - 1] else 1
            dp[i][j] = min(
                dp[i - 1][j] + 1,
                dp[i][j - 1] + 1,
                dp[i - 1][j - 1] + cost,
            )
    return dp[n][m] / float(n)


def cer(ref: str, hyp: str) -> float:
    if not ref:
        return 0.0 if not hyp else 1.0
    n = len(ref)
    m = len(hyp)
    dp = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(n + 1):
        dp[i][0] = i
    for j in range(m + 1):
        dp[0][j] = j
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            cost = 0 if ref[i - 1] == hyp[j - 1] else 1
            dp[i][j] = min(
                dp[i - 1][j] + 1,
                dp[i][j - 1] + 1,
                dp[i - 1][j - 1] + cost,
            )
    return dp[n][m] / float(n)


def reference_text(doc: Path, page_index: int, root: Optional[Path]) -> Optional[str]:
    if not root:
        return None
    page_path = root / f"{doc.stem}.page{page_index}.txt"
    if page_path.exists():
        return page_path.read_text().strip()
    doc_path = root / f"{doc.stem}.txt"
    if doc_path.exists():
        return doc_path.read_text().strip()
    return None


def ensure_parent(path: Path) -> None:
    if path.parent:
        path.parent.mkdir(parents=True, exist_ok=True)


def main() -> None:
    args = parse_args()
    if not args.cli.exists():
        raise SystemExit(f"CLI not found: {args.cli}")
    if not args.corpus.exists():
        raise SystemExit(f"Corpus not found: {args.corpus}")

    ensure_parent(args.out)
    rows: List[List] = []

    for doc in iter_documents(args.corpus):
        try:
            payloads, wall = run_cli(args.cli, doc, args)
        except RuntimeError as exc:
            print(exc, file=sys.stderr)
            continue

        for page in payloads:
            page_index = page.get("page_index", 0)
            meta = page.get("meta", {})
            joined_text = "\n".join(line.get("text", "") for line in page.get("lines", []))
            reference = reference_text(doc, page_index, args.reference_root)
            wer_score = None
            cer_score = None
            if reference:
                wer_score = wer(reference.split(), joined_text.split())
                cer_score = cer(reference, joined_text)

            routing = page.get("routing", {}) if isinstance(page.get("routing"), dict) else {}
            decision = routing.get("decision") or meta.get("route")
            non_content = page.get("non_content")
            mode_fallback = page.get("mode_fallback")

            rows.append([
                str(doc.relative_to(args.corpus)),
                page_index,
                meta.get("duration_ms"),
                meta.get("line_count"),
                meta.get("mean_confidence"),
                meta.get("line_density"),
                meta.get("numeric_density"),
                meta.get("tau_conf"),
                decision,
                non_content,
                mode_fallback,
                meta.get("script_conflict_rate"),
                meta.get("script_conflict_rate_max"),
                wall,
                wer_score,
                cer_score,
                None,
                None,
            ])

    header = [
        "document",
        "page_index",
        "duration_ms",
        "line_count",
        "mean_confidence",
        "line_density",
        "numeric_density",
        "tau_conf",
        "route",
        "non_content",
        "mode_fallback",
        "script_conflict_rate",
        "script_conflict_rate_max",
        "doc_wall_time_s",
        "wer",
        "cer",
        "kv_f1",
        "cell_f1"
    ]
    with args.out.open("w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(header)
        writer.writerows(rows)

    print(f"wrote {len(rows)} rows → {args.out}")


if __name__ == "__main__":
    main()
