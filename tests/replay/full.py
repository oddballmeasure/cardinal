"""Replay candidate for the completed issue."""

import argparse
from datetime import date
from pathlib import Path

from ledger.formatters import render_csv, render_json
from ledger.query import filter_merchant, filter_since
from ledger.records import load_transactions


def parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid date: {value}") from None


def main() -> None:
    parser = argparse.ArgumentParser(description="Print transactions")
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--since", type=parse_date)
    parser.add_argument("--merchant")
    parser.add_argument("--format", choices=["json", "csv"], default="json")
    args = parser.parse_args()
    rows = filter_since(filter_merchant(load_transactions(args.input), args.merchant), args.since)
    if args.format == "csv":
        print(render_csv(rows), end="")
    else:
        print(render_json(rows))


if __name__ == "__main__":
    main()
