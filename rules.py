"""REVIEW_RULE.md 파싱 — 참고 환경(reference_environments) 설정.

REVIEW_RULE.md는 자유 텍스트지만, yaml 코드블록으로 참고 환경 그룹을 선언할 수 있다:

    ```yaml
    reference_environments:
      - [dev, dev2, qa2]
      - [prd-a, prd-b]
    ```

같은 그룹에 속한 환경들은 설정이 통일되어야 한다는 의미.
한 환경의 파일이 변경되면 같은 그룹의 다른 환경에서 대응 파일을 찾아 비교한다.
"""
import logging
import re

import yaml

log = logging.getLogger(__name__)

YAML_FENCE_RE = re.compile(r"```ya?ml\s*\n(.*?)```", re.S)


def parse_env_groups(rule_texts: list[str]) -> list[list[str]]:
    """REVIEW_RULE.md 본문들에서 참고 환경 그룹을 추출한다.

    지원 형식:
      reference_environments:        # 그룹 여러 개
        - [dev, dev2, qa2]
        - [prd-a, prd-b]
      reference_environments: [dev, dev2, qa2]   # 평탄 리스트 = 그룹 1개
    """
    for text in rule_texts:
        for block in YAML_FENCE_RE.findall(text):
            try:
                data = yaml.safe_load(block)
            except yaml.YAMLError:
                continue
            if not isinstance(data, dict):
                continue
            envs = data.get("reference_environments") or data.get("environments")
            if not envs or not isinstance(envs, list):
                continue
            if all(isinstance(e, str) for e in envs):
                return [envs]
            groups = [g for g in envs if isinstance(g, list) and all(isinstance(e, str) for e in g)]
            if groups:
                return groups
    return []


def find_peer_paths(changed_paths: list[str], env_groups: list[list[str]]) -> dict[str, list[str]]:
    """변경 파일 경로에서 환경 세그먼트를 찾아, 같은 그룹의 다른 환경 경로를 만든다.

    예: env_groups=[[dev, dev2, qa2]] 이고
        gitops/lcm-manila/kustomize/overlay/dev2/deployment.yaml 이 변경됐으면
        → {".../dev2/deployment.yaml": [".../dev/deployment.yaml", ".../qa2/deployment.yaml"]}

    이번 PR에서 함께 변경된 파일은 비교 대상에서 제외한다 (diff에 이미 있으므로).
    """
    peers: dict[str, list[str]] = {}
    changed_set = set(changed_paths)
    for path in changed_paths:
        segments = path.split("/")
        peer_list: list[str] = []
        for group in env_groups:
            for i, seg in enumerate(segments):
                if seg not in group:
                    continue
                for env in group:
                    if env == seg:
                        continue
                    peer = "/".join(segments[:i] + [env] + segments[i + 1:])
                    if peer not in changed_set and peer not in peer_list:
                        peer_list.append(peer)
        if peer_list:
            peers[path] = peer_list
    return peers
