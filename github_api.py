"""GitHub REST API 클라이언트 (httpx 직접 호출).

GitHub MCP(stdio/docker) 대신 REST API를 직접 부른다. 이유:
- 쓰는 기능이 PR 조회 + 코멘트 게시 몇 개뿐이라 MCP는 오버스펙
- docker/별도 프로세스 의존이 사내 환경에서 깨지기 쉬움
- GitHub Enterprise는 base URL(--github-api-url)만 바꾸면 됨

파일 읽기는 로컬 도구(tools.py)가 담당하므로 여기엔 없다.
메서드 시그니처는 기존 MCP 래퍼와 동일해 graph.py는 그대로 동작한다.
"""
import logging
from contextlib import asynccontextmanager

import httpx

from config import Config

log = logging.getLogger(__name__)


class GitHubAPI:
    def __init__(self, client: httpx.AsyncClient, cfg: Config):
        self.client = client
        self.owner = cfg.owner
        self.repo = cfg.repo
        self.pr = cfg.pr_number

    @property
    def _base(self) -> str:
        return f"/repos/{self.owner}/{self.repo}"

    # ---- read ----------------------------------------------------------

    async def get_pull_request(self) -> dict:
        r = await self.client.get(f"{self._base}/pulls/{self.pr}")
        r.raise_for_status()
        return r.json()

    async def get_pull_request_files(self) -> list[dict]:
        files: list[dict] = []
        page = 1
        while True:
            r = await self.client.get(
                f"{self._base}/pulls/{self.pr}/files",
                params={"per_page": 100, "page": page},
            )
            r.raise_for_status()
            batch = r.json()
            files.extend(batch)
            if len(batch) < 100:  # 마지막 페이지
                break
            page += 1
        return files

    async def get_pull_request_diff(self) -> str:
        r = await self.client.get(
            f"{self._base}/pulls/{self.pr}",
            headers={"Accept": "application/vnd.github.diff"},
        )
        r.raise_for_status()
        return r.text

    async def get_existing_comments(self) -> list[dict]:
        """PR 대화 코멘트 + 인라인 리뷰 코멘트. 조회 실패해도 빈 목록으로 진행."""
        out: list[dict] = []
        try:
            for c in await self._get_all(f"{self._base}/issues/{self.pr}/comments"):
                out.append({
                    "id": c.get("id"), "type": "issue",
                    "user": (c.get("user") or {}).get("login", ""),
                    "body": c.get("body", ""),
                })
        except Exception as e:
            log.warning("PR 대화 코멘트 조회 실패 (계속): %s", e)
        try:
            for c in await self._get_all(f"{self._base}/pulls/{self.pr}/comments"):
                out.append({
                    "id": c.get("id"), "type": "inline",
                    "user": (c.get("user") or {}).get("login", ""),
                    "path": c.get("path", ""),
                    "line": c.get("line") or c.get("original_line"),
                    "body": c.get("body", ""),
                })
        except Exception as e:
            log.warning("인라인 리뷰 코멘트 조회 실패 (계속): %s", e)
        return out

    async def _get_all(self, path: str) -> list[dict]:
        items: list[dict] = []
        page = 1
        while True:
            r = await self.client.get(path, params={"per_page": 100, "page": page})
            r.raise_for_status()
            batch = r.json()
            items.extend(batch)
            if len(batch) < 100:
                break
            page += 1
        return items

    # ---- write ---------------------------------------------------------

    async def add_issue_comment(self, body: str) -> None:
        r = await self.client.post(
            f"{self._base}/issues/{self.pr}/comments", json={"body": body}
        )
        r.raise_for_status()

    async def post_inline_review(self, comments: list[dict], body: str) -> int:
        """reviews API로 인라인 코멘트를 한 번에 등록. 반환: 등록된 코멘트 수.

        한 방 실패(422 등 — 한 코멘트의 line이 diff 밖이면 전체 거부)하면
        코멘트를 개별 리뷰로 쪼개 되는 것만 건진다.
        """
        payload = {
            "body": body,
            "event": "COMMENT",
            "comments": [_review_comment(c) for c in comments],
        }
        r = await self.client.post(f"{self._base}/pulls/{self.pr}/reviews", json=payload)
        if r.status_code < 300:
            return len(comments)

        log.warning("일괄 인라인 리뷰 실패(%s) — 개별 등록 시도: %s",
                    r.status_code, r.text[:200])
        posted = 0
        for i, c in enumerate(comments):
            one = {
                "body": body if i == 0 else "",
                "event": "COMMENT",
                "comments": [_review_comment(c)],
            }
            rr = await self.client.post(f"{self._base}/pulls/{self.pr}/reviews", json=one)
            if rr.status_code < 300:
                posted += 1
            else:
                log.warning("인라인 코멘트 등록 실패 %s:%s — %s",
                            c.get("path"), c.get("line"), rr.text[:120])
        return posted

    async def reply_to_review_comment(self, comment_id: int, body: str) -> None:
        """기존 인라인 코멘트 스레드에 답글. 구버전 GHE는 미지원일 수 있어 예외 발생."""
        r = await self.client.post(
            f"{self._base}/pulls/{self.pr}/comments/{comment_id}/replies",
            json={"body": body},
        )
        r.raise_for_status()


def _review_comment(c: dict) -> dict:
    return {
        "path": c["path"],
        "line": c["line"],
        "side": c.get("side", "RIGHT"),
        "body": c["body"],
    }


@asynccontextmanager
async def github_api(cfg: Config):
    headers = {
        "Authorization": f"Bearer {cfg.github_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    async with httpx.AsyncClient(
        base_url=cfg.github_api_url, headers=headers, timeout=30.0
    ) as client:
        yield GitHubAPI(client, cfg)
