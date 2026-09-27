"""Unused prototype CSV output; the CLI does not import this module."""


def old_export(rows: list[dict]) -> str:
    return "\n".join(",".join(str(value) for value in row.values()) for row in rows)
