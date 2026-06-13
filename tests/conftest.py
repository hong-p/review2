"""공통 테스트 헬퍼 — Fake LLM/GitHub, 임시 레포, Config 빌더."""
import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from reviewbot.config import Config


# ---- openai message 흉내 -----------------------------------------------------


class FakeToolCall:
    def __init__(self, id, name, args):
        self.id = id
        self.function = SimpleNamespace(name=name, arguments=json.dumps(args))


class FakeMsg:
    def __init__(self, content=None, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls


# ---- 임시 레포 ---------------------------------------------------------------


def make_repo(files: dict[str, str]) -> str:
    """{상대경로: 내용} 으로 임시 레포 디렉토리를 만든다."""
    d = tempfile.mkdtemp()
    for rel, content in files.items():
        p = Path(d) / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    return d


def base_cfg(repo_dir: str = ".", **overrides) -> Config:
    cfg = dict(
        github_token="t", owner="o", repo="r", pr_number=1,
        llm_base_url="x", llm_model="m",
        repo_dir=repo_dir, dry_run=True, agent_concurrency=1,
    )
    cfg.update(overrides)
    return Config(**cfg)


# ---- Fake GitHub (github_api.GitHubAPI와 동일 인터페이스) --------------------


class FakeGH:
    def __init__(self, files=None, diff="", pr=None, existing=None):
        self.files = files or []
        self.diff = diff
        self.pr = pr or {"title": "t", "body": "", "base": {"sha": "b"}, "head": {"sha": "h"}}
        self.existing = existing or []
        self.issue_comments: list[str] = []
        self.inline_comments: list = []
        self.replies: list = []

    async def get_pull_request(self):
        return self.pr

    async def get_pull_request_files(self):
        return self.files

    async def get_pull_request_diff(self):
        return self.diff

    async def get_existing_comments(self):
        return self.existing

    async def add_issue_comment(self, body):
        self.issue_comments.append(body)

    async def post_inline_review(self, comments, body):
        self.inline_comments = comments
        return len(comments)

    async def reply_to_review_comment(self, comment_id, body):
        self.replies.append((comment_id, body))


class ScriptedLLM:
    """planner/aggregator 응답과 에이전트 동작을 주입하는 가짜 LLM.

    planner/aggregator: JSON 문자열 또는 callable(user)->str
    agent_fn: callable(messages)->FakeMsg (기본은 도구 없이 즉시 발견사항 반환)
    """
    def __init__(self, planner, aggregator='{"summary":"ok","inline_comments":[],"agreements":[]}',
                 agent_fn=None):
        self.planner = planner
        self.aggregator = aggregator
        self.agent_fn = agent_fn
        self.planner_users: list[str] = []
        self.agg_systems: list[str] = []
        self.agg_users: list[str] = []
        self.agent_starts: list[str] = []  # 각 에이전트의 시작 user 메시지

    async def chat(self, system, user, temperature=0.2, no_think=None, tag="chat"):
        if "작업 분배" in system:
            self.planner_users.append(user)
            return self.planner(user) if callable(self.planner) else self.planner
        if "최종 리뷰" in system:
            self.agg_systems.append(system)
            self.agg_users.append(user)
            return self.aggregator(user) if callable(self.aggregator) else self.aggregator
        return "{}"

    async def chat_with_tools(self, messages, tools, temperature=0.2, no_think=None, tag="agent"):
        if not any(m.get("role") == "assistant" for m in messages):
            self.agent_starts.append(messages[1]["content"])  # 첫 턴 = 시작 메시지
        if self.agent_fn:
            return self.agent_fn(messages)
        return FakeMsg(content="특이사항 없음")


@pytest.fixture
def fake_msg():
    return FakeMsg


@pytest.fixture
def fake_tool_call():
    return FakeToolCall
