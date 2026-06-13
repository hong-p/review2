"""REVIEW_RULE.md의 environment_checks 파싱 + 비교 대상 산출.

승급 파이프라인형 비대칭 단방향 매핑을 지원한다. 예:

    ```yaml
    environment_checks:
      - changed: [dev2-kr-west1, dev2-kr-west2]
        compare_with: [lcm3-kr-west1]
      - changed: [qa2-kr-west1, qa2-kr-west2]
        compare_with: [lcm3-kr-west1, dev2-kr-west1, dev2-kr-west2]
    ```

PR에서 changed 목록의 환경이 변경되면, compare_with 환경들과 비교해야 한다는 의미.
방향성이 있다(비대칭). 대칭 비교가 필요하면 changed/compare_with에 서로를 양방향으로 적는다.

환경명 표기 불일치 대응: 룰엔 `dev2-kr-west1`인데 실제 디렉토리는 `dev2`처럼 다를 수 있다.
- 코드가 정규화 매칭(대소문자·하이픈/언더스코어 무시 + 접두어 일치)으로 best-effort로 잇고,
  실제 형제 디렉토리를 스캔해 비교 경로를 만든다.
- 코드가 못 맞춘 환경은 directive에 '실제 디렉토리에서 찾아 비교(LLM)' 항목으로 남겨,
  에이전트가 glob/list_dir로 탐색해 보완하게 한다.
"""
import os
import re

import yaml

YAML_FENCE_RE = re.compile(r"```ya?ml\s*\n(.*?)```", re.S)


def _norm(s: str) -> str:
    return s.lower().replace("-", "").replace("_", "")


def _envs_equiv(a: str, b: str) -> bool:
    """두 환경명이 같은 환경을 가리키는지 (정규화 정확 또는 접두어 일치)."""
    na, nb = _norm(a), _norm(b)
    return na == nb or na.startswith(nb) or nb.startswith(na)


def _matches_any(actual_seg: str, rule_envs: list[str]) -> list[str]:
    """실제 디렉토리 세그먼트에 대응하는 룰 환경명들을 모두 반환 (복수 허용).

    실제 dev2 가 룰 dev2-kr-west1, dev2-kr-west2 둘 다에 대응할 수 있다.
    """
    return [e for e in rule_envs if _envs_equiv(actual_seg, e)]


def _match_env(target: str, candidates: list[str]) -> str | None:
    """target(룰 환경명)에 대응하는 실제 디렉토리명을 candidates에서 1:1로 찾는다.

    정확 일치 우선, 없으면 정규화 등가. 후보가 여럿이면(모호) None → LLM 보완.
    """
    if target in candidates:
        return target
    eq = [c for c in candidates if _envs_equiv(target, c)]
    return eq[0] if len(eq) == 1 else None


def parse_environment_checks(rule_texts: list[str]) -> list[dict]:
    """REVIEW_RULE.md 본문들에서 environment_checks 추출.

    returns [{"changed": [...], "compare_with": [...]}]
    """
    checks: list[dict] = []
    for text in rule_texts:
        for block in YAML_FENCE_RE.findall(text):
            try:
                data = yaml.safe_load(block)
            except yaml.YAMLError:
                continue
            if not isinstance(data, dict):
                continue
            ec = data.get("environment_checks")
            if not isinstance(ec, list):
                continue
            for item in ec:
                if not isinstance(item, dict):
                    continue
                changed = item.get("changed")
                compare = item.get("compare_with")
                if isinstance(changed, list) and isinstance(compare, list):
                    checks.append({
                        "changed": [str(e) for e in changed],
                        "compare_with": [str(e) for e in compare],
                    })
    return checks


def _list_subdirs(repo_dir: str, rel_parent: str) -> list[str]:
    full = os.path.join(repo_dir, rel_parent) if rel_parent else repo_dir
    try:
        return [n for n in os.listdir(full) if os.path.isdir(os.path.join(full, n))]
    except OSError:
        return []


def resolve_comparisons(changed_paths: list[str], checks: list[dict], repo_dir: str = "."):
    """변경 파일 경로에서 환경을 감지하고 비교 대상 파일을 산출한다.

    환경명은 실제 디렉토리와 표기가 다를 수 있어(룰 dev2-kr-west1 ↔ 실제 dev2),
    정규화 매칭으로 잇고 실제 형제 디렉토리를 스캔해 경로를 만든다.

    returns:
      comparisons: {변경환경(실제 디렉토리명): [비교대상환경(룰 표기), ...]}
      peer_paths:  {변경파일경로: [실제 비교대상파일경로, ...]}
      unresolved:  {변경환경: [코드가 실제 디렉토리를 못 찾은 비교대상(룰 표기), ...]}
                   — 에이전트가 glob/list_dir로 찾아 보완해야 할 항목
    """
    all_changed: list[str] = []
    for c in checks:
        all_changed.extend(c["changed"])

    comparisons: dict[str, list[str]] = {}
    peer_paths: dict[str, list[str]] = {}
    unresolved: dict[str, list[str]] = {}

    for path in changed_paths:
        segs = path.split("/")
        for i, seg in enumerate(segs):
            # 이 세그먼트(실제 환경 디렉토리명)가 룰의 어떤 changed에 대응하나 (복수 가능)
            rule_envs = _matches_any(seg, all_changed)
            if not rule_envs:
                continue
            targets: list[str] = []
            for c in checks:
                if any(re_ in c["changed"] for re_ in rule_envs):
                    for t in c["compare_with"]:
                        if t not in targets:
                            targets.append(t)
            if not targets:
                continue
            comparisons.setdefault(seg, [])
            for t in targets:
                if t not in comparisons[seg]:
                    comparisons[seg].append(t)
            # 비교 대상 환경(룰 표기)을 실제 형제 디렉토리와 매칭해 경로 생성
            siblings = _list_subdirs(repo_dir, "/".join(segs[:i]))
            for t in targets:
                actual = _match_env(t, siblings)
                if actual and actual != seg:
                    peer = "/".join(segs[:i] + [actual] + segs[i + 1:])
                    peer_paths.setdefault(path, [])
                    if peer not in peer_paths[path]:
                        peer_paths[path].append(peer)
                elif not actual:
                    unresolved.setdefault(seg, [])
                    if t not in unresolved[seg]:
                        unresolved[seg].append(t)
    return comparisons, peer_paths, unresolved


def build_directive(comparisons: dict, peer_paths: dict, unresolved: dict | None = None) -> str:
    """에이전트/플래너에 줄 '환경 비교 지시' 텍스트. 비어 있으면 ''."""
    if not comparisons:
        return ""
    unresolved = unresolved or {}
    lines = ["## 환경 비교 지시 (REVIEW_RULE.md 기반 — 반드시 수행)"]
    for env, targets in comparisons.items():
        lines.append(f"- 변경 환경 `{env}` → 다음 환경과 값을 비교: {', '.join(targets)}")
    if any(peer_paths.values()):
        lines.append("\n비교 대상 파일 (read_file/grep으로 값을 대조하라):")
        for src, peers in peer_paths.items():
            for p in peers:
                lines.append(f"- `{src}` ↔ `{p}`")
    if any(unresolved.values()):
        lines.append("\n아래는 실제 디렉토리명이 룰 표기와 달라 경로를 자동 확정하지 못했다.")
        lines.append("glob/list_dir로 대응하는 실제 환경 디렉토리를 찾아 비교하라 (표기가 약간 달라도 같은 환경이면 비교):")
        for env, targets in unresolved.items():
            lines.append(f"- `{env}` 의 비교 대상(룰 표기): {', '.join(targets)}")
    return "\n".join(lines)
