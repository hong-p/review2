"""로컬 fs 도구 · 레포 트리."""
from conftest import make_repo

from tools import ToolContext, build_repo_tree, execute_tool

BASE = "gitops/lcm-manila/kustomize/overlay"


def _ctx(repo, **kw):
    return ToolContext(repo_dir=repo, **kw)


def test_grep_with_recursive_glob():
    d = make_repo({
        f"{BASE}/dev2/deployment.yaml": "replicas: 3\n",
        f"{BASE}/qa2/deployment.yaml": "replicas: 1\n",
    })
    out = execute_tool("grep", {"pattern": "replicas:", "path_glob": f"{BASE}/**/deployment.yaml"}, _ctx(d))
    assert "dev2" in out and "qa2" in out


def test_grep_no_match():
    d = make_repo({f"{BASE}/dev2/x.yaml": "a: 1\n"})
    out = execute_tool("grep", {"pattern": "nonexistent"}, _ctx(d))
    assert "매칭 없음" in out


def test_read_file_and_line_range():
    d = make_repo({"a.yaml": "l1\nl2\nl3\nl4\n"})
    full = execute_tool("read_file", {"path": "a.yaml"}, _ctx(d))
    assert "l1" in full and "l4" in full
    ranged = execute_tool("read_file", {"path": "a.yaml", "start_line": 2, "end_line": 3}, _ctx(d))
    assert "l2" in ranged and "l3" in ranged and "l1" not in ranged


def test_read_file_path_traversal_blocked():
    d = make_repo({"a.yaml": "x\n"})
    out = execute_tool("read_file", {"path": "../../../etc/passwd"}, _ctx(d))
    assert "레포 밖 경로" in out


def test_read_missing_file():
    d = make_repo({"a.yaml": "x\n"})
    out = execute_tool("read_file", {"path": "nope.yaml"}, _ctx(d))
    assert "파일 없음" in out


def test_glob_and_list_dir():
    d = make_repo({f"{BASE}/dev2/deployment.yaml": "x\n", f"{BASE}/qa2/deployment.yaml": "x\n"})
    g = execute_tool("glob", {"pattern": f"{BASE}/**/deployment.yaml"}, _ctx(d))
    assert "dev2" in g and "qa2" in g
    ls = execute_tool("list_dir", {"path": BASE}, _ctx(d))
    assert "dev2 (dir)" in ls and "qa2 (dir)" in ls


def test_get_diff_and_changed_files():
    ctx = _ctx(make_repo({"x": "y"}),
               changed_files=[{"path": "a.yaml", "status": "modified"}],
               diff_by_file={"a.yaml": "@@ -1 +1 @@\nR1   +a: 2"})
    assert "a.yaml" in execute_tool("get_changed_files", {}, ctx)
    assert "R1" in execute_tool("get_diff", {"path": "a.yaml"}, ctx)


def test_unknown_tool():
    out = execute_tool("frobnicate", {}, _ctx(make_repo({"x": "y"})))
    assert "알 수 없는 도구" in out


def test_build_repo_tree_shows_envs():
    d = make_repo({f"{BASE}/{e}/deployment.yaml": "x\n"
                   for e in ["dev2", "qa2", "prd-a"]} | {f"gitops/lcm-manila/helm/values.yaml": "x\n"})
    tree = build_repo_tree(d, max_depth=6)
    assert all(e in tree for e in ["dev2", "qa2", "prd-a", "helm"])
