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
(reference_environments의 대칭 그룹과 달리 방향성이 있다.)

코드가 '무엇과 비교할지'를 결정론적으로 산출하고, 실제 값 비교는 에이전트가 도구로 한다.
"""
import re

import yaml

YAML_FENCE_RE = re.compile(r"```ya?ml\s*\n(.*?)```", re.S)


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


def resolve_comparisons(changed_paths: list[str], checks: list[dict]):
    """변경 파일 경로에서 환경을 감지하고 비교 대상 파일을 산출한다.

    환경 이름은 경로의 디렉토리 세그먼트와 정확히 일치해야 한다
    (예: .../overlay/dev2-kr-west1/deployment.yaml → 환경 dev2-kr-west1).

    returns:
      comparisons: {변경환경: [비교대상환경, ...]}
      peer_paths:  {변경파일경로: [비교대상파일경로, ...]}
        — 변경 파일의 환경 세그먼트를 비교 대상 환경으로 치환한 경로
    """
    all_envs: set[str] = set()
    for c in checks:
        all_envs |= set(c["changed"]) | set(c["compare_with"])

    comparisons: dict[str, list[str]] = {}
    peer_paths: dict[str, list[str]] = {}

    for path in changed_paths:
        segs = path.split("/")
        for i, seg in enumerate(segs):
            if seg not in all_envs:
                continue
            # 이 환경이 changed에 포함된 모든 규칙의 compare_with 합집합
            targets: list[str] = []
            for c in checks:
                if seg in c["changed"]:
                    for t in c["compare_with"]:
                        if t != seg and t not in targets:
                            targets.append(t)
            if not targets:
                continue
            comparisons.setdefault(seg, [])
            for t in targets:
                if t not in comparisons[seg]:
                    comparisons[seg].append(t)
            for t in targets:
                peer = "/".join(segs[:i] + [t] + segs[i + 1:])
                peer_paths.setdefault(path, [])
                if peer not in peer_paths[path]:
                    peer_paths[path].append(peer)
    return comparisons, peer_paths


def build_directive(comparisons: dict, peer_paths: dict) -> str:
    """에이전트/플래너에 줄 '환경 비교 지시' 텍스트. 비어 있으면 ''."""
    if not comparisons:
        return ""
    lines = ["## 환경 비교 지시 (REVIEW_RULE.md 기반 — 반드시 수행)"]
    for env, targets in comparisons.items():
        lines.append(f"- 변경 환경 `{env}` → 다음 환경과 값을 비교: {', '.join(targets)}")
    lines.append("\n비교 대상 파일 (read_file/grep으로 값을 대조하라):")
    for src, peers in peer_paths.items():
        for p in peers:
            lines.append(f"- `{src}` ↔ `{p}`")
    return "\n".join(lines)
