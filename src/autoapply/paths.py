"""코드가 있는 곳과 데이터가 있는 곳을 가른다.

이 저장소는 **코드**다. 실제로 돌릴 때 쓰는 개인 데이터 — 진짜 이력서, 계정 정보,
수집된 공고 DB — 는 바깥의 **인스턴스 디렉터리**에 둔다. `AUTOAPPLY_HOME` 환경변수가
그곳을 가리킨다. 안 걸면 인스턴스는 저장소 자신이다.

모듈마다 `Path(__file__).parents[N]`을 다시 계산하지 말고 여기서 가져다 쓴다.
"""

from __future__ import annotations

import os
from pathlib import Path

# 이 파일은 <repo>/src/autoapply/paths.py 다.
CODE_ROOT = Path(__file__).resolve().parents[2]

ENV_VAR = "AUTOAPPLY_HOME"


def _resolve_instance_root() -> Path:
    raw = os.environ.get(ENV_VAR, "").strip()
    if not raw:
        return CODE_ROOT

    root = Path(raw).expanduser()
    if not root.is_dir():
        raise RuntimeError(
            f"{ENV_VAR}가 가리키는 곳이 디렉터리가 아닙니다: {root}\n"
            f"경로를 고치거나, 저장소 자체를 인스턴스로 쓰려면 {ENV_VAR}를 지우세요."
        )
    return root.resolve()


INSTANCE_ROOT = _resolve_instance_root()

# 인스턴스 쪽 — 사람마다 다르고 git에 들어가면 안 된다
PROFILE_DIR = INSTANCE_ROOT / "profile"
DATA_DIR = INSTANCE_ROOT / "data"
ASSET_DIR = DATA_DIR / "assets"
DB_PATH = DATA_DIR / "jobs.db"
# 스케줄 잡의 로그(plist StandardErrorPath)와 **같은 곳**이다. 폰에서 부른
# 작업도 여기 남겨야 "왜 안 됐나"를 한 자리에서 본다 — tasks.spawn() 참고.
LOG_DIR = DATA_DIR / "logs"
RECIPE_DIR = INSTANCE_ROOT / "recipes"

# 브라우저 프로필. 사람이 여기 한 번 로그인하면 러너가 계속 재사용한다.
# 세션이 사는 유일한 장소다 — 지우면 다시 로그인해야 한다.
BROWSER_DIR = PROFILE_DIR / "browser"
# 지원 증적(스크린샷·폼 스냅샷). 원장의 evidence_path가 여기를 가리킨다.
EVIDENCE_DIR = DATA_DIR / "evidence"

# 이력서 원본(MD SSOT). 어셈블러가 읽는 사실 저장소 — 여기 없는 사실은 만들지 않는다.
RESUME_SRC_DIR = PROFILE_DIR / "resume"
# 공고별로 조립된 결과. 지원 원장과 짝을 이뤄 "무엇을 보냈는지"를 남긴다.
RESUME_OUT_DIR = PROFILE_DIR / "generated"

# 사람이 준 수정 요청의 원장. 이력서를 쓸 때 가이드와 **함께** 읽는다.
#
# RESUME_SRC_DIR 밖에 두는 이유: load_guide()가 그 폴더의 .md를 전부 합쳐
# 사실 저장소로 넘긴다. 원장이 거기 섞이면 "지난번에 이렇게 고쳐달라고 했다"가
# "이것이 규칙이다"와 구분되지 않는다. 둘은 무게가 다르다.
REVISION_LOG = PROFILE_DIR / "revision-log.md"

RESUME_PATH = PROFILE_DIR / "resume.md"
CREDENTIALS_PATH = PROFILE_DIR / "credentials.json"


def _overridable(name: str) -> Path:
    """인스턴스에 같은 이름 파일이 있으면 그쪽, 없으면 저장소 기본값."""
    candidate = INSTANCE_ROOT / name
    return candidate if candidate.exists() else CODE_ROOT / name


CONFIG_PATH = _overridable("config.yaml")


def describe() -> dict[str, str]:
    return {
        "코드": str(CODE_ROOT),
        "인스턴스": str(INSTANCE_ROOT),
        "config.yaml": str(CONFIG_PATH),
        "jobs.db": str(DB_PATH) + ("" if DB_PATH.exists() else "  ← 없음"),
        "recipes/": str(RECIPE_DIR) + ("" if RECIPE_DIR.exists() else "  ← 없음"),
    }
