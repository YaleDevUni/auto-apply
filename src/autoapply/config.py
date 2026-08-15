"""config.yaml 로딩. 콤마로 나열한 키워드 문자열을 평탄한 리스트로 정규화한다."""

from __future__ import annotations

import copy
import functools
from pathlib import Path
from typing import Any

import yaml

from .paths import CONFIG_PATH, PROFILE_DIR, RESUME_PATH  # noqa: F401  (재수출)


def _flatten_keywords(raw: Any) -> list[str]:
    """['a, b', 'c'] → ['a', 'b', 'c'] (소문자, 공백 정리)"""
    out: list[str] = []
    for entry in raw or []:
        for part in str(entry).split(","):
            kw = part.strip().lower()
            if kw:
                out.append(kw)
    return out


@functools.lru_cache(maxsize=1)
def load_config(path: Path | None = None) -> dict[str, Any]:
    cfg = yaml.safe_load((path or CONFIG_PATH).read_text(encoding="utf-8"))

    for track in cfg.get("tracks", {}).values():
        track["keywords"] = _flatten_keywords(track.get("keywords"))

    for cut in cfg.get("hardcuts", {}).values():
        cut["keywords"] = _flatten_keywords(cut.get("keywords"))
        cut["unless"] = _flatten_keywords(cut.get("unless"))
        cut["always"] = _flatten_keywords(cut.get("always"))

    scoring = cfg.get("scoring", {})
    for key in list(scoring):
        if key.startswith("keywords_"):
            scoring[key] = _flatten_keywords(scoring[key])

    loc = cfg.get("location", {})
    loc["preferred"] = _flatten_keywords(loc.get("preferred"))

    ap = cfg.get("applicability", {})
    for key in list(ap):
        if key.startswith("keywords_") or key.startswith("hosts_"):
            ap[key] = _flatten_keywords(ap[key])

    return cfg


def effective_config() -> dict[str, Any]:
    """판정에 실제로 쓰이는 설정. 항상 새 dict라 캐시를 오염시키지 않는다."""
    return copy.deepcopy(load_config())
