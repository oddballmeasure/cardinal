"""Select active transactions by merchant and inclusive date."""

from datetime import date


def filter_merchant(rows: list[dict], merchant: str | None) -> list[dict]:
    if merchant is None:
        return rows
    return [row for row in rows if row["merchant"] == merchant]


def filter_since(rows: list[dict], since: date | None) -> list[dict]:
    if since is None:
        return rows
    return [row for row in rows if date.fromisoformat(row["date"]) >= since]
