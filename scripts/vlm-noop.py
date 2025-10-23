#!/usr/bin/env python3
"""No-op VLM runner.

Reads a request on stdin and emits a valid vlm.response.v1 with empty tables/text.
Use with `--vlm-runner scripts/vlm-noop.py` for plumbing tests.
"""
from __future__ import annotations

import json
import sys


def main() -> None:
    data = sys.stdin.read()
    if not data.strip():
        return
    req = json.loads(data)
    resp = {
        "schema": "vlm.response.v1",
        "page_index": req.get("page_index", 0),
        "roi_id": req.get("roi_id"),
        "tables": [],
        "text_blocks": [],
        "model_meta": {"runner": "noop", "echo": True}
    }
    json.dump(resp, sys.stdout)


if __name__ == "__main__":
    main()
