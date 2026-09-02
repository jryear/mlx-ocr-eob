#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path
from typing import Tuple

from lexical_repair import load_maps, repair_money, repair_date, repair_code


def eval_cases(cases: list[tuple[str, str, str]], fn) -> Tuple[int, int]:
    ok = 0
    total = 0
    for before, after, typ in cases:
        total += 1
        maps = load_maps("config/confusion_maps.v1.json")
        if typ == "money":
            rep, _ = repair_money(before, maps.get("money", {}))
        elif typ == "date":
            rep, _ = repair_date(before, maps.get("date", {}))
        else:
            rep, _ = repair_code(before, maps.get("code", {}))
        if rep == after:
            ok += 1
    return ok, total


def main() -> None:
    # Minimal fixtures inline for speed; can be moved to samples later
    money_cases = [
        ("$1O.5O", "10.50", "money"),
        ("(12.34)", "-12.34", "money"),
        ("2000", "2000.00", "money")
    ]
    date_cases = [
        ("O1/15/2O24", "2024-01-15", "date"),
        ("2024-02-29", "2024-02-29", "date")
    ]
    code_cases = [
        ("d0120", "D0120", "code"),
        ("99213", "99213", "code")
    ]
    total_ok = 0
    total = 0
    for cases, typ in [(money_cases, "money"), (date_cases, "date"), (code_cases, "code")]:
        ok, tot = eval_cases(cases, typ)
        total_ok += ok; total += tot
        print(f"{typ}: {ok}/{tot}")
    acc = 0.0 if total == 0 else total_ok / total
    print(f"typed_cell_accuracy={acc:.2f}")
    if acc < 0.80:
        print("FAIL: typed_cell_accuracy < 0.80")
        raise SystemExit(1)
    print("PASS")


if __name__ == "__main__":
    main()

