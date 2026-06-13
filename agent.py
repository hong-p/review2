"""단일 에이전트의 tool use loop.

에이전트 = LLM이 도구(grep/read/glob 등)를 반복 호출하며 자기 영역을 리뷰하는 루프.

흐름:
  messages = [system(focus), user(시작 지시)]
  while 턴 < max_turns:
      msg = LLM(messages, tools)
      messages.append(msg)
      if msg에 tool_calls 없음 → 최종 발견사항이므로 종료
      각 tool_call 실행 → 결과를 messages에 append → 다음 턴
  턴 초과 시 → 도구 없이 결론 강제

핵심: 매 호출의 입력(messages)은 누적되지만, 한 턴에 보내는 도구 결과가 작아서
한 호출이 게이트웨이 타임아웃(60초) 안에 들어온다. 이게 큰-입력-한방 구조와의 차이.
"""
import json
import logging

import prompts
from llm import LLM
from tools import TOOL_SCHEMAS, ToolContext, execute_tool

log = logging.getLogger(__name__)


async def run_agent(llm: LLM, agent: dict, ctx: ToolContext, max_turns: int,
                    language: str, no_think: bool) -> dict:
    """에이전트 1개를 끝까지 돌리고 {name, findings}를 반환."""
    name = agent.get("name", "reviewer")
    focus = agent.get("focus", "PR 전반")
    files = agent.get("files") or [f["path"] for f in ctx.changed_files]

    messages = [
        {"role": "system", "content": prompts.AGENT_SYSTEM.format(focus=focus, language=language)},
        {"role": "user", "content": f"리뷰를 시작하라. 담당 변경 파일:\n" + "\n".join(f"- {p}" for p in files)},
    ]

    for turn in range(max_turns):
        msg = await llm.chat_with_tools(messages, TOOL_SCHEMAS, no_think=no_think)
        messages.append(_assistant_dict(msg))

        if not getattr(msg, "tool_calls", None):
            findings = (msg.content or "").strip() or "특이사항 없음"
            log.info("에이전트 '%s' 완료 (%d턴)", name, turn + 1)
            return {"name": name, "findings": findings}

        for tc in msg.tool_calls:
            result = _run_one_tool(tc, ctx)
            messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": result,
            })
        log.debug("에이전트 '%s' 턴 %d: 도구 %d개 실행", name, turn + 1, len(msg.tool_calls))

    # 턴 초과 → 도구 없이 결론 강제
    log.warning("에이전트 '%s' 최대 턴(%d) 도달 — 결론 강제", name, max_turns)
    messages.append({
        "role": "user",
        "content": "도구 사용을 멈추고, 지금까지 확인한 내용만으로 발견사항을 형식에 맞춰 출력하라.",
    })
    msg = await llm.chat_with_tools(messages, tools=[], no_think=no_think)
    findings = (msg.content or "").strip() or "(턴 초과로 리뷰 미완)"
    return {"name": name, "findings": findings}


def _run_one_tool(tc, ctx: ToolContext) -> str:
    name = tc.function.name
    try:
        args = json.loads(tc.function.arguments or "{}")
    except json.JSONDecodeError:
        return f"ERROR: 도구 인자가 올바른 JSON이 아니다: {tc.function.arguments!r}. 다시 호출하라."
    if not isinstance(args, dict):
        return "ERROR: 도구 인자는 JSON 객체여야 한다."
    return execute_tool(name, args, ctx)


def _assistant_dict(msg) -> dict:
    """openai message 객체를 다음 턴에 다시 보낼 dict로 직렬화."""
    d = {"role": "assistant", "content": msg.content or ""}
    if getattr(msg, "tool_calls", None):
        d["tool_calls"] = [
            {
                "id": tc.id,
                "type": "function",
                "function": {"name": tc.function.name, "arguments": tc.function.arguments},
            }
            for tc in msg.tool_calls
        ]
    return d
