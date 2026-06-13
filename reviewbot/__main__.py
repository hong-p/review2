"""GitOps PR 리뷰봇 엔트리포인트.

환경변수 또는 CLI 파라미터로 설정 (CLI 인자 우선):
    python main.py --repo my-org/gitops --pr-number 123 \
        --github-token ghp_xxx --llm-base-url http://llm:8000/v1 --llm-model qwen2.5
전체 옵션: python main.py --help
"""
import asyncio
import logging
import sys

from .config import load_config
from .github_api import github_api
from .graph import build_graph
from .llm import LLM

log = logging.getLogger("review-bot")


async def run() -> int:
    cfg = load_config()
    logging.basicConfig(
        level=getattr(logging, cfg.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    log.info(
        "리뷰 시작: %s/%s PR #%d (dry_run=%s, log_level=%s)",
        cfg.owner, cfg.repo, cfg.pr_number, cfg.dry_run, cfg.log_level,
    )
    async with github_api(cfg) as gh:
        llm = LLM(cfg)
        graph = build_graph(gh, llm, cfg)
        result = await graph.ainvoke({})

    log.info(
        "리뷰 완료: 전체 코멘트=%s, 인라인 %d개, 기존 코멘트 동의 %d개 (탈락 %d개)",
        result.get("posted_summary"),
        result.get("posted_inline", 0),
        result.get("posted_agreements", 0),
        len(result.get("dropped_comments", [])),
    )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
