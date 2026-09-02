#!/usr/bin/env python3
from __future__ import annotations

import argparse, json, sys
from jsonschema import Draft202012Validator

def main() -> None:
    p=argparse.ArgumentParser()
    p.add_argument("--schema", required=True)
    p.add_argument("--in", dest="inp", required=True)
    args=p.parse_args()
    schema=json.load(open(args.schema,"r",encoding="utf-8"))
    validator=Draft202012Validator(schema)
    n=0
    with open(args.inp,"r",encoding="utf-8") as fh:
        for i,line in enumerate(fh,1):
            line=line.strip()
            if not line: continue
            try:
                obj=json.loads(line)
            except Exception as e:
                print(f"[invalid] line {i}: not JSON ({e})", file=sys.stderr); sys.exit(1)
            errs=list(validator.iter_errors(obj))
            if errs:
                print(f"[invalid] line {i}: {errs[0].message}", file=sys.stderr); sys.exit(1)
            n+=1
    print(f"[ok] {n} records")

if __name__=="__main__":
    main()

