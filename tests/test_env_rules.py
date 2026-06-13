"""environment_checks 파싱 · 비교 대상 산출 · 표기 불일치 매칭."""
from conftest import make_repo

from reviewbot.env_rules import build_directive, parse_environment_checks, resolve_comparisons

RULE = """# 룰
```yaml
environment_checks:
  - changed: [dev2-kr-west1, dev2-kr-west2]
    compare_with: [lcm3-kr-west1]
  - changed: [qa2-kr-west1, qa2-kr-west2]
    compare_with: [lcm3-kr-west1, dev2-kr-west1, dev2-kr-west2]
  - changed: [prd-e-kr-west1, prd-e-kr-east1]
    compare_with: [dev2-kr-west1, dev2-kr-west2, qa2-kr-west1, qa2-kr-west2]
```
"""
BASE = "gitops/lcm-manila/kustomize/overlay"


def test_parse_environment_checks():
    checks = parse_environment_checks([RULE])
    assert len(checks) == 3
    assert checks[0]["changed"] == ["dev2-kr-west1", "dev2-kr-west2"]


def test_resolve_exact_names():
    """룰 표기 = 실제 디렉토리명 (정확 일치)."""
    d = make_repo({f"{BASE}/{e}/x.yaml": "a: 1\n" for e in ["dev2-kr-west1", "lcm3-kr-west1"]})
    checks = parse_environment_checks([RULE])
    comp, peers, unres = resolve_comparisons([f"{BASE}/dev2-kr-west1/x.yaml"], checks, d)
    assert comp == {"dev2-kr-west1": ["lcm3-kr-west1"]}
    assert peers[f"{BASE}/dev2-kr-west1/x.yaml"] == [f"{BASE}/lcm3-kr-west1/x.yaml"]
    assert not unres


def test_resolve_fuzzy_names():
    """룰엔 dev2-kr-west1인데 실제 디렉토리는 dev2 (정규화 매칭)."""
    d = make_repo({f"{BASE}/{e}/x.yaml": "a: 1\n" for e in ["dev2", "lcm3", "qa2"]})
    checks = parse_environment_checks([RULE])
    comp, peers, unres = resolve_comparisons([f"{BASE}/dev2/x.yaml"], checks, d)
    assert "dev2" in comp and comp["dev2"] == ["lcm3-kr-west1"]
    assert peers[f"{BASE}/dev2/x.yaml"] == [f"{BASE}/lcm3/x.yaml"]
    assert not unres


def test_resolve_unresolved_goes_to_llm():
    """비교 대상이 실제 레포에 없으면 unresolved → LLM 보완."""
    d = make_repo({f"{BASE}/dev2/x.yaml": "a: 1\n"})
    checks = [{"changed": ["dev2-kr-west1"], "compare_with": ["staging-special"]}]
    comp, peers, unres = resolve_comparisons([f"{BASE}/dev2/x.yaml"], checks, d)
    assert unres.get("dev2") == ["staging-special"]
    directive = build_directive(comp, peers, unres)
    assert "glob/list_dir" in directive and "staging-special" in directive


def test_resolve_qa_accumulates_targets():
    """승급 파이프라인: qa는 lcm3 + dev2들과 비교 (누적)."""
    d = make_repo({f"{BASE}/{e}/x.yaml": "a: 1\n"
                   for e in ["qa2-kr-west1", "lcm3-kr-west1", "dev2-kr-west1", "dev2-kr-west2"]})
    checks = parse_environment_checks([RULE])
    comp, _, _ = resolve_comparisons([f"{BASE}/qa2-kr-west1/x.yaml"], checks, d)
    assert comp["qa2-kr-west1"] == ["lcm3-kr-west1", "dev2-kr-west1", "dev2-kr-west2"]


def test_no_match_returns_empty():
    d = make_repo({f"{BASE}/sandbox/x.yaml": "a: 1\n"})
    checks = parse_environment_checks([RULE])
    comp, _, _ = resolve_comparisons([f"{BASE}/sandbox/x.yaml"], checks, d)
    assert comp == {}
