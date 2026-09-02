#!/usr/bin/env python3
"""Stage D: EOB → Chunks (deterministic)."""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
import unicodedata as ud

def nfc(s: str | None) -> str:
    return ud.normalize("NFC", s or "")

def canon(obj: dict) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)

def sha(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()

def quarter(date_iso: str | None) -> str | None:
    if not date_iso: return None
    y,m,_ = date_iso.split("-")
    return f"{y}Q{(int(m)-1)//3 + 1}"

def add_ids(rec: dict) -> dict:
    tmp = {k:v for k,v in rec.items() if k not in ("chunk_uid","chunk_sha256","chunk_sha256_scope")}
    payload = canon(tmp); h = sha(payload)
    rec["chunk_uid"] = h; rec["chunk_sha256"] = h
    rec["chunk_sha256_scope"] = "canonical JSON with {chunk_uid,chunk_sha256,*_scope} omitted"
    return rec

def text_header(h: dict) -> str:
    parts = [
        f"Payer: {h.get('payer_name','')} ({h.get('payer_id','')})",
        f"Member: {h.get('member_id','')} / {h.get('subscriber_name','')}",
        f"Provider NPI: {h.get('provider_npi','')}",
        f"Office: {h.get('office_id','')}"
    ]
    return nfc("\n".join([p for p in parts if p.strip()]))[:8000]

def text_claim(c: dict) -> str:
    parts = [
        f"Claim {c.get('claim_id','')} on {c.get('claim_date','')} — {c.get('claim_status','')}",
        f"Totals: billed ${c.get('total_billed',0):.2f} | allowed ${c.get('total_allowed',0):.2f} | paid ${c.get('total_paid',0):.2f} | patient ${c.get('total_patient',0):.2f}"
    ]
    return nfc("\n".join([p for p in parts if p.strip()]))[:8000]

def text_line(sl: dict) -> str:
    parts = [
        f"{sl.get('proc_system','ADA')} {sl.get('proc_cd','')}: {sl.get('description','')}",
        f"DOS {sl.get('dos','')} | billed ${sl.get('billed',0):.2f}, allowed ${sl.get('allowed',0):.2f}, paid ${sl.get('paid',0):.2f}, patient ${sl.get('patient_responsibility',0):.2f}"
    ]
    return nfc("\n".join([p for p in parts if p.strip()]))[:8000]

def emit_chunks(eob_path: Path, out_path: Path) -> tuple[int,str,str]:
    eob = json.loads(eob_path.read_text())
    src_sha = eob.get("source_doc_sha256") or hashlib.sha256(eob_path.read_bytes()).hexdigest()
    line_count = 0
    with out_path.open("w", encoding="utf-8") as w:
        hdr = {
            "type":"header",
            "payer_name": eob.get("claim",{}).get("payer_name"),
            "member_id": eob.get("patient",{}).get("subscriber_id"),
            "provider_npi": eob.get("provider",{}).get("npi"),
            "office_id": eob.get("provider",{}).get("location"),
            "source_doc_sha256": src_sha,
            "page_idx": sorted({0})
        }
        hdr["text"] = text_header(hdr); add_ids(hdr); w.write(canon(hdr)+"\n"); line_count+=1

        claim = eob.get("claim", {})
        c = {
            "type":"claim",
            "payer_name": eob.get("claim",{}).get("payer_name"),
            "member_id": eob.get("patient",{}).get("subscriber_id"),
            "provider_npi": eob.get("provider",{}).get("npi"),
            "office_id": eob.get("provider",{}).get("location"),
            "claim_id": claim.get("claim_number"),
            "claim_date": claim.get("claim_processed_date") or claim.get("claim_received_date"),
            "claim_status": claim.get("status"),
            "claim_quarter": quarter(claim.get("claim_processed_date") or claim.get("claim_received_date")),
            "total_billed": float(eob.get("totals",{}).get("total_charged",0) or 0),
            "total_allowed": float(eob.get("totals",{}).get("total_allowed",0) or 0),
            "total_paid": float(eob.get("totals",{}).get("total_paid",0) or 0),
            "total_patient": float(eob.get("totals",{}).get("total_patient_responsibility",0) or 0),
            "source_doc_sha256": src_sha,
            "page_idx": [0],
            "eob_json_pointer": "/claim"
        }
        c["text"] = text_claim(c); add_ids(c); w.write(canon(c)+"\n"); line_count+=1

        for i,sl in enumerate(eob.get("services", [])):
            code = (sl.get("procedure_code") or "").upper()
            system = "ADA" if code.startswith("D") else ("CPT" if code[:1].isdigit() else None)
            rec = {
                "type":"service_line",
                "claim_id": claim.get("claim_number"),
                "payer_name": claim.get("payer_name"),
                "member_id": eob.get("patient",{}).get("subscriber_id"),
                "provider_npi": eob.get("provider",{}).get("npi"),
                "office_id": eob.get("provider",{}).get("location"),
                "proc_system": system,
                "proc_cd": code,
                "description": sl.get("remark_description"),
                "dos": sl.get("service_date"),
                "decision": claim.get("status"),
                "billed": float(sl.get("charged_amount",0) or 0),
                "allowed": float(sl.get("allowed_amount",0) or 0),
                "paid": float(sl.get("insurance_paid",0) or 0),
                "patient_responsibility": float(sl.get("patient_responsibility",0) or 0),
                "source_doc_sha256": src_sha,
                "page_idx": [0],
                "line_idx": i,
                "eob_json_pointer": f"/services/{i}"
            }
            rec["text"] = text_line(rec); add_ids(rec); w.write(canon(rec)+"\n"); line_count+=1

    data = out_path.read_bytes()
    import zlib
    crc = f"{zlib.crc32(data) & 0xffffffff:08x}"
    return line_count, crc, sha(data.decode("utf-8"))

def main() -> None:
    out_dir = Path("out/chunks"); out_dir.mkdir(parents=True, exist_ok=True)
    in_dir = Path("out/eob")
    for p in sorted(in_dir.glob("*.json")):
        out = out_dir / f"{p.stem}.jsonl"
        line_count, crc, digest = emit_chunks(p, out)
        print(json.dumps({"file": str(out), "line_count": line_count, "crc32": crc, "sha256": digest}))

if __name__ == "__main__":
    main()

