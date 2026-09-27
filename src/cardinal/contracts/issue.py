from pydantic import BaseModel


class Issue(BaseModel):
    repository: str
    number: int
    title: str
    body: str
