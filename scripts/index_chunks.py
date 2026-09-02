#!/usr/bin/env python3
"""Create DuckDB index for chunks."""
from __future__ import annotations

import duckdb  # type: ignore
from pathlib import Path

def main() -> None:
    idx_dir = Path('index/duckdb'); idx_dir.mkdir(parents=True, exist_ok=True)
    db_path = idx_dir / 'eobs.duckdb'
    con = duckdb.connect(str(db_path))
    con.execute("CREATE TABLE IF NOT EXISTS chunks AS SELECT * FROM read_json_auto('out/chunks/*.jsonl');")
    con.execute("CREATE INDEX IF NOT EXISTS idx_type      ON chunks(type);")
    con.execute("CREATE INDEX IF NOT EXISTS idx_proc_cd   ON chunks(proc_cd);")
    con.execute("CREATE INDEX IF NOT EXISTS idx_decision  ON chunks(decision);")
    con.execute("CREATE INDEX IF NOT EXISTS idx_claim_qtr ON chunks(claim_quarter);")
    con.execute("CREATE INDEX IF NOT EXISTS idx_office    ON chunks(office_id);")
    con.execute("CREATE INDEX IF NOT EXISTS idx_payer     ON chunks(payer_name);")
    print(f"Indexed chunks in {db_path}")

if __name__ == '__main__':
    main()

