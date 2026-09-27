"""Calculate the total amount of transaction records for reporting."""

from decimal import Decimal


def transaction_total(rows: list[dict]) -> Decimal:
    return sum((Decimal(str(row["amount"])) for row in rows), Decimal(0))
