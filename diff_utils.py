"""unified diff 파싱.

두 가지를 만든다:
1. valid_lines — 인라인 코멘트를 달 수 있는 (path, side, line) 집합.
   GitHub 리뷰 코멘트는 diff에 나타난 라인에만 달 수 있다.
2. annotated diff — 각 라인 앞에 R<새파일 라인번호> / L<원본 라인번호>를 붙인 텍스트.
   LLM이 정확한 라인번호로 인라인 코멘트를 지정할 수 있게 한다.
"""
import re

HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def parse_diff(diff_text: str) -> tuple[dict, str]:
    """returns (valid_lines, annotated_diff)

    valid_lines: {path: {"RIGHT": set[int], "LEFT": set[int]}}
    """
    valid: dict[str, dict[str, set[int]]] = {}
    annotated: list[str] = []
    path: str | None = None
    old_ln = new_ln = 0
    in_hunk = False

    for line in diff_text.splitlines():
        if line.startswith("diff --git"):
            in_hunk = False
            path = None
            annotated.append(line)
            continue
        if line.startswith("+++ "):
            target = line[4:].strip()
            if target == "/dev/null":
                path = None
            else:
                path = target[2:] if target.startswith("b/") else target
                valid.setdefault(path, {"RIGHT": set(), "LEFT": set()})
            in_hunk = False
            annotated.append(line)
            continue
        if line.startswith("--- "):
            annotated.append(line)
            continue

        m = HUNK_RE.match(line)
        if m:
            old_ln, new_ln = int(m.group(1)), int(m.group(3))
            in_hunk = True
            annotated.append(line)
            continue

        if not in_hunk or path is None:
            annotated.append(line)
            continue

        if line.startswith("+"):
            valid[path]["RIGHT"].add(new_ln)
            annotated.append(f"R{new_ln:<5}{line}")
            new_ln += 1
        elif line.startswith("-"):
            valid[path]["LEFT"].add(old_ln)
            annotated.append(f"L{old_ln:<5}{line}")
            old_ln += 1
        elif line.startswith("\\"):  # "\ No newline at end of file"
            annotated.append(line)
        else:  # context 라인 — 양쪽에 존재, 코멘트는 RIGHT 기준
            valid[path]["RIGHT"].add(new_ln)
            annotated.append(f"R{new_ln:<5}{line}")
            old_ln += 1
            new_ln += 1

    return valid, "\n".join(annotated)


def validate_comments(comments: list[dict], valid_lines: dict) -> tuple[list[dict], list[dict]]:
    """LLM이 만든 인라인 코멘트를 diff 기준으로 검증.

    returns (등록 가능한 코멘트, 탈락한 코멘트)
    """
    ok: list[dict] = []
    dropped: list[dict] = []
    for c in comments:
        path = c.get("path")
        line = c.get("line")
        side = str(c.get("side") or "RIGHT").upper()
        if side not in ("RIGHT", "LEFT"):
            side = "RIGHT"
        if not path or not isinstance(line, int):
            dropped.append(c)
            continue
        lines_for = valid_lines.get(path)
        if not lines_for:
            dropped.append(c)
            continue
        if line in lines_for[side]:
            c["side"] = side
            ok.append(c)
        elif side == "RIGHT" and line in lines_for["LEFT"]:
            # 삭제된 라인을 RIGHT로 잘못 지정한 경우 보정
            c["side"] = "LEFT"
            ok.append(c)
        else:
            dropped.append(c)
    return ok, dropped
