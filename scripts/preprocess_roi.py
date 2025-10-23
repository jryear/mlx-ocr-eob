#!/usr/bin/env python3
"""Lightweight ROI preprocessor stub.

Copies input image to output path and emits a small JSON ops log to stdout.
Can be replaced with an OpenCV-based implementation without changing the CLI.
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path


def main() -> None:
    if len(sys.argv) < 3:
        print(json.dumps({"ops": ["noop"], "error": "usage: preprocess_roi.py IN OUT"}))
        return
    src = Path(sys.argv[1])
    dst = Path(sys.argv[2])
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)
        print(json.dumps({"ops": ["noop"]}))
    except Exception as exc:
        print(json.dumps({"ops": ["noop"], "error": str(exc)}))


if __name__ == "__main__":
    main()

