"""어댑터 레지스트리. 새 플랫폼은 여기에 한 줄 추가하면 파이프라인이 자동으로 태운다."""

from .base import Adapter, JobPosting
from .jasoseol import JasoseolAdapter
from .saramin import SaraminAdapter
from .wanted import WantedAdapter

REGISTRY: dict[str, type[Adapter]] = {
    WantedAdapter.name: WantedAdapter,
    SaraminAdapter.name: SaraminAdapter,
    JasoseolAdapter.name: JasoseolAdapter,
}

LABELS = {
    "wanted": "원티드",
    "saramin": "사람인",
    "jasoseol": "자소설닷컴",
}

__all__ = ["Adapter", "JobPosting", "REGISTRY", "LABELS"]
