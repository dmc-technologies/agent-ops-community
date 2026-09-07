"""Immutable machine-wide skill choices, independent of deployment targets.

Source identifiers resolve through the existing source registry. This document
records exact commits and selected repository paths; it neither fetches sources
nor authorizes installation, and it contains no source URLs or credentials.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import PurePosixPath, PureWindowsPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator


class SharedSourceBinding(BaseModel):
    """Selected skill directories at one registered source's exact commit."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_id: str
    commit: str
    skill_paths: tuple[str, ...]

    @field_validator("source_id")
    @classmethod
    def validate_source_id(cls, value: str) -> str:
        if not value or value.strip() != value or any(ord(char) < 32 for char in value):
            raise ValueError("source_id must be nonempty without surrounding whitespace")
        return value

    @field_validator("commit")
    @classmethod
    def validate_commit(cls, value: str) -> str:
        if re.fullmatch(r"[0-9a-f]{40}", value) is None:
            raise ValueError("commit must be an exact 40-character lowercase hexadecimal commit")
        return value

    @field_validator("skill_paths")
    @classmethod
    def validate_skill_paths(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if not values:
            raise ValueError("skill_paths must contain at least one selected skill")
        for value in values:
            path = PurePosixPath(value)
            if (
                not value
                or value == "."
                or path.is_absolute()
                or PureWindowsPath(value).drive
                or "\\" in value
                or ".." in path.parts
                or str(path) != value
                or any(ord(char) < 32 for char in value)
            ):
                raise ValueError("skill path must be a normalized repository-relative path")
        if len(set(values)) != len(values):
            raise ValueError("skill paths must be unique within each source")
        return tuple(sorted(values))


class SharedSelection(BaseModel):
    """Persistable selection shared by all participating framework targets."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1] = 1
    sources: tuple[SharedSourceBinding, ...]

    @field_validator("schema_version", mode="before")
    @classmethod
    def validate_schema_version(cls, value: object) -> object:
        if type(value) is not int or value != 1:
            raise ValueError("schema_version must be the integer 1")
        return value

    @field_validator("sources")
    @classmethod
    def validate_sources(
        cls, values: tuple[SharedSourceBinding, ...]
    ) -> tuple[SharedSourceBinding, ...]:
        if not values:
            raise ValueError("selection must contain at least one source")
        identifiers = [value.source_id for value in values]
        if len(set(identifiers)) != len(identifiers):
            raise ValueError("selection source identifiers must be unique")
        return tuple(sorted(values, key=lambda value: value.source_id))

    @property
    def fingerprint(self) -> str:
        """SHA-256 fingerprint of canonical versioned content, excluding presentation."""
        content = json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(content.encode("utf-8")).hexdigest()
