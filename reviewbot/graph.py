"""LangGraph 파이프라인 — 동적 멀티에이전트 tool use loop.

fetch_pr → planner ─(Send fan-out)→ [agent × N 병렬] → aggregator ─┬→ post_summary
                                     각자 tool use loop              └→ post_inline

- planner: 변경 파일을 보고 LLM이 에이전트를 몇 개로 나눌지 동적 결정
- agent:   각자 grep/read/glob 도구로 레포를 탐색하며 자기 영역 리뷰 (agent.py)
- aggregator: 에이전트 발견사항 + 기존 코멘트 종합 → 최종 리뷰 JSON

도구는 로컬 체크아웃(cfg.repo_dir)에서 실행된다. GitHub MCP는 PR 메타 조회와 게시에만 쓴다.
"""
import asyncio
import json
import logging
import operator
import os
import posixpath
import re
from typing import Annotated, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from . import prompts
from .agent import run_agent
from .config import Config
from .diff_utils import parse_diff, split_diff_by_file, validate_comments
from .env_rules import build_directive, parse_environment_checks, resolve_comparisons
from .github_api import GitHubAPI
from .llm import LLM, clip
from .tools import ToolContext, build_repo_tree

RULE_FILENAME = "REVIEW_RULE.md"

log = logging.getLogger(__name__)

SEVERITY_MARK = {"error": "🔴", "warn": "🟡", "info": "🔵"}


class ReviewState(TypedDict, total=False):
    pr_title: str
    pr_body: str
    changed_files: list[dict]
    repo_tree: str
    team_rules: str
    valid_lines: dict
    existing_comments: list[dict]
    agents: list[dict]                              # planner 결과
    agent_findings: Annotated[list[dict], operator.add]  # 각 agent가 append (reducer)
    review_summary: str
    inline_comments: list[dict]
    dropped_comments: list[dict]
    agreements: list[dict]
    posted_summary: bool
    posted_inline: int
    posted_agreements: int


def build_graph(gh: GitHubAPI, llm: LLM, cfg: Config):
    lang = cfg.review_language
    ctx_holder: dict[str, ToolContext] = {}      # fetch_pr가 채워 agent 노드와 공유
    agent_sem = asyncio.Semaphore(cfg.agent_concurrency)

    # ---- fetch_pr -------------------------------------------------------

    async def fetch_pr(state: ReviewState) -> ReviewState:
        pr = await gh.get_pull_request()
        files = await gh.get_pull_request_files()
        diff = await gh.get_pull_request_diff()
        existing = await gh.get_existing_comments()

        changed = [
            {"path": f.get("filename") or f.get("path"), "status": f.get("status", "")}
            for f in files
        ]
        valid_lines, annotated_diff = parse_diff(diff)
        diff_by_file = split_diff_by_file(annotated_diff)

        ctx_holder["ctx"] = ToolContext(
            repo_dir=cfg.repo_dir,
            diff_by_file=diff_by_file,
            changed_files=changed,
            max_tool_result_chars=cfg.max_tool_result_chars,
            max_file_chars=cfg.max_file_chars,
            max_diff_chars=cfg.max_diff_chars,
        )
        repo_tree = build_repo_tree(cfg.repo_dir, cfg.max_tree_depth, cfg.max_tree_chars)

        # REVIEW_RULE.md: environment_checks로 비교 대상 산출 + 리뷰 포맷 등 전체 텍스트는
        # aggregator가 최종 리뷰 형식/톤에 반영하도록 보관
        rule_texts = _read_rule_files(cfg.repo_dir, changed)
        team_rules = clip("\n\n".join(rule_texts), cfg.max_rule_chars, "룰") if rule_texts else ""
        checks = parse_environment_checks(rule_texts)
        comparisons, peer_paths, unresolved = resolve_comparisons(
            [c["path"] for c in changed], checks, cfg.repo_dir,
        )
        env_directive = build_directive(comparisons, peer_paths, unresolved)
        ctx_holder["env_directive"] = env_directive

        log.info(
            "PR #%s: 파일 %d개, 기존 코멘트 %d개, 환경 비교 규칙 %d개 → 비교환경 %s%s",
            cfg.pr_number, len(changed), len(existing), len(checks),
            {k: v for k, v in comparisons.items()} or "(없음)",
            f" (LLM 보완 필요: {unresolved})" if unresolved else "",
        )
        return {
            "pr_title": pr.get("title", ""),
            "pr_body": pr.get("body") or "",
            "changed_files": changed,
            "repo_tree": repo_tree,
            "team_rules": team_rules,
            "valid_lines": valid_lines,
            "existing_comments": existing,
        }

    # ---- planner --------------------------------------------------------

    async def planner(state: ReviewState) -> ReviewState:
        file_list = "\n".join(
            f"- {f['path']} ({f['status']})" for f in state["changed_files"]
        )
        directive = ctx_holder.get("env_directive", "")
        directive_section = f"\n\n{directive}" if directive else ""
        user = (
            f"# PR: {state['pr_title']}\n{state['pr_body'][:1000]}\n\n"
            f"## 레포 디렉토리 구조\n{state['repo_tree']}\n\n"
            f"## 변경된 파일 ({len(state['changed_files'])}개)\n{file_list}"
            f"{directive_section}"
        )
        raw = await llm.chat(
            prompts.PLANNER_SYSTEM.format(max_agents=cfg.max_agents), user,
            no_think=True, tag="planner",
        )
        agents = _parse_agents(raw, state["changed_files"], cfg.max_agents)
        log.info("planner: 에이전트 %d개 — %s", len(agents), [a["name"] for a in agents])
        return {"agents": agents}

    def route_to_agents(state: ReviewState):
        # 동적 fan-out: 에이전트마다 agent 노드를 하나씩 Send
        return [Send("agent", {"agent_spec": a}) for a in state["agents"]]

    # ---- agent (병렬, 각자 tool use loop) -------------------------------

    async def agent_node(payload: dict) -> ReviewState:
        ctx = ctx_holder["ctx"]
        async with agent_sem:  # 단일 GPU면 cfg.agent_concurrency=1로 직렬화
            result = await run_agent(
                llm, payload["agent_spec"], ctx,
                max_turns=cfg.max_turns, language=lang, no_think=cfg.no_think,
                env_hint=ctx_holder.get("env_directive", ""),
            )
        return {"agent_findings": [result]}

    # ---- aggregator -----------------------------------------------------

    async def aggregator(state: ReviewState) -> ReviewState:
        findings_text = "\n\n".join(
            f"## 에이전트: {f['name']}\n{f['findings']}" for f in state["agent_findings"]
        )
        existing_section = ""
        if state["existing_comments"]:
            existing_section = (
                "\n\n## 이미 PR에 달린 코멘트 (중복 지적 금지, 같은 취지면 agreements로)\n"
                + _format_existing(state["existing_comments"], cfg.max_comments_chars)
            )
        rules_section = ""
        if state.get("team_rules"):
            rules_section = (
                "\n\n## 팀 리뷰 규칙 (리뷰 포맷·톤·심각도 기준을 여기에 맞춰라)\n"
                + state["team_rules"]
            )
        user = f"# 에이전트 발견사항\n{findings_text}{existing_section}{rules_section}"
        raw = await llm.chat(
            prompts.AGGREGATOR_SYSTEM.format(language=lang), user,
            no_think=cfg.no_think, tag="aggregator",
        )
        try:
            review = _extract_json(raw)
        except (ValueError, json.JSONDecodeError):
            log.warning("리뷰 JSON 파싱 실패 — 복구 시도")
            repaired = await llm.chat(prompts.JSON_REPAIR_SYSTEM, raw, temperature=0.0,
                                      no_think=True, tag="aggregator-json-repair")
            review = _extract_json(repaired)

        summary = str(review.get("summary", "")).strip()
        comments = _normalize_comments(review.get("inline_comments") or [])
        # diff에 있는 라인 → 인라인 코멘트, diff 밖 라인 → 통합리뷰(요약 하단)로 분리
        ok, dropped = validate_comments(comments, state["valid_lines"])
        for c in dropped:
            log.info("[aggregator] diff 밖 라인이라 인라인 불가 → 통합리뷰로 이동: %s:%s",
                     c.get("path"), c.get("line"))
        agreements = _normalize_agreements(review.get("agreements") or [], state["existing_comments"])
        log.info(
            "[aggregator] 인라인 %d개 / 통합리뷰로 이동 %d개 / 기존 코멘트 동의 %d개",
            len(ok), len(dropped), len(agreements),
        )
        return {
            "review_summary": summary,
            "inline_comments": ok,
            "dropped_comments": dropped,
            "agreements": agreements,
        }

    # ---- post_summary (병렬 1) ------------------------------------------

    async def post_summary(state: ReviewState) -> ReviewState:
        body = "## 🤖 자동 PR 리뷰\n\n" + (state["review_summary"] or "특이사항 없음")
        if state["dropped_comments"]:
            extra = "\n".join(
                f"- `{c.get('path')}:{c.get('line')}` — {c.get('body')}"
                for c in state["dropped_comments"]
            )
            body += f"\n\n---\n### 추가 지적 (diff에 없는 라인이라 인라인 불가)\n{extra}"
        if cfg.dry_run:
            log.info("[DRY_RUN] PR 전체 코멘트:\n%s", body)
            return {"posted_summary": False}
        await gh.add_issue_comment(body)
        log.info("PR 전체 코멘트 등록 완료")
        return {"posted_summary": True}

    # ---- post_inline (병렬 2) -------------------------------------------

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
        posted = 0
        if cfg.dry_run:
            for c in comments:
                log.info("[DRY_RUN] 인라인 %s:%s(%s)\n%s", c["path"], c["line"], c["side"], c["body"])
        elif comments:
            posted = await gh.post_inline_review(comments, body="🤖 자동 리뷰 인라인 코멘트")
            log.info("인라인 코멘트 %d/%d개 등록 완료", posted, len(comments))
        else:
            log.info("인라인 코멘트 없음 — 건너뜀")

        posted_agreements = await _post_agreements(state)
        return {"posted_inline": posted, "posted_agreements": posted_agreements}

    async def _post_agreements(state: ReviewState) -> int:
        if not state["agreements"]:
            return 0
        by_id = {c["id"]: c for c in state["existing_comments"]}
        posted = 0
        fallbacks: list[str] = []
        for a in state["agreements"]:
            target = by_id.get(a["comment_id"])
            if target is None:
                continue
            body = f"🤖 {a['body']}"
            if cfg.dry_run:
                log.info("[DRY_RUN] 기존 코멘트(id=%s, @%s) 동의 답글:\n%s",
                         a["comment_id"], target.get("user"), body)
                continue
            if target["type"] == "inline":
                try:
                    await gh.reply_to_review_comment(a["comment_id"], body)
                    posted += 1
                    continue
                except Exception as e:
                    log.warning("답글 등록 실패 (id=%s), 일반 코멘트로 대체: %s", a["comment_id"], e)
            quote = (target.get("body") or "").replace("\n", " ")[:120]
            fallbacks.append(f"> @{target.get('user')}: {quote}\n\n{body}")
        if fallbacks and not cfg.dry_run:
            await gh.add_issue_comment("\n\n---\n\n".join(fallbacks))
            posted += len(fallbacks)
        if posted:
            log.info("기존 코멘트 동의 %d개 등록 완료", posted)
        return posted

    # ---- wiring ---------------------------------------------------------

    g = StateGraph(ReviewState)
    g.add_node("fetch_pr", fetch_pr)
    g.add_node("planner", planner)
    g.add_node("agent", agent_node)
    g.add_node("aggregator", aggregator)
    g.add_node("post_summary", post_summary)
    g.add_node("post_inline", post_inline)

    g.add_edge(START, "fetch_pr")
    g.add_edge("fetch_pr", "planner")
    g.add_conditional_edges("planner", route_to_agents, ["agent"])  # 동적 fan-out
    g.add_edge("agent", "aggregator")                                # fan-in
    g.add_edge("aggregator", "post_summary")
    g.add_edge("aggregator", "post_inline")
    g.add_edge("post_summary", END)
    g.add_edge("post_inline", END)
    return g.compile()


# ---- helpers ----------------------------------------------------------------


def _parse_agents(raw: str, changed_files: list[dict], max_agents: int) -> list[dict]:
    """planner 출력 파싱. 실패하면 단일 에이전트로 fallback."""
    try:
        data = _extract_json(raw)
        agents = data.get("agents") or []
        agents = [a for a in agents if isinstance(a, dict) and a.get("focus")][:max_agents]
    except (ValueError, json.JSONDecodeError):
        agents = []
    if not agents:
        agents = [{
            "name": "reviewer",
            "focus": "이 PR의 모든 변경을 리뷰한다 (룰 위반, 환경 불일치, 누락된 연관 설정 포함).",
            "files": [f["path"] for f in changed_files],
        }]
    return agents


def _read_rule_files(repo_dir: str, changed: list[dict]) -> list[str]:
    """변경 파일들의 상위 디렉토리에서 REVIEW_RULE.md를 찾아 로컬에서 읽는다.
    (서비스마다 다를 수 있어 변경 파일 경로별로 거슬러 올라가며 수집)
    """
    seen: set[str] = set()
    texts: list[str] = []
    for f in changed:
        d = posixpath.dirname(f["path"])
        while True:
            rule = posixpath.join(d, RULE_FILENAME) if d else RULE_FILENAME
            if rule not in seen:
                seen.add(rule)
                full = os.path.join(repo_dir, rule)
                if os.path.isfile(full):
                    with open(full, encoding="utf-8", errors="replace") as fh:
                        texts.append(fh.read())
            if not d:
                break
            d = posixpath.dirname(d)
    return texts


def _format_existing(comments: list[dict], limit: int) -> str:
    lines = []
    for c in comments:
        loc = f" {c.get('path')}:{c.get('line')}" if c["type"] == "inline" else ""
        body = (c.get("body") or "").replace("\n", " ")[:300]
        lines.append(f"- [id={c['id']}] ({c['type']}{loc}, @{c.get('user', '')}) {body}")
    return clip("\n".join(lines), limit, "기존 코멘트")


def _normalize_comments(comments: list) -> list[dict]:
    """LLM이 line을 문자열로 주거나 형 오류를 낸 경우 보정."""
    out = []
    for c in comments:
        if not isinstance(c, dict):
            continue
        line = c.get("line")
        if isinstance(line, str) and line.strip().isdigit():
            c["line"] = int(line.strip())
        out.append(c)
    return out


def _normalize_agreements(agreements: list, existing: list[dict]) -> list[dict]:
    existing_ids = {c["id"] for c in existing}
    out: list[dict] = []
    seen: set = set()
    for a in agreements:
        if not isinstance(a, dict):
            continue
        cid = a.get("comment_id")
        if isinstance(cid, str) and cid.strip().isdigit():
            cid = int(cid.strip())
        if cid in existing_ids and cid not in seen and a.get("body"):
            seen.add(cid)
            out.append({"comment_id": cid, "body": str(a["body"])})
    return out


def _extract_json(text: str) -> dict:
    """JSON 추출. 전체를 감싼 코드펜스만 벗기고(summary 내부 펜스는 보존),
    문자열 내 raw 개행도 허용(strict=False)."""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*\n?", "", text)
        text = re.sub(r"\n?```\s*$", "", text)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("JSON 객체를 찾을 수 없음")
    return json.loads(text[start : end + 1], strict=False)
