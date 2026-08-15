"""자소설닷컴 어댑터 — 내부 JSON API. 로그인 없이 진행중 공고 전체를 한 번에 받는다.

목록: GET /api/v1/employment_companies?all=true   → 진행중 공고 전량 (약 300건)
상세: GET /api/v1/employment_companies/{id}        → content(HTML), 이미지형 공고 다수

자소설 공고는 상당수가 '이미지 한 장'이다. 완전 자동화 관점에서 이건 치명적이다 —
텍스트가 없으면 자소서 문항도, 지원 요건도 읽어낼 수 없다. 그래서 이미지형은
applicability에서 IMAGE_ONLY로 막고, 에이전트가 비전으로 읽어야 할 대상으로 넘긴다.

한계: 자소서 문항 원문은 로그인 세션이 있어야 조회된다 (has_resume 플래그만 공개).
"""

from __future__ import annotations

import logging
import re
from typing import Any, Iterator

from selectolax.parser import HTMLParser

from ..paths import ASSET_DIR
from .base import Adapter, JobPosting

log = logging.getLogger(__name__)

API = "https://jasoseol.com/api/v1"
LIST_URL = f"{API}/employment_companies"
HEADERS = {"Accept": "application/json", "Referer": "https://jasoseol.com/recruit"}


class JasoseolAdapter(Adapter):
    name = "jasoseol"
    label = "자소설닷컴"

    def fetch(self) -> Iterator[JobPosting]:
        data = self.fetcher.get_json(LIST_URL, params={"all": "true"}, headers=HEADERS)
        if not data:
            log.error("자소설닷컴 목록 조회 실패")
            return

        for company in data:
            employments = company.get("employments") or []
            if not employments:
                yield self._build(company, None)
                continue

            # 한 회사가 여러 직무를 올린 경우 직무별로 분리해 필터링 정확도를 높인다.
            # 다만 자소설은 같은 자리를 employment_id만 바꿔 여러 번 준다. 제목이
            # 구분되지 않으면 같은 공고이므로 여기서 접는다.
            seen_titles: set[str] = set()
            for emp in employments:
                job = self._build(company, emp)
                if job.title in seen_titles:
                    continue
                seen_titles.add(job.title)
                yield job

    def _build(self, company: dict[str, Any], emp: dict[str, Any] | None) -> JobPosting:
        cid = company.get("id")
        eid = emp.get("id") if emp else None
        pid = f"{cid}-{eid}" if eid else str(cid)

        field = (emp or {}).get("field") or ""
        base_title = company.get("title") or ""
        title = f"{base_title} / {field}" if field and field not in base_title else base_title

        deadline = (emp or {}).get("end_time") or company.get("end_time")

        desc_parts = []
        if field:
            desc_parts.append(f"[모집직무] {field}")
        for key, label in (
            ("english_score_requirement", "영어성적"),
            ("certificate_requirement", "자격증"),
            ("graduate_condition", "졸업조건"),
            ("work_type", "근무형태"),
        ):
            if (emp or {}).get(key):
                desc_parts.append(f"[{label}] {emp[key]}")
        if (emp or {}).get("has_resume"):
            desc_parts.append("[자소서] 자기소개서 문항 있음")

        group = company.get("company_group") or {}

        return JobPosting(
            platform=self.name,
            platform_job_id=pid,
            url=f"https://jasoseol.com/recruit/{cid}",
            company=company.get("name", ""),
            title=title,
            category=field or None,
            deadline=deadline,
            posted_at=company.get("opened_at") or company.get("created_at"),
            image_url=company.get("image_url"),
            description="\n".join(desc_parts),
            raw={
                "company_id": cid,
                "employment_id": eid,
                "employment": emp,
                "has_resume": bool((emp or {}).get("has_resume")),
                "employment_page_url": company.get("employment_page_url"),
                "business_size": group.get("business_size"),
                "business_type": group.get("business_type"),
            },
        )

    def enrich(self, job: JobPosting) -> JobPosting:
        """상세 content(HTML)를 받아 본문 텍스트 또는 공고 이미지를 확보한다."""
        cid = job.raw.get("company_id")
        if not cid:
            return job

        data = self.fetcher.get_json(f"{LIST_URL}/{cid}", headers=HEADERS)
        if not data:
            return job

        content = data.get("content") or ""
        if content:
            tree = HTMLParser(content)
            text = re.sub(r"\n{3,}", "\n\n", tree.text(separator="\n")).strip()
            if len(text) > 40:
                job.description = (job.description + "\n\n[공고 본문]\n" + text).strip()

            imgs = [
                img.attributes.get("src") for img in tree.css("img") if img.attributes.get("src")
            ]
            if imgs:
                job.image_url = imgs[0]
                job.raw["content_images"] = imgs

        if job.image_url:
            self._save_image(job)

        job.raw["detail"] = {k: v for k, v in data.items() if k != "content"}
        return job

    def _save_image(self, job: JobPosting) -> None:
        """공고 이미지를 로컬에 저장해 에이전트가 비전으로 읽을 수 있게 한다."""
        url = job.image_url
        ext = ".webp" if ".webp" in url else (".png" if ".png" in url else ".jpg")
        dest = ASSET_DIR / job.platform / f"{job.platform_job_id}{ext}"
        if dest.exists():
            job.image_path = str(dest)
            return
        if self.fetcher.download(url, dest):
            job.image_path = str(dest)
