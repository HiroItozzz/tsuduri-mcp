"""LLM の呼び出し。pydantic-ai を通すので、モデルは差し替えられる（テストでは通信しないモデルを使う）。"""

import os
from dataclasses import dataclass
from importlib.resources import files

import pydantic_ai
from pydantic_ai import Agent
from pydantic_ai.models import Model
from pydantic_ai.models.google import GoogleModel
from pydantic_ai.providers.google import GoogleProvider

# 初回実行時のバナー（stderr）で MCP のログを汚さない。
# ライブラリ側に型注釈がなく ty が Literal[True] と推論するが、False を入れるのが公式の使い方
pydantic_ai.BANNER_ENABLED = False  # ty: ignore[invalid-assignment]

DEFAULT_GEMINI_MODEL = "gemini-3-flash-preview"


def gemini(model_name: str = DEFAULT_GEMINI_MODEL) -> Model:
    # pydantic-ai は GOOGLE_API_KEY を優先して読むので、GEMINI_API_KEY を明示して渡す
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY が見えません。MCP サーバーを起動する環境に設定してください")
    return GoogleModel(model_name, provider=GoogleProvider(api_key=api_key))


def load_prompt(name: str) -> str:
    """src/tsuduri_mcp/prompts/<name>.md を読む。"""
    return files("tsuduri_mcp").joinpath("prompts", f"{name}.md").read_text(encoding="utf-8")


@dataclass
class LlmResult[T]:
    output: T
    input_tokens: int
    output_tokens: int


async def generate[T](model: Model, instructions: str, prompt: str, output_type: type[T]) -> LlmResult[T]:
    agent = Agent(model, instructions=instructions, output_type=output_type)
    result = await agent.run(prompt)
    return LlmResult(result.output, result.usage.input_tokens, result.usage.output_tokens)
