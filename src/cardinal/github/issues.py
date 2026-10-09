"""Issues and their labels. State labels form one axis: setting one removes the others."""

from cardinal.config.models import Labels
from cardinal.contracts.issue import Issue
from cardinal.github.gh import gh, gh_json

# Colors match oddballmeasure/cardinal_test_repo so every repository shows the same state axis.
LABEL_STYLES = {
    "ready": ("0e8a16", "Ready for Cardinal"),
    "working": ("fbca04", "Cardinal is working this issue"),
    "done": ("5319e7", "Cardinal verified and merged"),
    "error": ("b60205", "Cardinal failed; see the issue comments"),
    "needs_human": ("d93f0b", "Cardinal needs a human decision"),
    "investigate": ("c5def5", "Cardinal needs research before coding"),
    "proposed": ("bfdadc", "Proposed by Cardinal's scout; swap for ready to approve, close to reject"),
}


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


def titles(repo: str) -> list[dict]:
    """The newest issues, open and closed: number, title and state."""
    return gh_json("issue", "list", "-R", repo, "--state", "all", "--limit", "200", "--json", "number,title,state")


def last_comment(repo: str, number: int) -> str:
    comments = gh_json("issue", "view", str(number), "-R", repo, "--json", "comments")["comments"]
    return comments[-1]["body"] if comments else ""


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
    for key, (color, description) in LABEL_STYLES.items():
        gh("label", "create", getattr(labels, key), "-R", repo, "--color", color, "--description", description, "--force")


def create(repo: str, title: str, body: str, labels: tuple[str, ...] = ()) -> int:
    url = gh("issue", "create", "-R", repo, "--title", title, "--body", body,
             *(arg for label in labels for arg in ("--label", label))).strip().splitlines()[-1]
    return int(url.rstrip("/").rsplit("/", 1)[1])


def comment(repo: str, number: int, body: str) -> None:
    gh("issue", "comment", str(number), "-R", repo, "--body", body)
