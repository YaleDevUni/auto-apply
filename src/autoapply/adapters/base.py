"""플랫폼 어댑터 공통 인터페이스.

새 플랫폼(잡코리아, 링크드인 등)을 추가하려면 Adapter를 상속해 fetch()만 구현하고
adapters/__init__.py의 REGISTRY에 등록하면 된다. 나머지 파이프라인은 그대로 동작한다.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Iterator

from .. import normalize
from ..http import Fetcher


@dataclass
class JobPosting:
    """모든 플랫폼이 이 형태로 정규화된다."""

    platform: str
    platform_job_id: str
    url: str
    company: str
    title: str
    category: str | None = None
    location: str | None = None
    employment_type: str | None = None
    experience_req: str | None = None
    education_req: str | None = None
    salary: str | None = None
    deadline: str | None = None  # ISO8601 또는 None(상시)
    posted_at: str | None = None
    description: str = ""
    image_url: str | None = None
    image_path: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    def searchable_text(self) -> str:
        """필터가 훑는 텍스트 뭉치."""
        parts = [
            self.title,
            self.category,
            self.company,
            self.location,
            self.employment_type,
            self.experience_req,
            self.education_req,
            self.description,
        ]
        return " ".join(p for p in parts if p).lower()

    def content_hash(self) -> str:
        blob = f"{self.title}|{self.company}|{self.deadline}|{self.description[:2000]}"
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]

    def to_db(self) -> dict[str, Any]:
        return {
            "platform": self.platform,
            "platform_job_id": self.platform_job_id,
            "url": self.url,
            "company": self.company,
            "company_norm": normalize.company(self.company),
            "title": self.title,
            "title_norm": normalize.title(self.title),
            "category": self.category,
            "location": self.location,
            "employment_type": self.employment_type,
            "experience_req": self.experience_req,
            "education_req": self.education_req,
            "salary": self.salary,
            "deadline": self.deadline,
            "posted_at": self.posted_at,
            "description": self.description,
            "image_url": self.image_url,
            "image_path": self.image_path,
            "raw": self.raw,
            "content_hash": self.content_hash(),
            "canonical_key": normalize.canonical_key(self.company, self.title),
        }


class Adapter:
    name: str = "base"
    label: str = "base"

    def __init__(self, fetcher: Fetcher, cfg: dict[str, Any]):
        self.fetcher = fetcher
        self.cfg = cfg

    def fetch(self) -> Iterator[JobPosting]:
        raise NotImplementedError
