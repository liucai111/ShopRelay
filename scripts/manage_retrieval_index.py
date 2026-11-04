"""Explicit versioned policy-index management; never runs at API startup."""
import argparse
from pathlib import Path
from app.m2.retrieval import IndexRegistry, ROOT

def main():
    p=argparse.ArgumentParser(); sub=p.add_subparsers(dest="action",required=True)
    b=sub.add_parser("build"); b.add_argument("--policies",type=Path,default=ROOT/"knowledge/policies.json")
    r=sub.add_parser("rollback"); r.add_argument("fingerprint")
    args=p.parse_args(); registry=IndexRegistry()
    if args.action=="build":
        fp=registry.build(args.policies); print(f"activated fingerprint={fp}")
    else:
        registry.activate(args.fingerprint); print(f"activated fingerprint={args.fingerprint}")

if __name__=="__main__": main()
