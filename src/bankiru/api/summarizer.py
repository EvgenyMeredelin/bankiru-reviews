"""Recursive map-reduce summarizer for arbitrarily large review batches.

Pipeline
--------
1. Discover the model's context window via `model_catalog.get_model_context`
   (TTL-cached; falls back to `DEFAULT_MODEL_CONTEXT`).
2. Tokenize each input text once with `tiktoken` (`cl100k_base` — close
   enough for OpenAI-compatible models; overcounts slightly for non-OpenAI
   tokenizers, which is the safe direction).
3. Pack texts greedily into chunks of <= `input_budget` tokens.
4. Map: summarize each chunk concurrently (`asyncio.gather` capped at
   `SUMMARIZER_MAP_CONCURRENCY`) with a "partial batch" system prompt.
5. Reduce: if the joined chunk summaries fit one call, finalize with a
   "merging" system prompt. Otherwise, recurse — pack & map the chunk
   summaries themselves until convergence or `SUMMARIZER_MAX_PASSES`.
6. Always returns a string. Provider errors are surfaced as their
   server-supplied message.

Budget arithmetic
-----------------
  per_call_output = min(OUTPUT_TOKENS_LIMIT, max(256, max_model_len // 4))
  input_budget    = max_model_len - system_prompt_tokens
                     - per_call_output - SUMMARIZER_SAFETY_MARGIN_TOKENS

The `// 4` cap on per_call_output protects small-context models: without
it a model with a 4 k context and OUTPUT_TOKENS_LIMIT=10000 would consume
the entire context budget for output alone, leaving a negative input_budget.
The `max(256, …)` floor keeps per_call_output sensible for sub-1k-token
models (effectively theoretical, but guards against zero/negative values).
"""

from __future__ import annotations

import asyncio
from functools import lru_cache

import logfire
import tiktoken
from pydantic_ai import Agent, UsageLimits
from pydantic_ai.exceptions import ModelHTTPError, UsageLimitExceeded
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

from bankiru.api.model_catalog import get_model_context
from bankiru.config import get_settings

# ── System prompts ───────────────────────────────────────────────────────────
# Three prompts for different stages of the map-reduce pipeline:
#   FINAL — used when all texts fit in a single call (or for the final reduce)
#   MAP   — used for each chunk in the map phase (partial summaries)
#   REDUCE — used to merge partial summaries into a final summary
#
# All prompts are in Russian because the review texts are in Russian.
# The strict two-section structure (## Наиболее острые... / ## Наиболее частые...)
# ensures consistent output formatting across all LLM calls.

SYSTEM_PROMPT_FINAL = """Ты модель-суммаризатор.
Структура ответа — РОВНО два раздела, каждый с заголовком вида `## …`:
## Наиболее острые темы и причины жалоб
## Наиболее частые темы и причины жалоб
Внутри каждого раздела — нумерованный список фактов. Будь краток и
точен, опирайся только на текст жалоб. НЕ оборачивай ответ внешним
заголовком вроде «# Сводка жалоб» — начинай сразу с первого `##`.
"""

SYSTEM_PROMPT_MAP = """Ты модель-суммаризатор. Перед тобой ЧАСТЬ корпуса жалоб.
Извлеки строго по фактам:
- острые темы и причины жалоб в этой части;
- частые темы и причины жалоб в этой части.
Не делай обобщающих выводов о всём корпусе. Будь краток. НЕ добавляй
внешний заголовок «Сводка жалоб» — пиши простой текст.
"""

SYSTEM_PROMPT_REDUCE = """Ты модель-суммаризатор. Перед тобой набор частичных
суммаризаций, каждая описывает свою порцию корпуса жалоб. Слей их в одну
итоговую суммаризацию РОВНО в двух разделах с заголовками вида `## …`:
## Наиболее острые темы и причины жалоб
## Наиболее частые темы и причины жалоб
Сохраняй фактологию из частичных суммаризаций. НЕ оборачивай ответ
внешним заголовком вроде «# Сводка жалоб» — начинай сразу с первого `##`.
"""

# Separator used between texts when joining them into a single prompt.
# The "---" horizontal rule makes chunk boundaries visible to the LLM.
CHUNK_SEPARATOR = "\n\n---\n\n"

# Words that indicate a "wrapper" heading the LLM sometimes adds despite
# being told not to. These are stripped from the output to keep the
# response format consistent.
_WRAPPER_HEADING_WORDS = ("сводка", "summary", "резюме", "обзор")


def _strip_wrapper_heading(text: str) -> str:
    """Drop a stray leading wrapper heading like `## Сводка жалоб`.

    LLMs sometimes add a top-level heading (e.g. "# Сводка жалоб") even
    when the prompt explicitly says not to. This function removes it while
    preserving the intended section headings like "## Наиболее острые темы…".

    Only matches a top-level `#`/`##` line whose title contains a known
    wrapper word — section headings like `## Наиболее острые темы…` are
    intentionally preserved.
    """
    stripped = text.lstrip()
    if not stripped.startswith("#"):
        return text
    first, _, rest = stripped.partition("\n")
    title = first.lstrip("#").strip().lower()
    if any(word in title for word in _WRAPPER_HEADING_WORDS):
        return rest.lstrip()
    return text


@lru_cache(maxsize=1)
def _encoding() -> tiktoken.Encoding:
    """Return the cl100k_base tokenizer (cached singleton).

    cl100k_base is the tokenizer used by GPT-4 and similar models. It
    slightly overcounts tokens for non-OpenAI models, which is the safe
    direction (we'd rather underestimate available space than overflow).
    """
    return tiktoken.get_encoding("cl100k_base")


def _count(text: str) -> int:
    """Count the number of tokens in a text string."""
    return len(_encoding().encode(text))


def _truncate_to_tokens(text: str, n: int) -> str:
    """Truncate text to at most n tokens, preserving valid UTF-8.

    Used by _pack() to handle individual texts that exceed the input budget.
    The tiktoken decode step ensures the truncation doesn't break mid-character.
    """
    enc = _encoding()
    tokens = enc.encode(text)
    if len(tokens) <= n:
        return text
    return enc.decode(tokens[:n])


def _pack(texts: list[str], input_budget: int) -> list[str]:
    """Greedy bin-pack into chunks <= `input_budget` tokens.

    Texts that exceed `input_budget` on their own are truncated and
    prefixed with ``"[усечено] "``.
    """
    chunks: list[str] = []
    current: list[str] = []
    current_tokens = 0
    sep_tokens = _count(CHUNK_SEPARATOR)

    for text in texts:
        text_tokens = _count(text)
        if text_tokens > input_budget:
            text = "[усечено] " + _truncate_to_tokens(text, input_budget - 8)
            text_tokens = _count(text)

        extra = text_tokens + (sep_tokens if current else 0)
        if current and current_tokens + extra > input_budget:
            chunks.append(CHUNK_SEPARATOR.join(current))
            current = [text]
            current_tokens = text_tokens
        else:
            current.append(text)
            current_tokens += extra

    if current:
        chunks.append(CHUNK_SEPARATOR.join(current))

    return chunks


def _budgets(max_model_len: int, system_prompt_tokens: int) -> tuple[int, int]:
    """Calculate token budgets for a single LLM call.

    Returns (input_budget, per_call_output) where:
      - input_budget: max tokens available for the user message (review texts)
      - per_call_output: max tokens the model may generate in its response

    The arithmetic ensures that:
      system_prompt + input_budget + per_call_output + safety_margin <= max_model_len

    The per_call_output is capped at max_model_len // 4 to protect small-context
    models from having their entire budget consumed by output tokens.
    """
    s = get_settings()
    # Cap output tokens at 1/4 of the context window (protects small models).
    # Floor of 256 ensures a minimum useful output even for tiny contexts.
    per_call_output = min(s.OUTPUT_TOKENS_LIMIT, max(256, max_model_len // 4))
    input_budget = (
        max_model_len
        - system_prompt_tokens
        - per_call_output
        - s.SUMMARIZER_SAFETY_MARGIN_TOKENS
    )
    # Floor of 256 tokens for input to avoid degenerate cases.
    return max(256, input_budget), per_call_output


def _build_agent(model_name: str, system_prompt: str) -> Agent:
    """Create a pydantic-ai Agent configured for the given model and prompt.

    Uses the OpenAI-compatible provider (works with Cloud.ru Foundation Models
    and any other OpenAI-compatible endpoint).
    """
    s = get_settings()
    model = OpenAIChatModel(
        model_name=model_name,
        provider=OpenAIProvider(
            api_key=s.OPENAI_API_KEY or "",
            base_url=s.OPENAI_BASE_URL,
        ),
    )
    return Agent(model, system_prompt=system_prompt)


async def _run_one(
    agent: Agent,
    text: str,
    output_tokens_limit: int,
) -> str:
    """Run a single LLM call and strip any wrapper heading from the output."""
    limits = UsageLimits(output_tokens_limit=output_tokens_limit)
    run = await agent.run(text, usage_limits=limits)
    return _strip_wrapper_heading(run.output)


async def _run_many(
    agent: Agent,
    chunks: list[str],
    output_tokens_limit: int,
) -> list[str]:
    """Run multiple LLM calls concurrently, capped by SUMMARIZER_MAP_CONCURRENCY.

    Uses an asyncio.Semaphore to limit the number of concurrent API calls,
    preventing rate-limit errors from the LLM provider. The default concurrency
    of 4 balances throughput against provider rate limits.
    """
    sem = asyncio.Semaphore(get_settings().SUMMARIZER_MAP_CONCURRENCY)

    async def _guarded(chunk: str) -> str:
        async with sem:
            return await _run_one(agent, chunk, output_tokens_limit)

    return await asyncio.gather(*(_guarded(c) for c in chunks))


async def summarize_map_reduce(texts: list[str], *, model_name: str) -> str:
    """Recursive map-reduce summarization. Public entry point.

    Always returns a string — never raises. On LLM errors, returns the
    provider's error message as the summary text.

    Algorithm:
      1. If all texts fit in one LLM call → single FINAL call → done
      2. Otherwise, pack texts into chunks that fit the input budget
      3. MAP: summarize each chunk concurrently (partial summaries)
      4. REDUCE: if partial summaries fit one call → FINAL call → done
      5. Otherwise, recurse: treat partial summaries as new input texts
      6. Repeat until convergence or SUMMARIZER_MAX_PASSES reached

    Args:
        texts: List of review body texts to summarize.
        model_name: LLM model identifier (e.g. "anthropic/claude-sonnet-4.6").

    Returns:
        Markdown-formatted summary string.
    """
    if not texts:
        return ""

    s = get_settings()
    # Look up the model's context window size from the cached catalog.
    # Falls back to DEFAULT_MODEL_CONTEXT if the catalog is unavailable.
    max_model_len = await get_model_context(model_name)

    try:
        with logfire.span(
            "summarize_map_reduce n_texts={n} model={model} context={ctx}",
            n=len(texts), model=model_name, ctx=max_model_len,
        ):
            # `current` holds the texts to process in this pass.
            # On the first pass, these are the original review texts.
            # On subsequent passes, these are the partial summaries from
            # the previous map phase.
            current = list(texts)
            # First pass uses MAP prompt; subsequent passes use REDUCE prompt.
            current_prompt = SYSTEM_PROMPT_MAP

            for pass_no in range(1, s.SUMMARIZER_MAX_PASSES + 1):
                prompt_tokens = _count(current_prompt)
                input_budget, output_budget = _budgets(max_model_len, prompt_tokens)

                # ── Short-circuit: everything fits in one call ────────
                # If the joined texts fit within the input budget, skip
                # the map phase and go straight to a FINAL summary call.
                # The 1024-text cap prevents degenerate cases where many
                # tiny texts technically fit but would produce a poor summary.
                joined = CHUNK_SEPARATOR.join(current)
                if _count(joined) <= input_budget and len(current) <= 1024:
                    agent = _build_agent(model_name, SYSTEM_PROMPT_FINAL)
                    with logfire.span(
                        "summarize final pass={p}", p=pass_no,
                    ):
                        return await _run_one(agent, joined, output_budget)

                # ── Pack texts into chunks that fit the input budget ──
                chunks = _pack(current, input_budget)
                logfire.info(
                    "pass {p}: {n_in} -> {n_out} chunks "
                    "(input_budget={ib}, output_budget={ob})",
                    p=pass_no, n_in=len(current), n_out=len(chunks),
                    ib=input_budget, ob=output_budget,
                )

                # If packing produced a single chunk, it fits one call.
                if len(chunks) == 1:
                    agent = _build_agent(model_name, SYSTEM_PROMPT_FINAL)
                    with logfire.span(
                        "summarize final pass={p}", p=pass_no,
                    ):
                        return await _run_one(agent, chunks[0], output_budget)

                # ── MAP phase: summarize each chunk concurrently ──────
                agent = _build_agent(model_name, current_prompt)
                with logfire.span(
                    "summarize map pass={p} n_chunks={n}",
                    p=pass_no, n=len(chunks),
                ):
                    summaries = await _run_many(agent, chunks, output_budget)

                # Feed the partial summaries back as input for the next pass.
                # Switch to the REDUCE prompt for merging partial summaries.
                current = summaries
                current_prompt = SYSTEM_PROMPT_REDUCE

            # Exceeded max passes without convergence — concatenate the
            # partial summaries we have. This is a safety net; in practice,
            # 4 passes should be sufficient for any realistic review count.
            return CHUNK_SEPARATOR.join(current)

    except ModelHTTPError as error:
        # LLM provider returned an HTTP error (e.g. 429 rate limit, 500).
        # Extract the human-readable message from the error body.
        body = error.body or {}
        return body.get("message", str(error))

    except UsageLimitExceeded as error:
        # The LLM exceeded the output token limit we set.
        return error.message
