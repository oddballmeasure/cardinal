"""Replay candidate for the isolated date-filter ticket."""

import argparse
from datetime import date
from pathlib import Path

from ledger.formatters import render_json
from ledger.query import filter_merchant, filter_since
from ledger.records import load_transactions


def parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid date: {value}") from None


def main() -> None:
    parser = argparse.ArgumentParser(description="Print transactions as JSON")
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--since", type=parse_date)
    parser.add_argument("--merchant")
    args = parser.parse_args()
    rows = filter_merchant(load_transactions(args.input), args.merchant)
    print(render_json(filter_since(rows, args.since)))


if __name__ == "__main__":
    main()
