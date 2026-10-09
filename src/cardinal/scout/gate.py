"""Autonomy, decided by code per category from the measured track record. A category goes ready
on its own only while every bar holds, so a falling rate puts it back to propose-only by itself."""

from cardinal.config.models import Scout
from cardinal.scout.outcomes import WINDOW_DAYS


def decide(settings: Scout, record: dict[str, dict], category: str, repro_ok: bool | None) -> tuple[str, str]:
    """("auto" or "proposed", why). A bug also needs a test that failed on the base branch."""
    if settings.autonomy == "propose":
        return "proposed", "autonomy is propose"
    counts = record.get(category) or {"decided": 0, "approval": 0.0, "done_rate": None}
    problems = []
    if counts["decided"] < settings.auto_min_decided:
        problems.append(f"{counts['decided']} decided {category} proposals in {WINDOW_DAYS} days, "
                        f"under {settings.auto_min_decided}")
    if counts["approval"] < settings.auto_min_approval:
        problems.append(f"approval {counts['approval']:.0%} is under {settings.auto_min_approval:.0%}")
    if (counts["done_rate"] or 0.0) < settings.auto_min_done:
        done = "no landed issues" if counts["done_rate"] is None else f"done-rate {counts['done_rate']:.0%}"
        problems.append(f"{done}, under {settings.auto_min_done:.0%}")
    if category == "bug" and not repro_ok:
        problems.append("no test that fails on the base branch")
    if problems:
        return "proposed", "not auto: " + "; ".join(problems)
    return "auto", (f"{counts['decided']} decided {category} proposals in {WINDOW_DAYS} days, {counts['approval']:.0%} "
                    f"approved, {counts['done_rate']:.0%} of landed ones done"
                    + (", and a test that fails on the base branch" if category == "bug" else ""))
