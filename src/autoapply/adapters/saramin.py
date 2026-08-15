"""사람인 어댑터 — 검색결과 정적 HTML 파싱.

사람인은 검색 리스트 카드 하나에 지역/경력/학력/고용형태/연봉/직무키워드/마감일이
모두 들어있다. 그래서 상세 페이지를 따로 받지 않아도 하드컷 필터가 동작한다.
(요청 수를 40분의 1로 줄이는 핵심 지점)
"""

from __future__ import annotations

import logging
import re
from datetime import date
from typing import Iterator
from urllib.parse import quote, urljoin

from selectolax.parser import HTMLParser, Node

from .base import Adapter, JobPosting

log = logging.getLogger(__name__)

BASE = "https://www.saramin.co.kr"
SEARCH = BASE + "/zf_user/search/recruit"


def _text(node: Node | None) -> str:
    return node.text(strip=True) if node is not None else ""


def parse_deadline(raw: str) -> str | None:
    """'~ 08/29(토)' → '2026-08-29'. '상시채용'/'채용시' 등은 None."""
    raw = raw.strip()
    m = re.search(r"(\d{1,2})/(\d{1,2})", raw)
    if not m:
        return None
    month, day = int(m.group(1)), int(m.group(2))
    today = date.today()
    year = today.year
    # 이미 지난 달짜면 내년 공고로 간주 (연말/연초 경계 처리)
    if month < today.month - 1:
        year += 1
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        return None


def parse_posted(raw: str) -> str | None:
    """'등록일 26/07/30' → '2026-07-30'"""
    m = re.search(r"(\d{2})/(\d{2})/(\d{2})", raw)
    if not m:
        return None
    yy, mm, dd = (int(x) for x in m.groups())
    try:
        return date(2000 + yy, mm, dd).isoformat()
    except ValueError:
        return None


class SaraminAdapter(Adapter):
    name = "saramin"
    label = "사람인"

    def fetch(self) -> Iterator[JobPosting]:
        s = self.cfg["scrape"]
        seen: set[str] = set()

        # 직무 검색어 + 관심 기업 회사명. 둘 다 같은 검색 화면을 쓴다.
        #
        # 검색어만으로는 특정 공고를 보장하지 못한다. 사람인 검색은 관련도순이라
        # 직무명이 정확히 맞아야 상위에 뜬다. 놓치면 안 되는 회사는 회사명으로
        # 훑는 것이 유일하게 확실한 방법이다.
        queries = list(s["saramin_keywords"]) + list(s.get("saramin_companies") or [])

        for keyword in queries:
            for page in range(1, s["saramin_pages"] + 1):
                url = (
                    f"{SEARCH}?searchType=search&searchword={quote(keyword)}"
                    f"&recruitPage={page}&recruitSort=relation&recruitPageCount=40"
                )
                resp = self.fetcher.get(url)
                if resp is None:
                    break

                nodes = HTMLParser(resp.text).css(".item_recruit")
                if not nodes:
                    break

                for node in nodes:
                    job = self._parse_card(node, keyword)
                    if job is None or job.platform_job_id in seen:
                        continue
                    seen.add(job.platform_job_id)
                    yield job

                if len(nodes) < 40:
                    break

    def _parse_card(self, node: Node, keyword: str) -> JobPosting | None:
        rec_idx = node.attributes.get("value")
        if not rec_idx:
            return None

        link = node.css_first(".job_tit a")
        if link is None:
            return None
        title = link.attributes.get("title") or _text(link)
        href = link.attributes.get("href", "")

        company = _text(node.css_first(".area_corp .corp_name a")) or _text(
            node.css_first(".area_corp .corp_name")
        )

        # 조건 영역: 지역은 <a>, 나머지(경력/학력/고용형태/연봉)는 <span>
        cond = node.css_first(".job_condition")
        location = experience = education = emp_type = salary = None
        if cond is not None:
            locs = [_text(a) for a in cond.css("a") if _text(a)]
            location = " ".join(dict.fromkeys(locs)) or None
            spans = [_text(sp) for sp in cond.css("span") if _text(sp)]
            # 지역 span은 <a>를 감싸고 있어 중복되므로 제거
            spans = [
                sp for sp in spans if sp not in locs and sp != (location or "").replace(" ", "")
            ]
            for sp in spans:
                if re.search(r"신입|경력|무관|인턴", sp) and experience is None:
                    experience = sp
                elif "졸" in sp or "학력" in sp:
                    education = sp
                elif re.search(r"정규직|계약직|인턴|파견|프리랜서|아르바이트", sp):
                    emp_type = sp
                elif "만원" in sp or "연봉" in sp or "회사내규" in sp:
                    salary = sp

        sector_raw = _text(node.css_first(".job_sector"))
        posted_at = parse_posted(sector_raw)
        sector = re.sub(r"등록일.*$", "", sector_raw).strip(" ,")

        deadline = parse_deadline(_text(node.css_first(".job_date .date")))

        # 리스트 카드 자체가 필터에 필요한 정보를 다 담고 있으므로 description으로 합성
        description = "\n".join(
            f"[{k}] {v}"
            for k, v in (
                ("직무분야", sector),
                ("지역", location),
                ("경력", experience),
                ("학력", education),
                ("고용형태", emp_type),
                ("급여", salary),
                ("검색어", keyword),
            )
            if v
        )

        return JobPosting(
            platform=self.name,
            platform_job_id=str(rec_idx),
            url=urljoin(BASE, href.replace("&amp;", "&")),
            company=company,
            title=title,
            category=sector or None,
            location=location,
            employment_type=emp_type,
            experience_req=experience,
            education_req=education,
            salary=salary,
            deadline=deadline,
            posted_at=posted_at,
            description=description,
            raw={"rec_idx": rec_idx, "sector": sector, "keyword": keyword},
        )
