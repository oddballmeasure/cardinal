from typing import Literal

from pydantic import BaseModel, Field


class ProfileFile(BaseModel):
    path: str
    summary: str = Field(min_length=1)
    capabilities: list[str] = Field(min_length=1)
    status: Literal["active", "legacy", "support"]


class ProfileChunk(BaseModel):
    files: list[ProfileFile] = Field(min_length=1)


class RepoProfile(BaseModel):
    repository: str
    revision: str
    content_hash: str
    file_hashes: dict[str, str]
    files: list[ProfileFile] = Field(min_length=1)
