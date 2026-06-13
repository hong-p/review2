"""GitHub REST API 클라이언트 — httpx MockTransport."""
import json

import httpx
import pytest
from conftest import base_cfg

from reviewbot.github_api import GitHubAPI


def _client(handler):
    return httpx.AsyncClient(
        base_url="https://api.github.com",
        headers={"Authorization": "Bearer t"},
        transport=httpx.MockTransport(handler),
    )


async def test_get_pr_files_pagination():
    def handler(req):
        page = req.url.params.get("page")
        if page == "1":
            return httpx.Response(200, json=[{"filename": f"f{i}.yaml", "status": "modified"} for i in range(100)])
        return httpx.Response(200, json=[{"filename": "last.yaml", "status": "added"}])
    async with _client(handler) as c:
        gh = GitHubAPI(c, base_cfg(pr_number=7))
        files = await gh.get_pull_request_files()
    assert len(files) == 101  # 100 + 다음 페이지 1


async def test_get_diff_uses_diff_accept_header():
    seen = {}
    def handler(req):
        seen["accept"] = req.headers.get("accept")
        return httpx.Response(200, text="diff --git a/x b/x\n")
    async with _client(handler) as c:
        gh = GitHubAPI(c, base_cfg(pr_number=7))
        diff = await gh.get_pull_request_diff()
    assert "diff --git" in diff and "diff" in seen["accept"]


async def test_existing_comments_issue_and_inline():
    def handler(req):
        if "/issues/" in req.url.path:
            return httpx.Response(200, json=[{"id": 11, "user": {"login": "bot"}, "body": "이전"}])
        return httpx.Response(200, json=[{"id": 22, "user": {"login": "kim"}, "path": "x", "line": 1, "body": "인라인"}])
    async with _client(handler) as c:
        gh = GitHubAPI(c, base_cfg(pr_number=7))
        comments = await gh.get_existing_comments()
    types = {c["type"] for c in comments}
    assert types == {"issue", "inline"} and len(comments) == 2


async def test_post_inline_review_batch_success():
    def handler(req):
        return httpx.Response(200, json={"id": 1})
    async with _client(handler) as c:
        gh = GitHubAPI(c, base_cfg(pr_number=7))
        n = await gh.post_inline_review([{"path": "x", "line": 1, "side": "RIGHT", "body": "a"}], "r")
    assert n == 1


async def test_post_inline_review_fallback_on_422():
    """일괄 실패(line 999 포함) → 개별 재시도, 유효한 것만 등록."""
    def handler(req):
        body = json.loads(req.content)
        if any(c["line"] == 999 for c in body["comments"]):
            return httpx.Response(422, json={"message": "line invalid"})
        return httpx.Response(200, json={"id": 1})
    async with _client(handler) as c:
        gh = GitHubAPI(c, base_cfg(pr_number=7))
        n = await gh.post_inline_review([
            {"path": "x", "line": 1, "side": "RIGHT", "body": "a"},
            {"path": "x", "line": 999, "side": "RIGHT", "body": "b"},
        ], "r")
    assert n == 1  # line 1만 등록, 999 탈락


async def test_reply_to_review_comment():
    seen = {}
    def handler(req):
        seen["path"] = req.url.path
        return httpx.Response(201, json={"id": 5})
    async with _client(handler) as c:
        gh = GitHubAPI(c, base_cfg(pr_number=7))
        await gh.reply_to_review_comment(22, "동의")
    assert "/comments/22/replies" in seen["path"]
