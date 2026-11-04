from __future__ import annotations

import argparse
import json
from pathlib import Path

from .baseline import SingleAgentBaseline
from .data import ROOT, ingest, seed
from .evaluate import evaluate


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("seed").add_argument("--db", type=Path, default=ROOT / "data/shoprelay.sqlite3")
    sub.add_parser("ingest").add_argument("--index", type=Path, default=ROOT / "data/index.json")
    p = sub.add_parser("ask")
    p.add_argument("query")
    p.add_argument("--at", default="2026-10-18T00:00:00Z")
    p.add_argument("--index", type=Path, default=ROOT / "data/index.json")
    p = sub.add_parser("evaluate")
    p.add_argument("--index", type=Path, default=ROOT / "data/index.json")
    p.add_argument("--dataset", type=Path, default=ROOT / "eval/dataset/policy-initial.json")
    p.add_argument("--raw-dir", type=Path, default=ROOT / "eval/raw")
    p.add_argument("--llm", action="store_true")
    args = parser.parse_args()
    if args.command == "seed":
        result = seed(args.db)
    elif args.command == "ingest":
        result = ingest(args.index)
    elif args.command == "ask":
        result = SingleAgentBaseline(args.index).answer(args.query, args.at)
    else:
        result = evaluate(args.index, args.dataset, args.raw_dir, llm=args.llm)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
