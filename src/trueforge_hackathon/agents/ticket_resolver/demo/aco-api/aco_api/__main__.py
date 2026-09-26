"""Command line for the ACO API. Prints JSON.

  python -m aco_api build
  python -m aco_api patient 1EG4TE5MK72
  python -m aco_api claims 1EG4TE5MK72 [--year 2025]
  python -m aco_api claims-total 1EG4TE5MK72 [--year 2025]
  python -m aco_api attribution 1234567893
  python -m aco_api attributed-count
"""
from __future__ import annotations

import argparse
import json

from aco_api import db, service


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="aco_api")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("build")
    sub.add_parser("patient").add_argument("mbi")
    for name in ("claims", "claims-total"):
        p = sub.add_parser(name)
        p.add_argument("mbi")
        p.add_argument("--year", type=int)
    sub.add_parser("attribution").add_argument("npi")
    sub.add_parser("attributed-count")
    args = parser.parse_args(argv)

    if args.command == "build":
        out = {"built": str(db.build())}
    else:
        conn = db.connect()
        if args.command == "patient":
            out = service.find_patient(conn, args.mbi)
        elif args.command == "claims":
            out = service.patient_claims(conn, args.mbi, args.year)
        elif args.command == "claims-total":
            total = service.claim_total(conn, args.mbi, args.year)
            out = None if total is None else {"mbi": args.mbi, "year": args.year, "total_paid": str(total)}
        elif args.command == "attribution":
            out = service.attribution_list(conn, args.npi)
        else:
            out = {"attributed_beneficiaries": service.attributed_count(conn)}
    print(json.dumps(out, indent=2))
    if out is None:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
