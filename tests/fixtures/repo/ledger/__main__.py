"""Command line entry point for transaction browsing."""

import argparse
from pathlib import Path

from ledger.formatters import render_json
from ledger.query import filter_merchant
from ledger.records import load_transactions


def main() -> None:
    parser = argparse.ArgumentParser(description="Print transactions as JSON")
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--merchant")
    args = parser.parse_args()
    rows = load_transactions(args.input)
    print(render_json(filter_merchant(rows, args.merchant)))


if __name__ == "__main__":
    main()
