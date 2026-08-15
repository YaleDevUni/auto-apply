"""원티드 어댑터 — 공개 JSON API. 로그인/브라우저 불필요.

목록: /api/chaos/navigation/v1/results
상세: /api/chaos/jobs/v1/{id}/details   (본문 전문 포함)
"""

from __future__ import annotations

import logging
from typing import Any, Iterator

from .base import Adapter, JobPosting

log = logging.getLogger(__name__)

LIST_URL = "https://www.wanted.co.kr/api/chaos/navigation/v1/results"
DETAIL_URL = "https://www.wanted.co.kr/api/chaos/jobs/v1/{job_id}/details"
PAGE_SIZE = 100


class WantedAdapter(Adapter):
    name = "wanted"
    label = "원티드"

    def fetch(self) -> Iterator[JobPosting]:
        s = self.cfg["scrape"]
        seen: set[str] = set()

        for group in s["wanted_job_groups"]:
            for page in range(s["wanted_pages"]):
                params = {
                    "job_group_id": group["id"],
                    "country": "kr",
                    "job_sort": "job.latest_order",
                    "years": s.get("wanted_years", 0),
                    "locations": "all",
                    "limit": PAGE_SIZE,
                    "offset": page * PAGE_SIZE,
                }
                data = self.fetcher.get_json(LIST_URL, params=params)
                items = (data or {}).get("data") or []
                if not items:
                    break

                for item in items:
                    job_id = str(item.get("id"))
                    if job_id in seen:
                        continue
                    seen.add(job_id)
                    yield self._parse_list_item(item, group["name"])

                if len(items) < PAGE_SIZE:
                    break

    def _parse_list_item(self, item: dict[str, Any], group_name: str) -> JobPosting:
        company = (item.get("company") or {}).get("name", "")
        addr = item.get("address") or {}
        location = " ".join(
            x for x in (addr.get("location"), addr.get("district")) if x
        ) or addr.get("country")

        return JobPosting(
            platform=self.name,
            platform_job_id=str(item.get("id")),
            url=f"https://www.wanted.co.kr/wd/{item.get('id')}",
            company=company,
            title=item.get("position", ""),
            category=group_name,
            location=location,
            deadline=item.get("due_time"),
            image_url=(item.get("title_img") or {}).get("origin"),
            salary=item.get("reward_total"),
            raw=item,
        )

    def enrich(self, job: JobPosting) -> JobPosting:
        """상세 API로 본문 전문을 채운다. 하드컷 통과 건에만 호출한다."""
        data = self.fetcher.get_json(DETAIL_URL.format(job_id=job.platform_job_id))
        if not data:
            return job

        jd = data.get("job") or {}
        detail = jd.get("detail") or {}

        sections = [
            ("소개", detail.get("intro")),
            ("주요업무", detail.get("main_tasks")),
            ("자격요건", detail.get("requirements")),
            ("우대사항", detail.get("preferred_points")),
            ("혜택", detail.get("benefits")),
            ("채용절차", detail.get("hire_rounds")),
        ]
        job.description = "\n\n".join(
            f"[{name}]\n{body.strip()}" for name, body in sections if body and body.strip()
        )

        skills = jd.get("skill_tags") or []
        if skills:
            names = [s.get("title") for s in skills if isinstance(s, dict) and s.get("title")]
            if names:
                job.description += "\n\n[기술스택]\n" + ", ".join(names)

        job.deadline = jd.get("due_time") or job.deadline
        addr = jd.get("address") or {}
        if addr.get("full_location"):
            job.location = addr["full_location"]

        job.raw = {**job.raw, "detail": data}
        return job
