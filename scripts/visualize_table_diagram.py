#!/usr/bin/env python3
"""
Create a visual diagram of the table structure and column mappings.
Shows how the raw OCR table gets mapped to semantic roles.

Hardened with null-safe utilities to handle missing sidecars gracefully.
"""

import json
import sys
from pathlib import Path
from typing import Dict, Any, Optional

# Import null-safe utilities for graceful degradation
try:
    from null_safe_utils import safe_load_json, safe_get, safe_get_dict, safe_max, safe_invert_dict
except ImportError:
    # Fallback if running from different directory
    sys.path.insert(0, str(Path(__file__).parent))
    from null_safe_utils import safe_load_json, safe_get, safe_get_dict, safe_max, safe_invert_dict


def load_json(path: Path) -> Optional[Dict[str, Any]]:
    """Load JSON file with null-safe fallback."""
    return safe_load_json(path, {})


def create_table_diagram(outdir: Path):
    """Create ASCII art diagram of table structure and mappings."""

    # Find files
    eob_files = [f for f in outdir.glob("*.json")
                 if not any(x in f.name for x in ['.ccert.', '.proof.', '.evidence.',
                                                   '.budget.', '.stability.', '.summary_box.',
                                                   '.conservation.', '.coherence.', '.sha256'])]

    if not eob_files:
        print("No EOB files found")
        return

    eob_path = eob_files[0]
    stem = eob_path.stem

    # Load data
    eob = load_json(eob_path)
    assign_proof_path = outdir / f"{stem}.assign.proof.v1.json"

    if not assign_proof_path.exists():
        print("No assign proof found")
        return

    assign_proof = load_json(assign_proof_path)

    # Get table info
    services = eob.get("services", [])
    if not services:
        print("No services found")
        return

    tables = assign_proof.get("tables", [])
    if not tables:
        print("No tables found")
        return

    table = tables[0]
    column_map = table.get("column_map", {})

    print("\n" + "═" * 110)
    print("  📊 TABLE STRUCTURE DIAGRAM")
    print("═" * 110)

    # Determine number of columns (null-safe: handles empty column_map)
    max_col = safe_max(column_map.values(), 5) if column_map else 5

    # Create header showing column indices
    print("\n  Raw OCR Table:")
    print("  " + "┌" + "─" * 106 + "┐")

    col_headers = "  │ " + " │ ".join(f"Col {i:^8}" for i in range(max_col + 1)) + " │"
    print(col_headers)
    print("  " + "├" + "─" * 106 + "┤")

    # Show what's in each column (using first service as example)
    if services:
        svc = services[0]

        # Build reverse map (column -> role) - null-safe: handles None/empty column_map
        col_to_role = safe_invert_dict(column_map, {})

        # Determine what data is in each column
        role_to_value = {
            "procedure_code": svc.get("procedure_code", ""),
            "service_date": svc.get("service_date", ""),
            "charged_amount": svc.get("charged_amount", ""),
            "allowed_amount": svc.get("allowed_amount", ""),
            "insurance_paid": svc.get("insurance_paid", ""),
            "patient_responsibility": svc.get("patient_responsibility", ""),
        }

        # Row 0 (header row - synthetic data)
        header_row = []
        for i in range(max_col + 1):
            role = col_to_role.get(i, "")
            if role:
                header_map = {
                    "procedure_code": "Proc Code",
                    "service_date": "Date",
                    "charged_amount": "Charged",
                    "allowed_amount": "Allowed",
                    "insurance_paid": "Ins Paid",
                    "patient_responsibility": "Patient",
                }
                header_row.append(header_map.get(role, role[:10]))
            else:
                header_row.append("?")

        print("  │ " + " │ ".join(f"{h:^10}" for h in header_row) + " │")
        print("  " + "├" + "─" * 106 + "┤")

        # Row 1 (data row)
        data_row = []
        for i in range(max_col + 1):
            role = col_to_role.get(i, "")
            if role:
                value = str(role_to_value.get(role, ""))[:10]
                data_row.append(value)
            else:
                data_row.append("?")

        print("  │ " + " │ ".join(f"{d:^10}" for d in data_row) + " │")

    print("  " + "└" + "─" * 106 + "┘")

    # Show the mapping
    print("\n  Column → Role Mapping:")
    print("  " + "─" * 106)

    for col_idx in sorted(column_map.keys(), key=lambda k: column_map[k]):
        role = col_idx
        col_num = column_map[col_idx]

        # Create visual arrow
        arrow = "  " + " " * (col_num * 12 + 4) + "▲"
        label = "  " + " " * (col_num * 12 + 2) + f"│ {role}"

        print(arrow)
        print(label)

    print("\n  " + "─" * 106)

    # Show ILP alternatives if available (null-safe handling)
    ilp = safe_get_dict(table, "ilp", None)
    if ilp:
        print("\n" + "═" * 110)
        print("  🔍 ILP ALTERNATIVE MAPPINGS")
        print("═" * 110)

        for alt_type in ["best", "runner_up"]:
            alt = safe_get_dict(ilp, alt_type, None)
            if not alt:
                continue

            alt_map = safe_get_dict(alt, "column_map", {})
            certified = safe_get(alt, "certified_rows_sample", 0)

            print(f"\n  {alt_type.upper()} (Certified: {certified}):")

            # Show which columns differ from chosen
            different = []
            for role, chosen_col in column_map.items():
                if role in alt_map and alt_map[role] != chosen_col:
                    different.append(f"{role}: col{chosen_col} → col{alt_map[role]}")

            if different:
                for diff in different:
                    print(f"    • {diff}")
            else:
                print("    • Identical to chosen mapping")

    # Show certification status
    print("\n" + "═" * 110)
    print("  ⚖️  CERTIFICATION STATUS")
    print("═" * 110)

    for i, svc in enumerate(services, 1):
        locked = "🔒 LOCKED" if svc.get("_row_locked") else "🔓 UNLOCKED"
        print(f"\n  Row {i}: {locked}")

        # Show the inequality check visually
        try:
            charged = float(svc.get("charged_amount", 0))
            allowed = float(svc.get("allowed_amount", 0))
            paid = float(svc.get("insurance_paid", 0))
            patient = float(svc.get("patient_responsibility", 0))

            print(f"\n    Inequality Checks:")
            print(f"      Allowed ≤ Charged:  ${allowed:.2f} ≤ ${charged:.2f}  {'✓' if allowed <= charged else '✗'}")
            print(f"      Paid ≤ Allowed:     ${paid:.2f} ≤ ${allowed:.2f}  {'✓' if paid <= allowed else '✗'}")
            print(f"      Patient ≤ Allowed:  ${patient:.2f} ≤ ${allowed:.2f}  {'✓' if patient <= allowed else '✗'}")

            # Closure check
            closure_sum = paid + patient
            print(f"\n    Closure Check:")
            print(f"      Paid + Patient = Allowed:  ${paid:.2f} + ${patient:.2f} = ${closure_sum:.2f} vs ${allowed:.2f}  {'✓' if abs(closure_sum - allowed) < 0.01 else '✗'}")
        except:
            pass

    print("\n" + "═" * 110 + "\n")


def main():
    if len(sys.argv) < 2:
        print("Usage: visualize_table_diagram.py <output_dir>")
        sys.exit(1)

    outdir = Path(sys.argv[1])
    if not outdir.exists():
        print(f"Error: Directory {outdir} does not exist")
        sys.exit(1)

    create_table_diagram(outdir)


if __name__ == "__main__":
    main()
