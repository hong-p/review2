"""LLM 래퍼 — 디버그 로깅, tag, no_think."""
import logging
from types import SimpleNamespace

from conftest import base_cfg

from llm import LLM


class _FakeCompletions:
    def __init__(self, content="응답 내용", with_tool=False):
        self.content = content
        self.with_tool = with_tool
        self.last_kwargs = None

    async def create(self, **kw):
        self.last_kwargs = kw
        tcs = None
        if self.with_tool:
            tcs = [SimpleNamespace(id="c1", function=SimpleNamespace(name="grep", arguments='{"pattern":"x"}'))]
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=self.content, tool_calls=tcs))])


def _llm(comp, **cfg_kw):
    llm = LLM(base_cfg(**cfg_kw))
    llm.client = SimpleNamespace(chat=SimpleNamespace(completions=comp))
    return llm


async def test_debug_logs_request_and_response(caplog):
    comp = _FakeCompletions(content="이것은 응답")
    llm = _llm(comp, log_level="DEBUG")
    with caplog.at_level(logging.DEBUG, logger="llm"):
        await llm.chat("시스템", "유저질문입니다", tag="planner")
    text = caplog.text
    assert "[planner] → LLM 요청" in text and "[planner] ← LLM 응답" in text
    assert "유저질문입니다" in text and "이것은 응답" in text


async def test_info_level_hides_request_body(caplog):
    llm = _llm(_FakeCompletions(), log_level="INFO")
    with caplog.at_level(logging.INFO, logger="llm"):
        await llm.chat("시스템", "유저", tag="planner")
    assert "→ LLM 요청" not in caplog.text


async def test_tool_calls_logged_with_tag(caplog):
    comp = _FakeCompletions(with_tool=True)
    llm = _llm(comp, log_level="DEBUG")
    with caplog.at_level(logging.DEBUG, logger="llm"):
        await llm.chat_with_tools(
            [{"role": "system", "content": "S"}, {"role": "user", "content": "분석"}],
            tools=[{"type": "function"}], tag="agent[helm] turn 2",
        )
    assert "agent[helm] turn 2" in caplog.text and "grep" in caplog.text


async def test_no_think_sets_extra_body():
    comp = _FakeCompletions()
    llm = _llm(comp)
    await llm.chat("S", "U", no_think=True)
    assert comp.last_kwargs["extra_body"] == {"chat_template_kwargs": {"enable_thinking": False}}
    await llm.chat("S", "U", no_think=False)
    assert comp.last_kwargs["extra_body"] == {}


async def test_empty_tools_omits_tool_params():
    """턴 초과 시 tools=[]로 호출하면 tools/tool_choice를 안 보낸다."""
    comp = _FakeCompletions()
    llm = _llm(comp)
    await llm.chat_with_tools([{"role": "user", "content": "결론"}], tools=[])
    assert "tools" not in comp.last_kwargs and "tool_choice" not in comp.last_kwargs
