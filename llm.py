"""로컬 LLM (OpenAI-compatible 엔드포인트) 래퍼."""
import logging

from openai import AsyncOpenAI

from config import Config

log = logging.getLogger(__name__)


class LLM:
    def __init__(self, cfg: Config):
        self.client = AsyncOpenAI(base_url=cfg.llm_base_url, api_key=cfg.llm_api_key)
        self.model = cfg.llm_model

    async def chat(self, system: str, user: str, temperature: float = 0.2) -> str:
        resp = await self.client.chat.completions.create(
            model=self.model,
            temperature=temperature,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
        return resp.choices[0].message.content or ""


def clip(text: str, limit: int, label: str = "") -> str:
    if len(text) <= limit:
        return text
    omitted = len(text) - limit
    return text[:limit] + f"\n...[{label} 잘림: {omitted}자 생략]"
