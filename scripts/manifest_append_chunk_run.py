#!/usr/bin/env python3
"""Append chunk_runs[] entry to a run manifest."""
from __future__ import annotations

import json
import sys
from pathlib import Path

def main() -> None:
    if len(sys.argv) < 4:
        print("usage: manifest_append_chunk_run.py MANIFEST INPUT_EOB OUTPUT_JSONL", file=sys.stderr)
        sys.exit(2)
    manifest = Path(sys.argv[1])
    input_eob = sys.argv[2]
    output_jsonl = Path(sys.argv[3])
    entry = {
        "input_eob_path": input_eob,
        "output_jsonl_path": str(output_jsonl),
        "schema": "rag/chunk.v1"
    }
    try:
        data = json.loads(manifest.read_text())
    except Exception:
        print(f"error: cannot read manifest {manifest}", file=sys.stderr)
        sys.exit(1)
    # Compute counts/hashes
    import zlib, hashlib
    payload = output_jsonl.read_bytes()
    entry["line_count"] = sum(1 for _ in output_jsonl.read_text().splitlines() if _.strip())
    entry["crc32"] = f"{zlib.crc32(payload) & 0xffffffff:08x}"
    entry["response_sha256"] = hashlib.sha256(payload).hexdigest()
    from datetime import datetime, timezone
    entry["ts"] = datetime.now(timezone.utc).isoformat()
    runs = data.get("chunk_runs") or []
    runs.append(entry)
    data["chunk_runs"] = runs
    manifest.write_text(json.dumps(data, indent=2))
    print(f"appended chunk_runs to {manifest}")

if __name__ == "__main__":
    main()

