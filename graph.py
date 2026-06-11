"""LangGraph 파이프라인.

fetch_pr ──┬─> changed_analyzer ──┬─> compare_reviewer ──┬─> post_summary
           └─> base_analyzer    ──┘                      └─> post_inline
(analyzer 2개 병렬, 게시 2개 병렬)

대형 PR 처리: diff/base 파일이 호출당 예산(cfg.max_*)을 넘으면 파일 단위 배치로
쪼개서 LLM을 여러 번 호출하고 결과를 병합한다. diff 자체는 자르지 않는다.

중복 방지: 이미 PR에 달린 코멘트(봇/사람)를 reviewer에 전달한다. 같은 취지의
지적은 새로 달지 않고 기존 코멘트에 동의 답글(agreements)을 단다.
"""
import asyncio
import json
import logging
import posixpath
import re
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

import prompts
import rules
from config import Config
from diff_utils import pack_batches, parse_diff, split_diff_by_file, validate_comments
from github_mcp import GitHubMCP
from llm import LLM, clip

log = logging.getLogger(__name__)

RULE_FILENAME = "RULE.md"
SEVERITY_MARK = {"error": "🔴", "warn": "🟡", "info": "🔵"}
ANALYSIS_BUDGET = 30_000  # reviewer에 전달하는 analyzer 결과물 상한 (각각)


class ReviewState(TypedDict, total=False):
    # fetch_pr 결과
    pr_title: str
    pr_body: str
    base_sha: str
    head_sha: str
    changed_files: list[dict]      # [{path, status}]
    annotated_diff: str
    diff_by_file: dict[str, str]   # path → 해당 파일 diff (배치 처리용)
    valid_lines: dict              # {path: {"RIGHT": set, "LEFT": set}}
    rule_changed: bool
    head_rules: dict[str, str]     # PR에서 RULE.md가 변경된 경우 head 버전
    base_rules: dict[str, str]     # 변경 안 된 경우 base 버전
    base_files: dict[str, str]     # 변경된 파일들의 base 원본
    peer_map: dict[str, list[str]]  # 변경 파일 → 참고 환경 대응 파일 경로
    peer_files: dict[str, str]      # 참고 환경 대응 파일 내용 (변경되지 않은 파일)
    missing_peers: list[str]        # 참고 환경에 존재하지 않는 대응 파일
    existing_comments: list[dict]   # 이미 달린 코멘트 [{id, type, user, path?, line?, body}]
    # analyzer 결과 (병렬 — 서로 다른 키에 기록)
    changed_analysis: str
    base_analysis: str
    # reviewer 결과
    review_summary: str
    inline_comments: list[dict]
    dropped_comments: list[dict]
    agreements: list[dict]          # 기존 코멘트 동의 답글 [{comment_id, body}]
    # 게시 결과 (병렬 — 서로 다른 키에 기록)
    posted_summary: bool
    posted_inline: int
    posted_agreements: int


def build_graph(gh: GitHubMCP, llm: LLM, cfg: Config):
    lang = cfg.review_language

    # ---- node: fetch_pr -------------------------------------------------

    async def fetch_pr(state: ReviewState) -> ReviewState:
        pr = await gh.get_pull_request()
        files = await gh.get_pull_request_files()
        diff = await gh.get_pull_request_diff()  # 대형 PR도 전체 유지 (배치로 처리)
        existing_comments = await gh.get_existing_comments()

        base_sha = pr["base"]["sha"]
        head_sha = pr["head"]["sha"]
        changed = [
            {"path": f.get("filename") or f.get("path"), "status": f.get("status", "")}
            for f in files
        ]
        valid_lines, annotated_diff = parse_diff(diff)
        diff_by_file = split_diff_by_file(annotated_diff)

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

        # --- 참고 환경 교차 비교: RULE.md의 reference_environments 기반 ---
        rule_texts = list(head_rules.values()) + list(base_rules.values())
        env_groups = rules.parse_env_groups(rule_texts)
        peer_map = (
            rules.find_peer_paths([c["path"] for c in changed], env_groups)
            if env_groups
            else {}
        )
        peer_files: dict[str, str] = {}
        missing_peers: list[str] = []
        for src, peer_paths in peer_map.items():
            for p in peer_paths:
                if p in peer_files or p in missing_peers:
                    continue
                content = await gh.get_file_contents(p, head_sha)
                if content is None:
                    missing_peers.append(p)
                else:
                    peer_files[p] = clip(content, cfg.max_file_chars, p)
        if env_groups:
            log.info(
                "참고 환경 그룹 %s → 대응 파일 %d개 수집, %d개 없음",
                env_groups, len(peer_files), len(missing_peers),
            )

        # --- 변경 파일들의 base 원본 수집 (신규 파일 제외, 파일당 상한만 적용) ---
        base_files: dict[str, str] = {}
        for f in changed:
            if f["status"] == "added" or posixpath.basename(f["path"]) == RULE_FILENAME:
                continue
            content = await gh.get_file_contents(f["path"], base_sha)
            if content is not None:
                base_files[f["path"]] = clip(content, cfg.max_file_chars, f["path"])

        log.info(
            "PR #%s: 파일 %d개 (diff %d자), RULE.md 변경=%s, 기존 코멘트 %d개",
            cfg.pr_number, len(changed), len(diff), rule_changed, len(existing_comments),
        )
        return {
            "pr_title": pr.get("title", ""),
            "pr_body": pr.get("body") or "",
            "base_sha": base_sha,
            "head_sha": head_sha,
            "changed_files": changed,
            "annotated_diff": annotated_diff,
            "diff_by_file": diff_by_file,
            "valid_lines": valid_lines,
            "rule_changed": rule_changed,
            "head_rules": head_rules,
            "base_rules": base_rules,
            "base_files": base_files,
            "peer_map": peer_map,
            "peer_files": peer_files,
            "missing_peers": missing_peers,
            "existing_comments": existing_comments,
        }

    # ---- node: changed_analyzer (병렬 1) --------------------------------

    async def changed_analyzer(state: ReviewState) -> ReviewState:
        rule_section = ""
        if state["rule_changed"]:
            rule_section = "\n\n## 이번 PR에서 변경된 RULE.md (head 버전)\n" + _join_files(
                state["head_rules"]
            )
        system = prompts.CHANGED_ANALYZER_SYSTEM.format(language=lang)
        batches = pack_batches(state["diff_by_file"], cfg.max_diff_chars)

        async def one(batch: dict[str, str], idx: int) -> str:
            note = (
                f"\n(대형 PR: 파일 그룹 {idx + 1}/{len(batches)} — 이 그룹의 파일만 분석)\n"
                if len(batches) > 1 else ""
            )
            diff_text = clip("\n".join(batch.values()), cfg.max_diff_chars, "diff")
            user = (
                f"# PR: {state['pr_title']}\n{state['pr_body']}\n{note}"
                f"{rule_section}\n\n"
                f"## diff (라인번호 주석 포함)\n```diff\n{diff_text}\n```"
            )
            return await llm.chat(system, user)

        parts = await asyncio.gather(*(one(b, i) for i, b in enumerate(batches)))
        analysis = _merge_parts(parts)
        log.info("changed_analyzer 완료 (배치 %d개, %d자)", len(batches), len(analysis))
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
        peer_info = ""
        if state["peer_map"]:
            mapping = "\n".join(
                f"- {src}\n" + "\n".join(f"  - 비교 대상: {p}" for p in peers)
                for src, peers in state["peer_map"].items()
            )
            missing = ""
            if state["missing_peers"]:
                missing = "\n존재하지 않는 대응 파일 (환경 간 파일 구성 불일치 가능성):\n" + "\n".join(
                    f"- {p}" for p in state["missing_peers"]
                )
            peer_info = f"\n\n## 참고 환경 비교 매핑\n{mapping}\n{missing}"

        # base 원본과 참고 환경 파일을 합쳐서 배치 (접두어로 구분)
        items = {f"BASE::{p}": c for p, c in state["base_files"].items()}
        items.update({f"PEER::{p}": c for p, c in state["peer_files"].items()})
        system = prompts.BASE_ANALYZER_SYSTEM.format(language=lang)
        batches = pack_batches(items, cfg.max_base_total_chars)

        async def one(batch: dict[str, str], idx: int) -> str:
            note = (
                f"\n(대형 PR: 파일 그룹 {idx + 1}/{len(batches)} — 이 그룹의 파일만 분석)\n"
                if len(batches) > 1 else ""
            )
            base_part = {k[6:]: v for k, v in batch.items() if k.startswith("BASE::")}
            peer_part = {k[6:]: v for k, v in batch.items() if k.startswith("PEER::")}
            peer_section = ""
            if peer_part:
                peer_section = (
                    "\n\n## 참고 환경 대응 파일 (이번 PR에서 변경되지 않음)\n"
                    + _join_files(peer_part)
                )
            user = (
                f"# PR: {state['pr_title']}\n{note}\n"
                f"## 이번 PR에서 변경된 파일 목록\n{changed_list}\n"
                f"{rule_section}{peer_info}\n\n"
                f"## 변경 전(base) 원본 파일\n{_join_files(base_part) or '(전부 신규 파일)'}"
                f"{peer_section}"
            )
            return await llm.chat(system, user)

        parts = await asyncio.gather(*(one(b, i) for i, b in enumerate(batches)))
        analysis = _merge_parts(parts)
        log.info("base_analyzer 완료 (배치 %d개, %d자)", len(batches), len(analysis))
        return {"base_analysis": analysis}

    # ---- node: compare_reviewer ------------------------------------------

    async def compare_reviewer(state: ReviewState) -> ReviewState:
        system = prompts.COMPARE_REVIEWER_SYSTEM.format(language=lang)
        existing_section = ""
        if state["existing_comments"]:
            existing_section = (
                "\n\n## 이미 PR에 달려 있는 코멘트 (중복 지적 금지, 같은 취지면 agreements로)\n"
                + _format_existing_comments(state["existing_comments"], cfg.max_comments_chars)
            )
        analyses = (
            f"## 변경사항 분석 (에이전트 1)\n{clip(state['changed_analysis'], ANALYSIS_BUDGET, '분석1')}\n\n"
            f"## 기존 코드 분석 (에이전트 2)\n{clip(state['base_analysis'], ANALYSIS_BUDGET, '분석2')}"
        )
        batches = pack_batches(state["diff_by_file"], cfg.max_diff_chars)

        async def one(batch: dict[str, str], idx: int) -> dict:
            note = (
                f"\n(대형 PR: 파일 그룹 {idx + 1}/{len(batches)} — 이 그룹의 파일에 대해서만 지적)\n"
                if len(batches) > 1 else ""
            )
            diff_text = clip("\n".join(batch.values()), cfg.max_diff_chars, "diff")
            user = (
                f"# PR: {state['pr_title']}\n{note}\n"
                f"{analyses}{existing_section}\n\n"
                f"## diff (라인번호 주석 포함)\n```diff\n{diff_text}\n```"
            )
            raw = await llm.chat(system, user)
            try:
                return _extract_json(raw)
            except (ValueError, json.JSONDecodeError):
                log.warning("리뷰 JSON 파싱 실패 (배치 %d) — 복구 시도", idx + 1)
                repaired = await llm.chat(prompts.JSON_REPAIR_SYSTEM, raw, temperature=0.0)
                return _extract_json(repaired)

        results = await asyncio.gather(*(one(b, i) for i, b in enumerate(batches)))

        # 배치 결과 병합
        summaries = [str(r.get("summary", "")).strip() for r in results if r.get("summary")]
        if len(summaries) <= 1:
            summary = summaries[0] if summaries else ""
        else:
            summary = await llm.chat(
                prompts.MERGE_SUMMARY_SYSTEM.format(language=lang),
                "\n\n---\n\n".join(summaries),
            )
        # 배치 간 완전 동일한 코멘트 중복 제거
        comments: list[dict] = []
        seen_comments: set = set()
        for r in results:
            for c in r.get("inline_comments") or []:
                if not isinstance(c, dict):
                    continue
                key = (c.get("path"), c.get("line"),
                       str(c.get("side", "RIGHT")).upper(), c.get("body"))
                if key not in seen_comments:
                    seen_comments.add(key)
                    comments.append(c)
        ok, dropped = validate_comments(comments, state["valid_lines"])
        if dropped:
            log.warning("diff 밖 라인 지정 등으로 탈락한 인라인 코멘트 %d개", len(dropped))

        # agreements 병합: 실재하는 comment_id만, id당 하나만
        existing_ids = {c["id"]: c for c in state["existing_comments"]}
        agreements: list[dict] = []
        seen_ids: set = set()
        for r in results:
            for a in r.get("agreements") or []:
                cid = a.get("comment_id") if isinstance(a, dict) else None
                if cid in existing_ids and cid not in seen_ids and a.get("body"):
                    seen_ids.add(cid)
                    agreements.append({"comment_id": cid, "body": str(a["body"])})

        log.info(
            "compare_reviewer 완료 (배치 %d개): 인라인 %d개 (탈락 %d개), 기존 코멘트 동의 %d개",
            len(batches), len(ok), len(dropped), len(agreements),
        )
        return {
            "review_summary": summary,
            "inline_comments": ok,
            "dropped_comments": dropped,
            "agreements": agreements,
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
        """기존 코멘트와 중복인 지적 → 해당 코멘트에 동의 답글.

        인라인 코멘트 스레드에는 답글을 시도하고, 답글 미지원 서버이거나
        대화(issue) 코멘트면 원문을 인용한 일반 코멘트로 fallback.
        """
        if not state["agreements"]:
            return 0
        by_id = {c["id"]: c for c in state["existing_comments"]}
        posted = 0
        fallbacks: list[str] = []
        for a in state["agreements"]:
            target = by_id[a["comment_id"]]
            body = f"🤖 {a['body']}"
            if cfg.dry_run:
                log.info("[DRY_RUN] 기존 코멘트(id=%s, @%s)에 동의 답글:\n%s",
                         a["comment_id"], target.get("user"), body)
                continue
            if target["type"] == "inline":
                try:
                    await gh.reply_to_review_comment(a["comment_id"], body)
                    posted += 1
                    continue
                except Exception as e:
                    log.warning("답글 등록 실패 (id=%s), 일반 코멘트로 대체: %s", a["comment_id"], e)
            quote = target.get("body", "").replace("\n", " ")[:120]
            fallbacks.append(f"> @{target.get('user')}: {quote}\n\n{body}")
        if fallbacks:
            await gh.add_issue_comment("\n\n---\n\n".join(fallbacks))
            posted += len(fallbacks)
        if posted:
            log.info("기존 코멘트 동의 %d개 등록 완료", posted)
        return posted

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


def _merge_parts(parts) -> str:
    parts = [p for p in parts if p]
    if len(parts) == 1:
        return parts[0]
    return "\n\n".join(f"### 파일 그룹 {i + 1}\n{p}" for i, p in enumerate(parts))


def _format_existing_comments(comments: list[dict], limit: int) -> str:
    lines = []
    for c in comments:
        loc = f" {c.get('path')}:{c.get('line')}" if c["type"] == "inline" else ""
        body = (c.get("body") or "").replace("\n", " ")[:300]
        lines.append(f"- [id={c['id']}] ({c['type']}{loc}, @{c.get('user', '')}) {body}")
    return clip("\n".join(lines), limit, "기존 코멘트")


def _extract_json(text: str) -> dict:
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("JSON 객체를 찾을 수 없음")
    return json.loads(text[start : end + 1])
