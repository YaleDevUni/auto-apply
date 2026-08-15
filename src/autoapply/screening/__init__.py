"""두 축의 판정기.

  rules.screen(job, cfg)          → 적합도: 이 공고가 나한테 맞나
  applicability.evaluate(...)     → 지원가능성: 이걸 자동으로 지원할 수 있나

둘 다 순수 함수다. 네트워크도 LLM도 쓰지 않는다.
"""

from .applicability import evaluate as evaluate_applicability
from .applicability import summarize_blockers
from .rules import detect_track, screen

__all__ = ["screen", "detect_track", "evaluate_applicability", "summarize_blockers"]
