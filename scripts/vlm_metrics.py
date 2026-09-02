#!/usr/bin/env python3
"""Extract VLM performance metrics from response files."""
from __future__ import annotations

import json
import glob
import sys
from pathlib import Path

def main() -> None:
    vlm_dir = sys.argv[1] if len(sys.argv) > 1 else 'runs/vlm'
    files = glob.glob(f'{vlm_dir}/*_response.json')

    if not files:
        print(f'No VLM responses in {vlm_dir}')
        return

    times = []
    chars = []
    cells = []
    attempts = []

    for f in files:
        with open(f) as fp:
            d = json.load(fp)
        m = d.get('model_meta', {})
        if 'sec' in m:
            times.append(m['sec'])
        if 'chars_out' in m:
            chars.append(m['chars_out'])
        if 'cells_out' in m:
            cells.append(m['cells_out'])
        if 'attempts' in m:
            attempts.append(m['attempts'])

    print(f'ROIs processed: {len(times)}')
    if times:
        print(f'Time per ROI: avg={sum(times)/len(times):.2f}s, min={min(times):.2f}s, max={max(times):.2f}s')
    if chars:
        print(f'Chars output: avg={sum(chars)//len(chars)}, min={min(chars)}, max={max(chars)}')
    if cells:
        print(f'Cells output: avg={sum(cells)//len(cells)}, min={min(cells)}, max={max(cells)}')
    if attempts:
        max_attempts = max(attempts)
        retries = sum(1 for a in attempts if a > 1)
        print(f'Parse attempts: max={max_attempts}, retries={retries}/{len(attempts)}')

if __name__ == '__main__':
    main()
