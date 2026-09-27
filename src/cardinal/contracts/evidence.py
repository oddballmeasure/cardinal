from pydantic import BaseModel


class CommandEvidence(BaseModel):
    """What a command did. Output is bounded; `timed_out` keeps a kill apart from a failing exit."""

    command: list[str]
    exit_code: int
    timed_out: bool = False
    stdout_tail: str
    stderr_tail: str

    @property
    def passed(self) -> bool:
        return self.exit_code == 0 and not self.timed_out
