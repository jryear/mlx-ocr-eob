#!/usr/bin/env python3
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Dict, Tuple, List, Optional


def load_maps(path: str | Path) -> Dict[str, Dict[str, str]]:
    data = json.loads(Path(path).read_text())
    return {k: {kk: vv for kk, vv in v.items()} for k, v in data.items()}


def _apply_map(text: str, m: Dict[str, str]) -> Tuple[str, List[str]]:
    out = []
    notes = []
    for ch in text:
        if ch in m:
            out.append(m[ch])
            notes.append(f"map:{ch}_to_{m[ch]}")
        else:
            out.append(ch)
    return ("".join(out), notes)


MONEY_RE = re.compile(r"^\-?\d{1,9}(?:\.\d{2})$")


def repair_money(text: str, cmap: Dict[str, str]) -> Tuple[Optional[str], List[str]]:
    raw = text.strip()
    notes: List[str] = []
    # parens negative
    if raw.startswith("(") and raw.endswith(")"):
        raw = "-" + raw[1:-1]
        notes.append("money.format.parens_to_negative")
    raw = raw.replace("$", "")
    raw = re.sub(r"(?<=\d)[,'](?=\d{3}(?:\D|$))", "", raw)
    mapped, mnotes = _apply_map(raw, cmap)
    if mnotes:
        notes.extend([f"money.glyph.{n}" for n in mnotes])
    if mapped.endswith("."):
        mapped += "00"; notes.append("money.format.dot_pad")
    if "." not in mapped:
        mapped += ".00"; notes.append("money.format.ensure_decimals")
    if not MONEY_RE.match(mapped):
        return None, notes + ["money.validate.fail"]
    return mapped, notes


def repair_date(text: str, cmap: Dict[str, str]) -> Tuple[Optional[str], List[str]]:
    raw = text.strip()
    mapped, mnotes = _apply_map(raw, cmap)
    notes = [f"date.glyph.{n}" for n in mnotes]
    fmts = ["%m/%d/%Y", "%Y-%m-%d", "%m-%d-%y"]
    for fmt in fmts:
        try:
            dt = datetime.strptime(mapped, fmt)
            # century rule for %y: assume 2000+
            if fmt == "%m-%d-%y" and dt.year < 1970:
                dt = dt.replace(year=dt.year + 2000)
            return dt.strftime("%Y-%m-%d"), notes + [f"date.format.{fmt}"]
        except Exception:
            continue
    return None, notes + ["date.validate.fail"]


ADA_RE = re.compile(r"^D\d{4}[A-Z]?$", re.IGNORECASE)
CPT_RE = re.compile(r"^[0-9]{4,5}[A-Z0-9\-]*$", re.IGNORECASE)


def repair_code(text: str, cmap: Dict[str, str]) -> Tuple[Optional[str], List[str]]:
    raw = text.strip().upper()
    mapped, mnotes = _apply_map(raw, cmap)
    notes = [f"code.glyph.{n}" for n in mnotes]
    if ADA_RE.match(mapped) or CPT_RE.match(mapped):
        return mapped, notes
    return None, notes + ["code.validate.fail"]


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--maps", default="config/confusion_maps.v1.json")
    ap.add_argument("--type", choices=["money", "date", "code"], required=True)
    ap.add_argument("--text", required=True)
    args = ap.parse_args()
    maps = load_maps(args.maps)
    if args.type == "money":
        repaired, notes = repair_money(args.text, maps.get("money", {}))
    elif args.type == "date":
        repaired, notes = repair_date(args.text, maps.get("date", {}))
    else:
        repaired, notes = repair_code(args.text, maps.get("code", {}))
    print(json.dumps({"input": args.text, "repaired": repaired, "notes": notes}, ensure_ascii=False))


if __name__ == "__main__":
    main()

