"""GitOps PR 리뷰봇 엔트리포인트.

Jenkins에서:
    GITHUB_TOKEN, GITHUB_REPOSITORY, PR_NUMBER, LLM_BASE_URL, LLM_MODEL 설정 후
    python main.py
"""
import asyncio
import logging
import sys

from config import load_config
from github_mcp import github_mcp
from graph import build_graph
from llm import LLM

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("review-bot")


async def run() -> int:
    cfg = load_config()
    log.info(
        "리뷰 시작: %s/%s PR #%d (dry_run=%s)",
        cfg.owner, cfg.repo, cfg.pr_number, cfg.dry_run,
    )
    async with github_mcp(cfg) as gh:
        llm = LLM(cfg)
        graph = build_graph(gh, llm, cfg)
        result = await graph.ainvoke({})

    log.info(
        "리뷰 완료: 전체 코멘트=%s, 인라인 코멘트=%d개 등록 (탈락 %d개)",
        result.get("posted_summary"),
        result.get("posted_inline", 0),
        len(result.get("dropped_comments", [])),
    )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
