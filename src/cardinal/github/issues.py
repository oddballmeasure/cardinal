"""Issues and their labels. State labels form one axis: setting one removes the others."""

from cardinal.config.models import Labels
from cardinal.contracts.issue import Issue
from cardinal.github.gh import gh, gh_json

LABEL_COLORS = {"ready": "0e8a16", "working": "fbca04", "done": "5319e7", "error": "b60205", "needs_human": "d93f0b"}


def view(repo: str, number: int) -> tuple[Issue, list[str], str]:
    data = gh_json("issue", "view", str(number), "-R", repo, "--json", "number,title,body,labels,state")
    issue = Issue(repository=repo, number=data["number"], title=data["title"], body=data["body"] or "")
    return issue, [label["name"] for label in data["labels"]], data["state"]


def ready(repo: str, label: str) -> list[int]:
    data = gh_json("issue", "list", "-R", repo, "--state", "open", "--label", label,
                   "--limit", "100", "--json", "number")
    return sorted(item["number"] for item in data)


def with_label(repo: str, label: str, state: str = "open") -> list[int]:
    data = gh_json("issue", "list", "-R", repo, "--state", state, "--label", label,
                   "--limit", "100", "--json", "number")
    return sorted(item["number"] for item in data)


def set_state(repo: str, number: int, labels: Labels, target: str) -> None:
    if target not in labels.state_axis():
        raise ValueError(f"{target} is not a state label")
    _, current, _ = view(repo, number)
    stale = [name for name in labels.state_axis() if name in current and name != target]
    args = ["issue", "edit", str(number), "-R", repo]
    if target not in current:
        args += ["--add-label", target]
    if stale:
        args += ["--remove-label", ",".join(stale)]
    if len(args) > 5:
        gh(*args)


def ensure_labels(repo: str, labels: Labels) -> None:
    for key, color in LABEL_COLORS.items():
        gh("label", "create", getattr(labels, key), "-R", repo, "--color", color, "--force")


def comment(repo: str, number: int, body: str) -> None:
    gh("issue", "comment", str(number), "-R", repo, "--body", body)
