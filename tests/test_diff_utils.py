"""diff 파싱 · 라인번호 주석 · 인라인 코멘트 검증."""
from reviewbot.diff_utils import parse_diff, split_diff_by_file, validate_comments

DIFF = """diff --git a/gitops/f.yaml b/gitops/f.yaml
--- a/gitops/f.yaml
+++ b/gitops/f.yaml
@@ -10,3 +10,4 @@
 replicas: 2
-image: app:1.0
+image: app:1.1
+pullPolicy: Always
"""


def test_parse_diff_valid_lines():
    valid, annotated = parse_diff(DIFF)
    lines = valid["gitops/f.yaml"]
    # context(10) + 추가(11,12), 삭제(11)
    assert sorted(lines["RIGHT"]) == [10, 11, 12]
    assert sorted(lines["LEFT"]) == [11]
    # 주석이 라인번호와 함께 붙는다
    assert "R11" in annotated and "L11" in annotated


def test_validate_comments_in_and_out_of_diff():
    valid, _ = parse_diff(DIFF)
    ok, dropped = validate_comments([
        {"path": "gitops/f.yaml", "line": 11, "side": "RIGHT", "body": "a"},
        {"path": "gitops/f.yaml", "line": 11, "side": "LEFT", "body": "b"},
        {"path": "gitops/f.yaml", "line": 999, "body": "diff 밖"},
    ], valid)
    assert len(ok) == 2 and len(dropped) == 1
    assert dropped[0]["line"] == 999


def test_validate_comments_unknown_path_dropped():
    valid, _ = parse_diff(DIFF)
    ok, dropped = validate_comments([{"path": "nope.yaml", "line": 1, "body": "x"}], valid)
    assert not ok and len(dropped) == 1


def test_split_diff_by_file():
    multi = DIFF + """diff --git a/gitops/g.yaml b/gitops/g.yaml
--- a/gitops/g.yaml
+++ b/gitops/g.yaml
@@ -1,1 +1,1 @@
-x: 1
+x: 2
"""
    _, annotated = parse_diff(multi)
    chunks = split_diff_by_file(annotated)
    assert set(chunks) == {"gitops/f.yaml", "gitops/g.yaml"}
    assert "pullPolicy" in chunks["gitops/f.yaml"]
