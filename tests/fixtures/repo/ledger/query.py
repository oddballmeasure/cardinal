"""Active transaction selection by merchant."""


def filter_merchant(rows: list[dict], merchant: str | None) -> list[dict]:
    if merchant is None:
        return rows
    return [row for row in rows if row["merchant"] == merchant]
