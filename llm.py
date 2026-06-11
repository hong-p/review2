"""로컬 LLM (OpenAI-compatible 엔드포인트) 래퍼.

로컬 LLM은 느릴 수 있으므로:
- 호출당 타임아웃을 길게 (기본 600초, --llm-timeout)
- 타임아웃/연결 오류/일시 오류는 SDK가 백오프와 함께 자동 재시도 (--llm-retries)
- 배치 병렬 호출 시 동시 요청 수 제한 (--llm-concurrency, 기본 2)
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
        self._sem = asyncio.Semaphore(cfg.llm_concurrency)

    async def chat(self, system: str, user: str, temperature: float = 0.2) -> str:
        async with self._sem:
            started = time.monotonic()
            resp = await self.client.chat.completions.create(
                model=self.model,
                temperature=temperature,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            )
            elapsed = time.monotonic() - started
            if elapsed > 60:
                log.info("LLM 응답까지 %.0f초 소요", elapsed)
            return resp.choices[0].message.content or ""


def clip(text: str, limit: int, label: str = "") -> str:
    if len(text) <= limit:
        return text
    omitted = len(text) - limit
    return text[:limit] + f"\n...[{label} 잘림: {omitted}자 생략]"
