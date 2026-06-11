"""GitHub MCP 서버(stdio) 클라이언트 래퍼.

공식 github/github-mcp-server 기준 툴 이름을 사용한다.
서버 버전에 따라 이름이 다르면 TOOLS 딕셔너리만 고치면 된다.
"""
import base64
import json
import logging
import os
from contextlib import asynccontextmanager

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from config import Config

log = logging.getLogger(__name__)

TOOLS = {
    "get_pr": "get_pull_request",
    "get_files": "get_pull_request_files",
    "get_diff": "get_pull_request_diff",
    "get_file": "get_file_contents",
    "issue_comment": "add_issue_comment",
    "issue_comments": "get_issue_comments",
    "pr_comments": "get_pull_request_comments",
    "review_start": "create_pending_pull_request_review",
    "review_add_comment": "add_comment_to_pending_review",
    "review_submit": "submit_pending_pull_request_review",
    # 기존 리뷰 코멘트 스레드에 답글. 서버 버전에 따라 미지원일 수 있음 (호출부에서 fallback)
    "reply_comment": "add_pull_request_review_comment",
}


class GitHubMCP:
    def __init__(self, session: ClientSession, cfg: Config):
        self.session = session
        self.cfg = cfg
        self._file_cache: dict[tuple[str, str], str | None] = {}

    # ---- low level ----------------------------------------------------

    async def call(self, key: str, **args) -> str:
        """툴 호출 후 텍스트 콘텐츠를 합쳐서 반환."""
        tool = TOOLS[key]
        args.setdefault("owner", self.cfg.owner)
        args.setdefault("repo", self.cfg.repo)
        result = await self.session.call_tool(tool, args)
        parts = []
        for item in result.content:
            if getattr(item, "text", None) is not None:
                parts.append(item.text)
            elif getattr(item, "resource", None) is not None and getattr(item.resource, "text", None):
                parts.append(item.resource.text)
        text = "\n".join(parts)
        if result.isError:
            raise RuntimeError(f"MCP tool {tool} failed: {text[:500]}")
        return text

    async def call_json(self, key: str, **args):
        return json.loads(await self.call(key, **args))

    # ---- read ----------------------------------------------------------

    async def get_pull_request(self) -> dict:
        return await self.call_json("get_pr", pullNumber=self.cfg.pr_number)

    async def get_pull_request_files(self) -> list[dict]:
        data = await self.call_json("get_files", pullNumber=self.cfg.pr_number)
        # 서버 버전에 따라 배열 또는 {files: []} 형태
        if isinstance(data, dict):
            data = data.get("files", [])
        return data

    async def get_pull_request_diff(self) -> str:
        return await self.call("get_diff", pullNumber=self.cfg.pr_number)

    async def get_file_contents(self, path: str, ref: str) -> str | None:
        """파일 내용. 없으면 None. (path, ref) 단위 캐시."""
        cache_key = (path, ref)
        if cache_key in self._file_cache:
            return self._file_cache[cache_key]
        try:
            raw = await self.call("get_file", path=path, ref=ref)
        except Exception as e:
            log.debug("get_file_contents miss %s@%s: %s", path, ref, e)
            self._file_cache[cache_key] = None
            return None
        content = _decode_file_payload(raw)
        self._file_cache[cache_key] = content
        return content

    async def get_existing_comments(self) -> list[dict]:
        """이미 달린 코멘트 전부 (PR 대화 코멘트 + 인라인 리뷰 코멘트).

        returns [{id, type: "issue"|"inline", user, path?, line?, body}]
        코멘트 조회 툴이 없는 서버 버전이면 빈 목록 (경고만 남김).
        """
        out: list[dict] = []
        try:
            data = await self.call_json("issue_comments", issue_number=self.cfg.pr_number)
            if isinstance(data, dict):
                data = data.get("comments", [])
            for c in data:
                out.append({
                    "id": c.get("id"),
                    "type": "issue",
                    "user": (c.get("user") or {}).get("login", ""),
                    "body": c.get("body", ""),
                })
        except Exception as e:
            log.warning("기존 PR 대화 코멘트 조회 실패 (계속 진행): %s", e)
        try:
            data = await self.call_json("pr_comments", pullNumber=self.cfg.pr_number)
            if isinstance(data, dict):
                data = data.get("comments", [])
            for c in data:
                out.append({
                    "id": c.get("id"),
                    "type": "inline",
                    "user": (c.get("user") or {}).get("login", ""),
                    "path": c.get("path", ""),
                    "line": c.get("line") or c.get("original_line"),
                    "body": c.get("body", ""),
                })
        except Exception as e:
            log.warning("기존 인라인 리뷰 코멘트 조회 실패 (계속 진행): %s", e)
        return out

    # ---- write ---------------------------------------------------------

    async def add_issue_comment(self, body: str):
        return await self.call(
            "issue_comment", issue_number=self.cfg.pr_number, body=body
        )

    async def reply_to_review_comment(self, comment_id: int, body: str):
        """기존 인라인 리뷰 코멘트 스레드에 답글. 미지원 서버면 예외 발생."""
        return await self.call(
            "reply_comment",
            pullNumber=self.cfg.pr_number,
            in_reply_to=comment_id,
            body=body,
        )

    async def post_inline_review(self, comments: list[dict], body: str) -> int:
        """pending review 생성 → 인라인 코멘트 추가 → COMMENT로 제출.

        returns 실제 등록된 코멘트 수
        """
        await self.call("review_start", pullNumber=self.cfg.pr_number)
        added = 0
        for c in comments:
            try:
                await self.call(
                    "review_add_comment",
                    pullNumber=self.cfg.pr_number,
                    path=c["path"],
                    line=c["line"],
                    side=c.get("side", "RIGHT"),
                    subjectType="LINE",
                    body=c["body"],
                )
                added += 1
            except Exception as e:
                log.warning("인라인 코멘트 등록 실패 %s:%s — %s", c.get("path"), c.get("line"), e)
        await self.call(
            "review_submit",
            pullNumber=self.cfg.pr_number,
            event="COMMENT",
            body=body,
        )
        return added


def _decode_file_payload(raw: str) -> str:
    """get_file_contents 결과가 평문이면 그대로, GitHub API JSON이면 base64 디코드."""
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return raw
    if isinstance(data, dict) and "content" in data:
        content = data["content"]
        if data.get("encoding") == "base64" or _looks_base64(content):
            try:
                return base64.b64decode(content).decode("utf-8", errors="replace")
            except Exception:
                return content
        return content
    return raw


def _looks_base64(s) -> bool:
    return isinstance(s, str) and len(s) > 0 and "\n" not in s.strip()[:80] and " " not in s.strip()[:80]


@asynccontextmanager
async def github_mcp(cfg: Config):
    command, *args = cfg.mcp_command
    params = StdioServerParameters(
        command=command,
        args=args,
        env={
            **os.environ,
            "GITHUB_PERSONAL_ACCESS_TOKEN": cfg.github_token,
        },
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            yield GitHubMCP(session, cfg)
