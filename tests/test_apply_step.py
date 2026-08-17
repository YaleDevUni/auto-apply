"""`runner/apply.py`의 `_step()` — 스텝 종류별 분기.

if/elif 6갈래를 match로 바꾼 리팩터링(2026-08-18)의 회귀 테스트다. `_step`은
그때까지 단위 테스트가 없었다 — 실제로는 dry-run 브라우저 실행으로만
검증됐다. `Session`은 얇은 `Protocol`(session.py)이라 가짜로 채우기 쉽지만
`_click`은 `page()`로 오버레이를 걷어내는 Playwright 전용 로직을 갖고 있어
여기서는 몽키패치로 갈음한다 — 그건 이 테스트의 관심사가 아니다.
"""

from __future__ import annotations

import pytest

from src.autoapply.runner import apply as apply_mod

JOB = {"job_id": 1, "platform": "wanted", "company": "테스트"}


class FakeSession:
    """`Session` Protocol만 채운다. `_click`이 쓰는 `page()`는 없다 — 일부러다."""

    def __init__(self, *, exists: bool = True):
        self._exists = exists
        self.calls: list[tuple] = []

    def goto(self, url):
        self.calls.append(("goto", url))

    def exists(self, selector, timeout_ms=3000):
        self.calls.append(("exists", selector))
        return self._exists

    def check(self, selector):
        self.calls.append(("check", selector))

    def fill(self, selector, value):
        self.calls.append(("fill", selector, value))

    def upload(self, selector, path):
        self.calls.append(("upload", selector, path))


def test_goto_bypasses_existence_check():
    """goto는 sel이 없다 — exists 검사보다 먼저 return해야 한다."""
    s = FakeSession(exists=False)
    apply_mod._step(s, {"do": "goto", "url": "https://example.com"}, JOB)
    assert s.calls == [("goto", "https://example.com")]


def test_check_dispatches_to_session():
    s = FakeSession()
    apply_mod._step(s, {"do": "check", "sel": "input#agree"}, JOB)
    assert ("check", "input#agree") in s.calls


def test_fill_dispatches_with_rendered_value():
    s = FakeSession()
    apply_mod._step(s, {"do": "fill", "sel": "input#name", "value": "박예일"}, JOB)
    assert ("fill", "input#name", "박예일") in s.calls


def test_click_and_submit_go_through_click_helper(monkeypatch):
    """click/submit 둘 다 `_click`을 타되, submit 플래그만 다르다."""
    seen = []
    monkeypatch.setattr(
        apply_mod, "_click",
        lambda s, sel, *, optional, submit: seen.append((sel, optional, submit)),
    )
    s = FakeSession()
    apply_mod._step(s, {"do": "click", "sel": "button#go"}, JOB)
    apply_mod._step(s, {"do": "submit", "sel": "button#send"}, JOB)
    assert seen == [("button#go", False, False), ("button#send", False, True)]


def test_expect_only_relies_on_existence_check():
    """expect는 그 자체로 액션이 없다 — 위의 exists 검사가 곧 단언이다."""
    s = FakeSession()
    apply_mod._step(s, {"do": "expect", "sel": "text=완료"}, JOB)
    assert s.calls == [("exists", "text=완료")]


def test_upload_missing_file_raises(tmp_path):
    s = FakeSession()
    missing = str(tmp_path / "없는파일.pdf")
    with pytest.raises(apply_mod.RecipeError):
        apply_mod._step(s, {"do": "upload", "sel": "input[type=file]", "path": missing}, JOB)


def test_unknown_step_raises():
    s = FakeSession()
    with pytest.raises(apply_mod.RecipeError, match="모르는 스텝"):
        apply_mod._step(s, {"do": "teleport"}, JOB)


def test_optional_step_skipped_when_element_missing():
    """optional이면 요소가 없어도 조용히 넘어간다 — 예외 없음."""
    s = FakeSession(exists=False)
    apply_mod._step(s, {"do": "check", "sel": "input#missing", "optional": True}, JOB)
    assert s.calls == [("exists", "input#missing")]


def test_required_step_raises_when_element_missing():
    s = FakeSession(exists=False)
    with pytest.raises(apply_mod.RecipeError, match="요소를 찾지 못함"):
        apply_mod._step(s, {"do": "check", "sel": "input#missing"}, JOB)
