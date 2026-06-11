"""LangGraph 파이프라인.

fetch_pr ──┬─> changed_analyzer ──┬─> compare_reviewer ──┬─> post_summary
           └─> base_analyzer    ──┘                      └─> post_inline
(analyzer 2개 병렬, 게시 2개 병렬)
"""
import json
import logging
import posixpath
import re
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

import prompts
from config import Config
from diff_utils import parse_diff, validate_comments
from github_mcp import GitHubMCP
from llm import LLM, clip

log = logging.getLogger(__name__)

RULE_FILENAME = "RULE.md"
SEVERITY_MARK = {"error": "🔴", "warn": "🟡", "info": "🔵"}


class ReviewState(TypedDict, total=False):
    # fetch_pr 결과
    pr_title: str
    pr_body: str
    base_sha: str
    head_sha: str
    changed_files: list[dict]      # [{path, status}]
    annotated_diff: str
    valid_lines: dict              # {path: {"RIGHT": set, "LEFT": set}}
    rule_changed: bool
    head_rules: dict[str, str]     # PR에서 RULE.md가 변경된 경우 head 버전
    base_rules: dict[str, str]     # 변경 안 된 경우 base 버전
    base_files: dict[str, str]     # 변경된 파일들의 base 원본
    # analyzer 결과 (병렬 — 서로 다른 키에 기록)
    changed_analysis: str
    base_analysis: str
    # reviewer 결과
    review_summary: str
    inline_comments: list[dict]
    dropped_comments: list[dict]
    # 게시 결과 (병렬 — 서로 다른 키에 기록)
    posted_summary: bool
    posted_inline: int


def build_graph(gh: GitHubMCP, llm: LLM, cfg: Config):
    lang = cfg.review_language

    # ---- node: fetch_pr -------------------------------------------------

    async def fetch_pr(state: ReviewState) -> ReviewState:
        pr = await gh.get_pull_request()
        files = await gh.get_pull_request_files()
        diff = clip(await gh.get_pull_request_diff(), cfg.max_diff_chars, "diff")

        base_sha = pr["base"]["sha"]
        head_sha = pr["head"]["sha"]
        changed = [
            {"path": f.get("filename") or f.get("path"), "status": f.get("status", "")}
            for f in files
        ]
        valid_lines, annotated_diff = parse_diff(diff)

        # --- RULE.md 변경 여부 분기 ---
        rule_paths_in_pr = [
            f["path"] for f in changed if posixpath.basename(f["path"]) == RULE_FILENAME
        ]
        rule_changed = bool(rule_paths_in_pr)

        head_rules: dict[str, str] = {}
        base_rules: dict[str, str] = {}
        if rule_changed:
            # 변경됐으면 → changed_analyzer가 head 버전을 읽는다
            for p in rule_paths_in_pr:
                content = await gh.get_file_contents(p, head_sha)
                if content:
                    head_rules[p] = clip(content, cfg.max_file_chars, p)
        else:
            # 변경 안 됐으면 → base_analyzer가 기존 RULE.md를 읽는다.
            # 변경 파일들의 상위 디렉토리를 거슬러 올라가며 RULE.md를 찾는다.
            for rule_path in _candidate_rule_paths(c["path"] for c in changed):
                content = await gh.get_file_contents(rule_path, base_sha)
                if content:
                    base_rules[rule_path] = clip(content, cfg.max_file_chars, rule_path)

        # --- 변경 파일들의 base 원본 수집 (신규 파일 제외) ---
        base_files: dict[str, str] = {}
        total = 0
        for f in changed:
            if f["status"] == "added" or posixpath.basename(f["path"]) == RULE_FILENAME:
                continue
            if total >= cfg.max_base_total_chars:
                log.warning("base 파일 수집 상한 도달, 이후 파일 생략: %s", f["path"])
                break
            content = await gh.get_file_contents(f["path"], base_sha)
            if content is not None:
                content = clip(content, cfg.max_file_chars, f["path"])
                base_files[f["path"]] = content
                total += len(content)

        log.info(
            "PR #%s: 파일 %d개, RULE.md 변경=%s (head_rules=%d, base_rules=%d)",
            cfg.pr_number, len(changed), rule_changed, len(head_rules), len(base_rules),
        )
        return {
            "pr_title": pr.get("title", ""),
            "pr_body": pr.get("body") or "",
            "base_sha": base_sha,
            "head_sha": head_sha,
            "changed_files": changed,
            "annotated_diff": annotated_diff,
            "valid_lines": valid_lines,
            "rule_changed": rule_changed,
            "head_rules": head_rules,
            "base_rules": base_rules,
            "base_files": base_files,
        }

    # ---- node: changed_analyzer (병렬 1) --------------------------------

    async def changed_analyzer(state: ReviewState) -> ReviewState:
        rule_section = ""
        if state["rule_changed"]:
            rule_section = "\n\n## 이번 PR에서 변경된 RULE.md (head 버전)\n" + _join_files(
                state["head_rules"]
            )
        user = (
            f"# PR: {state['pr_title']}\n{state['pr_body']}\n"
            f"{rule_section}\n\n"
            f"## diff (라인번호 주석 포함)\n```diff\n{state['annotated_diff']}\n```"
        )
        analysis = await llm.chat(
            prompts.CHANGED_ANALYZER_SYSTEM.format(language=lang), user
        )
        log.info("changed_analyzer 완료 (%d자)", len(analysis))
        return {"changed_analysis": analysis}

    # ---- node: base_analyzer (병렬 2) ------------------------------------

    async def base_analyzer(state: ReviewState) -> ReviewState:
        rule_section = ""
        if not state["rule_changed"] and state["base_rules"]:
            rule_section = "\n\n## 기존 RULE.md (base 버전)\n" + _join_files(
                state["base_rules"]
            )
        changed_list = "\n".join(
            f"- {f['path']} ({f['status']})" for f in state["changed_files"]
        )
        user = (
            f"# PR: {state['pr_title']}\n\n"
            f"## 이번 PR에서 변경된 파일 목록\n{changed_list}\n"
            f"{rule_section}\n\n"
            f"## 변경 전(base) 원본 파일\n{_join_files(state['base_files']) or '(전부 신규 파일)'}"
        )
        analysis = await llm.chat(
            prompts.BASE_ANALYZER_SYSTEM.format(language=lang), user
        )
        log.info("base_analyzer 완료 (%d자)", len(analysis))
        return {"base_analysis": analysis}

    # ---- node: compare_reviewer ------------------------------------------

    async def compare_reviewer(state: ReviewState) -> ReviewState:
        user = (
            f"# PR: {state['pr_title']}\n\n"
            f"## 변경사항 분석 (에이전트 1)\n{state['changed_analysis']}\n\n"
            f"## 기존 코드 분석 (에이전트 2)\n{state['base_analysis']}\n\n"
            f"## diff (라인번호 주석 포함)\n```diff\n{state['annotated_diff']}\n```"
        )
        raw = await llm.chat(
            prompts.COMPARE_REVIEWER_SYSTEM.format(language=lang), user
        )
        try:
            review = _extract_json(raw)
        except (ValueError, json.JSONDecodeError):
            log.warning("리뷰 JSON 파싱 실패 — 복구 시도")
            repaired = await llm.chat(prompts.JSON_REPAIR_SYSTEM, raw, temperature=0.0)
            review = _extract_json(repaired)

        summary = str(review.get("summary", "")).strip()
        comments = review.get("inline_comments") or []
        comments = [c for c in comments if isinstance(c, dict)]
        ok, dropped = validate_comments(comments, state["valid_lines"])
        if dropped:
            log.warning("diff 밖 라인 지정 등으로 탈락한 인라인 코멘트 %d개", len(dropped))
        log.info("compare_reviewer 완료: 인라인 %d개 (탈락 %d개)", len(ok), len(dropped))
        return {
            "review_summary": summary,
            "inline_comments": ok,
            "dropped_comments": dropped,
        }

    # ---- node: post_summary (병렬 1) --------------------------------------

    async def post_summary(state: ReviewState) -> ReviewState:
        body = "## 🤖 자동 PR 리뷰\n\n" + state["review_summary"]
        if state["dropped_comments"]:
            extra = "\n".join(
                f"- `{c.get('path')}:{c.get('line')}` — {c.get('body')}"
                for c in state["dropped_comments"]
            )
            body += f"\n\n---\n### 기타 지적 (라인 특정 실패)\n{extra}"
        if cfg.dry_run:
            log.info("[DRY_RUN] PR 전체 코멘트:\n%s", body)
            return {"posted_summary": False}
        await gh.add_issue_comment(body)
        log.info("PR 전체 코멘트 등록 완료")
        return {"posted_summary": True}

    # ---- node: post_inline (병렬 2) ---------------------------------------

    async def post_inline(state: ReviewState) -> ReviewState:
        comments = [
            {
                "path": c["path"],
                "line": c["line"],
                "side": c["side"],
                "body": f"{SEVERITY_MARK.get(c.get('severity', 'info'), '🔵')} {c['body']}",
            }
            for c in state["inline_comments"]
        ]
        if not comments:
            log.info("인라인 코멘트 없음 — 건너뜀")
            return {"posted_inline": 0}
        if cfg.dry_run:
            for c in comments:
                log.info("[DRY_RUN] 인라인 %s:%s(%s)\n%s", c["path"], c["line"], c["side"], c["body"])
            return {"posted_inline": 0}
        added = await gh.post_inline_review(comments, body="🤖 자동 리뷰 인라인 코멘트")
        log.info("인라인 코멘트 %d/%d개 등록 완료", added, len(comments))
        return {"posted_inline": added}

    # ---- graph wiring -----------------------------------------------------

    g = StateGraph(ReviewState)
    g.add_node("fetch_pr", fetch_pr)
    g.add_node("changed_analyzer", changed_analyzer)
    g.add_node("base_analyzer", base_analyzer)
    g.add_node("compare_reviewer", compare_reviewer)
    g.add_node("post_summary", post_summary)
    g.add_node("post_inline", post_inline)

    g.add_edge(START, "fetch_pr")
    g.add_edge("fetch_pr", "changed_analyzer")        # ┐ 병렬
    g.add_edge("fetch_pr", "base_analyzer")           # ┘
    g.add_edge(["changed_analyzer", "base_analyzer"], "compare_reviewer")  # join
    g.add_edge("compare_reviewer", "post_summary")    # ┐ 병렬
    g.add_edge("compare_reviewer", "post_inline")     # ┘
    g.add_edge("post_summary", END)
    g.add_edge("post_inline", END)
    return g.compile()


# ---- helpers ----------------------------------------------------------------


def _candidate_rule_paths(changed_paths) -> list[str]:
    """변경 파일들의 모든 상위 디렉토리에서 RULE.md 후보 경로를 만든다.
    예: gitops/lcm-manila/helm/values.yaml
        → gitops/lcm-manila/helm/RULE.md, gitops/lcm-manila/RULE.md, gitops/RULE.md, RULE.md
    """
    candidates: list[str] = []
    seen: set[str] = set()
    for path in changed_paths:
        d = posixpath.dirname(path)
        while True:
            rule = posixpath.join(d, RULE_FILENAME) if d else RULE_FILENAME
            if rule not in seen:
                seen.add(rule)
                candidates.append(rule)
            if not d:
                break
            d = posixpath.dirname(d)
    return candidates


def _join_files(files: dict[str, str]) -> str:
    return "\n\n".join(f"### {path}\n```\n{content}\n```" for path, content in files.items())


def _extract_json(text: str) -> dict:
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("JSON 객체를 찾을 수 없음")
    return json.loads(text[start : end + 1])
