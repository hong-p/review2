"""로컬 LLM (OpenAI-compatible 엔드포인트) 래퍼.

로컬 LLM은 느릴 수 있으므로:
- 호출당 타임아웃을 길게 (기본 600초, --llm-timeout)
- 타임아웃/연결 오류/일시 오류는 SDK가 백오프와 함께 자동 재시도 (--llm-retries)
- 동시 요청 수 제한 (--llm-concurrency). 단일 GPU면 LLM 호출이 결국 직렬화되므로
  여기서 전역 동시성을 잡아 큐 폭주를 막는다.

두 가지 호출 방식:
- chat(): 단발 system+user → 텍스트 (planner, aggregator용)
- chat_with_tools(): 대화 누적 messages + tools → tool_calls 또는 텍스트 (tool use loop용)

thinking 제어: no_think=True면 Qwen3의 thinking을 끈다. 우선 extra_body로
enable_thinking=False를 시도하고, 서버가 무시할 때를 대비해 프롬프트에도 /no_think를
붙인다(프롬프트 쪽은 호출부에서 처리).
"""
import asyncio
import logging
import time

from openai import AsyncOpenAI

from config import Config

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
        self._sem = asyncio.Semaphore(cfg.llm_concurrency)

    def _extra_body(self, no_think: bool) -> dict:
        # vLLM/SGLang의 Qwen3 thinking 토글. 서버가 모르면 무시된다.
        if no_think:
            return {"chat_template_kwargs": {"enable_thinking": False}}
        return {}

    async def chat(self, system: str, user: str, temperature: float = 0.2,
                   no_think: bool | None = None) -> str:
        nt = self.no_think if no_think is None else no_think
        async with self._sem:
            started = time.monotonic()
            resp = await self.client.chat.completions.create(
                model=self.model,
                temperature=temperature,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                extra_body=self._extra_body(nt),
            )
            self._log_slow(started)
            return resp.choices[0].message.content or ""

    async def chat_with_tools(self, messages: list[dict], tools: list[dict],
                              temperature: float = 0.2, no_think: bool | None = None):
        """대화 누적 messages + tools로 호출. message 객체(content + tool_calls)를 반환.

        반환된 message는 그대로 messages에 append해서 다음 턴에 다시 보낸다.
        """
        nt = self.no_think if no_think is None else no_think
        kwargs: dict = {
            "model": self.model,
            "temperature": temperature,
            "messages": messages,
            "extra_body": self._extra_body(nt),
        }
        if tools:  # 빈 tools면 순수 텍스트 응답 강제 (턴 초과 시 결론 받기용)
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"
        async with self._sem:
            started = time.monotonic()
            resp = await self.client.chat.completions.create(**kwargs)
            self._log_slow(started)
            return resp.choices[0].message

    @staticmethod
    def _log_slow(started: float) -> None:
        elapsed = time.monotonic() - started
        if elapsed > 60:
            log.info("LLM 응답까지 %.0f초 소요 (게이트웨이 타임아웃 주의)", elapsed)


def clip(text: str, limit: int, label: str = "") -> str:
    if len(text) <= limit:
        return text
    omitted = len(text) - limit
    return text[:limit] + f"\n...[{label} 잘림: {omitted}자 생략]"
