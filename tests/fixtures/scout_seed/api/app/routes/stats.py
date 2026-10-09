"""Summary statistics over stored notes."""

from collections import Counter

from fastapi import APIRouter

from app.storage import list_entries

router = APIRouter()


@router.get("/stats/tags")
def tag_stats() -> dict:
    notes = list_entries("notes")
    total = sum(len(note["tags"]) for note in notes)
    return {"notes": len(notes), "tags": total, "average_tags_per_note": round(total / len(notes), 2)}


@router.get("/stats/top-tag")
def top_tag() -> dict:
    counts = Counter(tag for note in list_entries("notes") for tag in note["tags"])
    if not counts:
        return {"tag": None, "uses": 0}
    tag, uses = counts.most_common(1)[0]
    return {"tag": tag, "uses": uses}
