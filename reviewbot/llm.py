"""로컬 LLM (OpenAI-compatible 엔드포인트) 래퍼.

로컬 LLM은 느릴 수 있으므로:
- 호출당 타임아웃을 길게 (기본 600초, --llm-timeout)
- 타임아웃/연결 오류/일시 오류는 SDK가 백오프와 함께 자동 재시도 (--llm-retries)
- 동시 요청 수 제한 (--llm-concurrency)

로깅:
- 모든 호출에 tag(예: "planner", "agent[helm] turn 2", "aggregator")를 붙여
  어떤 주체가 몇 번째 loop에서 호출했는지 로그로 추적한다.
- DEBUG 레벨이면 요청 메시지와 응답(도구 호출 포함) 전문을 출력한다 (--debug).
- INFO 레벨이면 호출 요약(tag, 소요시간, 도구 호출 수)만.

두 호출 방식:
- chat(): 단발 system+user → 텍스트 (planner, aggregator용)
- chat_with_tools(): 대화 누적 messages + tools → tool_calls 또는 텍스트 (tool use loop용)
"""
import asyncio
import logging
import time

from openai import AsyncOpenAI

from .config import Config

log = logging.getLogger(__name__)


class LLM:
    def __init__(self, cfg: Config):
        self.client = AsyncOpenAI(
            base_url=cfg.llm_base_url,
            api_key=cfg.llm_api_key,
            timeout=cfg.llm_timeout,
            max_retries=cfg.llm_max_retries,
        )
        self.model = cfg.llm_model
        self.no_think = cfg.no_think
        self.max_log_chars = cfg.max_log_chars
        self._sem = asyncio.Semaphore(cfg.llm_concurrency)

    def _extra_body(self, no_think: bool) -> dict:
        if no_think:
            return {"chat_template_kwargs": {"enable_thinking": False}}
        return {}

    async def chat(self, system: str, user: str, temperature: float = 0.2,
                   no_think: bool | None = None, tag: str = "chat") -> str:
        nt = self.no_think if no_think is None else no_think
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        self._log_request(tag, messages, tools=None)
        async with self._sem:
            started = time.monotonic()
            resp = await self.client.chat.completions.create(
                model=self.model, temperature=temperature,
                messages=messages, extra_body=self._extra_body(nt),
            )
            elapsed = time.monotonic() - started
        msg = resp.choices[0].message
        self._log_response(tag, msg, elapsed)
        return msg.content or ""

    async def chat_with_tools(self, messages: list[dict], tools: list[dict],
                              temperature: float = 0.2, no_think: bool | None = None,
                              tag: str = "agent"):
        """대화 누적 messages + tools로 호출. message 객체(content + tool_calls)를 반환."""
        nt = self.no_think if no_think is None else no_think
        kwargs: dict = {
            "model": self.model, "temperature": temperature,
            "messages": messages, "extra_body": self._extra_body(nt),
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"
        self._log_request(tag, messages, tools=tools)
        async with self._sem:
            started = time.monotonic()
            resp = await self.client.chat.completions.create(**kwargs)
            elapsed = time.monotonic() - started
        msg = resp.choices[0].message
        self._log_response(tag, msg, elapsed)
        return msg

    # ---- 로깅 헬퍼 -----------------------------------------------------

    def _log_request(self, tag: str, messages: list[dict], tools) -> None:
        if not log.isEnabledFor(logging.DEBUG):
            return
        tools_note = f", tools={len(tools)}" if tools else ""
        # 누적 messages가 길 수 있어, 마지막 입력 위주로 + system은 처음만 의미 있음
        shown = messages if len(messages) <= 3 else messages[-2:]
        prefix = "" if len(messages) <= 3 else f"  …앞선 {len(messages) - 2}개 메시지 생략\n"
        body = "\n".join(self._fmt_msg(m) for m in shown)
        log.debug("[%s] → LLM 요청 (메시지 %d개%s)\n%s%s",
                  tag, len(messages), tools_note, prefix, body)

    def _log_response(self, tag: str, msg, elapsed: float) -> None:
        tcs = getattr(msg, "tool_calls", None)
        if log.isEnabledFor(logging.DEBUG):
            tc_str = ""
            if tcs:
                tc_str = "\n  tool_calls: " + "; ".join(
                    f"{t.function.name}({t.function.arguments})" for t in tcs
                )
            log.debug("[%s] ← LLM 응답 (%.1fs)%s\n  content: %s",
                      tag, elapsed, tc_str, self._short(msg.content or "(없음)"))
        elif elapsed > 60:
            log.info("[%s] LLM 응답까지 %.0f초 (게이트웨이 타임아웃 주의)", tag, elapsed)

    def _fmt_msg(self, m: dict) -> str:
        role = m.get("role", "?")
        if m.get("tool_calls"):
            calls = "; ".join(
                f"{t['function']['name']}({t['function']['arguments']})" for t in m["tool_calls"]
            )
            return f"  [{role}] tool_calls: {calls}"
        content = m.get("content") or ""
        if role == "tool":
            return f"  [tool 결과] {self._short(content)}"
        return f"  [{role}] {self._short(content)}"

    def _short(self, text: str) -> str:
        text = str(text)
        if len(text) <= self.max_log_chars:
            return text
        return text[: self.max_log_chars] + f" …({len(text) - self.max_log_chars}자 생략)"


def clip(text: str, limit: int, label: str = "") -> str:
    if len(text) <= limit:
        return text
    omitted = len(text) - limit
    return text[:limit] + f"\n...[{label} 잘림: {omitted}자 생략]"
