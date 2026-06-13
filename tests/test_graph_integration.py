"""graph 파이프라인 통합 — tool use loop, 멀티에이전트, 분할, 환경비교, 리뷰포맷."""
import json

from conftest import FakeGH, FakeMsg, FakeToolCall, ScriptedLLM, base_cfg, make_repo

from graph import build_graph

BASE = "gitops/lcm-manila/kustomize/overlay"


def _diff(path, frm="1", to="2"):
    return (f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n"
            f"@@ -1,2 +1,2 @@\n spec:\n-  replicas: {frm}\n+  replicas: {to}\n")


async def test_single_agent_tool_use_loop():
    """에이전트가 grep 도구를 실제로 돌려 결과를 받고 findings를 낸다."""
    d = make_repo({f"{BASE}/dev2/deployment.yaml": "spec:\n  replicas: 3\n",
                   f"{BASE}/qa2/deployment.yaml": "spec:\n  replicas: 1\n"})
    grep_result = {}

    def agent_fn(messages):
        if not any(m.get("role") == "tool" for m in messages):
            return FakeMsg(tool_calls=[FakeToolCall("c", "grep",
                           {"pattern": "replicas:", "path_glob": f"{BASE}/**/deployment.yaml"})])
        grep_result["text"] = [m for m in messages if m["role"] == "tool"][-1]["content"]
        return FakeMsg(content="발견사항:\n- [warn] dev2:2 qa2와 불일치")

    llm = ScriptedLLM(
        planner=f'{{"agents":[{{"name":"env","focus":"환경","files":["{BASE}/dev2/deployment.yaml"]}}]}}',
        aggregator=f'{{"summary":"불일치","inline_comments":[{{"path":"{BASE}/dev2/deployment.yaml","line":2,"side":"RIGHT","severity":"warn","body":"qa2는 1"}}],"agreements":[]}}',
        agent_fn=agent_fn,
    )
    gh = FakeGH(files=[{"filename": f"{BASE}/dev2/deployment.yaml", "status": "modified"}],
                diff=_diff(f"{BASE}/dev2/deployment.yaml", "1", "3"))
    result = await build_graph(gh, llm, base_cfg(d)).ainvoke({})
    assert "dev2" in grep_result["text"] and "qa2" in grep_result["text"]  # 실제 grep 동작
    assert len(result["inline_comments"]) == 1 and result["inline_comments"][0]["line"] == 2


async def test_multi_agent_fanout():
    """planner가 2개로 나누면 둘 다 병렬 실행되어 발견사항이 합쳐진다."""
    d = make_repo({"gitops/x": "y"})
    llm = ScriptedLLM(
        planner='{"agents":[{"name":"helm","focus":"helm","files":["a"]},{"name":"kustomize","focus":"kustomize","files":["b"]}]}',
    )
    gh = FakeGH(files=[{"filename": "gitops/helm/v.yaml", "status": "modified"}], diff=_diff("gitops/helm/v.yaml"))
    result = await build_graph(gh, llm, base_cfg(d, agent_concurrency=2)).ainvoke({})
    assert len(result["agents"]) == 2
    # aggregator가 받은 user에 두 에이전트 발견사항이 모두 들어감
    assert "helm" in llm.agg_users[0] and "kustomize" in llm.agg_users[0]


async def test_inline_vs_summary_split():
    """diff 안 라인은 인라인, diff 밖 라인은 통합리뷰(dropped)로."""
    d = make_repo({"gitops/x": "y"})
    path = "gitops/f.yaml"
    llm = ScriptedLLM(
        planner=f'{{"agents":[{{"name":"r","focus":"전체","files":["{path}"]}}]}}',
        aggregator=f'{{"summary":"s","inline_comments":[{{"path":"{path}","line":2,"side":"RIGHT","severity":"warn","body":"안"}},{{"path":"{path}","line":99,"side":"RIGHT","severity":"info","body":"밖"}}],"agreements":[]}}',
    )
    gh = FakeGH(files=[{"filename": path, "status": "modified"}], diff=_diff(path))
    result = await build_graph(gh, llm, base_cfg(d)).ainvoke({})
    assert len(result["inline_comments"]) == 1 and result["inline_comments"][0]["line"] == 2
    assert len(result["dropped_comments"]) == 1 and result["dropped_comments"][0]["line"] == 99


async def test_planner_receives_repo_tree():
    d = make_repo({f"{BASE}/{e}/deployment.yaml": "x\n" for e in ["dev2", "qa2", "prd-a"]})
    llm = ScriptedLLM(planner='{"agents":[{"name":"r","focus":"전체","files":["x"]}]}')
    gh = FakeGH(files=[{"filename": f"{BASE}/dev2/deployment.yaml", "status": "modified"}],
                diff=_diff(f"{BASE}/dev2/deployment.yaml"))
    await build_graph(gh, llm, base_cfg(d)).ainvoke({})
    u = llm.planner_users[0]
    assert "레포 디렉토리 구조" in u and "qa2" in u and "prd-a" in u


async def test_env_comparison_directive_resolves_actual_paths():
    """룰 dev2-kr-west1 → 실제 dev2/lcm3 경로로 비교 지시가 에이전트에 전달."""
    d = make_repo({
        f"{BASE}/dev2/deployment.yaml": "replicas: 3\n",
        f"{BASE}/lcm3/deployment.yaml": "replicas: 1\n",
        "gitops/lcm-manila/REVIEW_RULE.md":
            "```yaml\nenvironment_checks:\n  - changed: [dev2-kr-west1]\n    compare_with: [lcm3-kr-west1]\n```",
    })
    llm = ScriptedLLM(planner=f'{{"agents":[{{"name":"env","focus":"비교","files":["{BASE}/dev2/deployment.yaml"]}}]}}')
    gh = FakeGH(files=[{"filename": f"{BASE}/dev2/deployment.yaml", "status": "modified"}],
                diff=_diff(f"{BASE}/dev2/deployment.yaml"))
    await build_graph(gh, llm, base_cfg(d)).ainvoke({})
    start = llm.agent_starts[0]
    assert f"{BASE}/lcm3/deployment.yaml" in start  # 실제 경로로 해결됨


async def test_aggregator_receives_team_rules_format():
    d = make_repo({
        "gitops/lcm-manila/REVIEW_RULE.md": "## 리뷰 포맷\n- 정중하고 간결하게\n",
        "gitops/lcm-manila/svc/x.yaml": "a: 1\n",
    })
    llm = ScriptedLLM(planner='{"agents":[{"name":"r","focus":"전체","files":["gitops/lcm-manila/svc/x.yaml"]}]}')
    gh = FakeGH(files=[{"filename": "gitops/lcm-manila/svc/x.yaml", "status": "modified"}],
                diff=_diff("gitops/lcm-manila/svc/x.yaml"))
    await build_graph(gh, llm, base_cfg(d)).ainvoke({})
    assert "팀 리뷰 규칙" in llm.agg_users[0] and "정중하고 간결하게" in llm.agg_users[0]


async def test_existing_comment_agreement():
    """기존 코멘트와 중복이면 새로 안 달고 동의 답글."""
    d = make_repo({"gitops/x": "y"})
    llm = ScriptedLLM(
        planner='{"agents":[{"name":"r","focus":"전체","files":["a"]}]}',
        aggregator='{"summary":"s","inline_comments":[],"agreements":[{"comment_id":50,"body":"동의합니다"}]}',
    )
    gh = FakeGH(files=[{"filename": "gitops/f.yaml", "status": "modified"}], diff=_diff("gitops/f.yaml"),
                existing=[{"id": 50, "type": "inline", "user": "kim", "path": "gitops/f.yaml", "line": 1, "body": "지적"}])
    result = await build_graph(gh, llm, base_cfg(d, dry_run=False)).ainvoke({})
    assert gh.replies == [(50, "🤖 동의합니다")] and result["posted_agreements"] == 1


async def test_planner_parse_failure_falls_back_to_single():
    d = make_repo({"gitops/x": "y"})
    llm = ScriptedLLM(planner="이건 JSON이 아님 그냥 설명")
    gh = FakeGH(files=[{"filename": "gitops/f.yaml", "status": "modified"}], diff=_diff("gitops/f.yaml"))
    result = await build_graph(gh, llm, base_cfg(d)).ainvoke({})
    assert len(result["agents"]) == 1 and result["agents"][0]["name"] == "reviewer"


async def test_turn_limit_forces_conclusion():
    """에이전트가 계속 도구만 부르면 max_turns에서 결론 강제."""
    d = make_repo({"gitops/x": "y"})
    forced = {"n": 0}

    def agent_fn(messages):
        if not messages or messages[-1].get("role") == "user" and "도구 사용을 멈추고" in messages[-1].get("content", ""):
            forced["n"] += 1
            return FakeMsg(content="발견사항:\n- 강제 종료")
        return FakeMsg(tool_calls=[FakeToolCall("c", "list_dir", {"path": "gitops"})])

    llm = ScriptedLLM(planner='{"agents":[{"name":"r","focus":"전체","files":["a"]}]}', agent_fn=agent_fn)
    gh = FakeGH(files=[{"filename": "gitops/f.yaml", "status": "modified"}], diff=_diff("gitops/f.yaml"))
    result = await build_graph(gh, llm, base_cfg(d, max_turns=3)).ainvoke({})
    assert forced["n"] == 1  # 턴 초과 후 결론 강제 1회
