from __future__ import annotations

import math
import re
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")

SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{1,62}[a-z0-9])$")
CAPABILITY_KEY_RE = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$")


class PageMeta(BaseModel):
    page: int
    page_size: int
    total: int
    pages: int


class Page(BaseModel, Generic[T]):
    items: list[T]
    meta: PageMeta

    @classmethod
    def build(cls, items: list[T], *, page: int, page_size: int, total: int) -> Page[T]:
        return cls(
            items=items,
            meta=PageMeta(
                page=page,
                page_size=page_size,
                total=total,
                pages=max(1, math.ceil(total / page_size)) if page_size else 1,
            ),
        )


class Message(BaseModel):
    detail: str


class HealthResponse(BaseModel):
    status: str
    version: str
    environment: str
    checks: dict[str, Any] = Field(default_factory=dict)
