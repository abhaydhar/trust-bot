"""
1M Context Window Test for LiteLLM

Sends progressively larger prompts to verify whether the configured LiteLLM
model (from .env) supports up to ~1 million tokens of input context.

Strategy:
  - Generates synthetic C# code of known token-count tiers.
  - Approximate token count: 1 token ≈ 4 characters (conservative for code).
  - Tiers tested: 1K, 10K, 50K, 100K, 250K, 500K, 750K, 1M tokens.
  - For each tier, sends a single completion request asking the model to
    summarize the (padded) code, with max_tokens=100 to keep cost low.
  - Reports: success/failure, HTTP status, latency, actual prompt tokens.
"""

import asyncio
import os
import platform
import sys
import time
import uuid

sys.stdout.reconfigure(line_buffering=True)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import litellm  # noqa: E402
from trustbot.config import settings  # noqa: E402

CHARS_PER_TOKEN = 4

TOKEN_TIERS = [
    1_000,
    10_000,
    50_000,
    100_000,
    250_000,
    500_000,
    750_000,
    1_000_000,
]

CODE_TEMPLATE = """\
using System;
namespace Tier{idx}.Module{mod}
{{
    public class Service{mod}
    {{
        private readonly string _conn;
        public Service{mod}(string conn) {{ _conn = conn; }}

        public string Process(int id)
        {{
            var result = $"Processing {{id}} in module {mod}";
            Console.WriteLine(result);
            return result;
        }}

        public void Save(string data)
        {{
            Console.WriteLine($"Saving {{data}} via {{_conn}}");
        }}
    }}
}}
"""


def _generate_code_payload(target_tokens: int) -> str:
    """Build a synthetic code string of approximately `target_tokens` tokens."""
    target_chars = target_tokens * CHARS_PER_TOKEN
    chunks: list[str] = []
    total = 0
    mod = 0
    while total < target_chars:
        block = CODE_TEMPLATE.replace("{idx}", str(target_tokens)).replace(
            "{mod}", str(mod)
        )
        chunks.append(block)
        total += len(block)
        mod += 1
    payload = "\n".join(chunks)
    return payload[:target_chars]


def _litellm_kwargs() -> dict:
    return settings.get_litellm_kwargs()


async def test_tier(target_tokens: int) -> dict:
    """Send a prompt of ~target_tokens and return result dict."""
    nonce = uuid.uuid4().hex[:8]
    code_payload = _generate_code_payload(target_tokens)
    actual_chars = len(code_payload)
    estimated_tokens = actual_chars // CHARS_PER_TOKEN

    prompt = (
        "You are a code analyst. Below is a large C# codebase.\n"
        "Count how many classes are defined and reply ONLY with:\n"
        "CLASS_COUNT: <number>\n\n"
        f"```csharp\n{code_payload}\n```\n[ref:{nonce}]"
    )

    label = f"{target_tokens:>10,} tokens"
    print(f"\n{'-' * 70}")
    print(f"  TIER: ~{label}")
    print(f"  Payload chars  : {actual_chars:,}")
    print(f"  Est. tokens    : {estimated_tokens:,}")
    print(f"  Nonce          : {nonce}")

    result = {
        "tier": target_tokens,
        "est_tokens": estimated_tokens,
        "chars": actual_chars,
        "success": False,
        "prompt_tokens": None,
        "completion_tokens": None,
        "latency_s": None,
        "error": None,
        "response_preview": None,
    }

    t_start = time.perf_counter()
    try:
        response = await litellm.acompletion(
            model=settings.litellm_model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.0,
            max_tokens=100,
            **_litellm_kwargs(),
        )
        t_end = time.perf_counter()

        text = response.choices[0].message.content or ""
        usage = response.usage
        elapsed = t_end - t_start

        result["success"] = True
        result["latency_s"] = round(elapsed, 3)
        result["prompt_tokens"] = usage.prompt_tokens if usage else None
        result["completion_tokens"] = usage.completion_tokens if usage else None
        result["response_preview"] = text.strip()[:200]

        print(f"  Status         : OK")
        print(f"  Latency        : {elapsed:.3f}s")
        print(f"  Prompt tokens  : {usage.prompt_tokens if usage else '?'}")
        print(f"  Compl. tokens  : {usage.completion_tokens if usage else '?'}")
        print(f"  Response       : {text.strip()[:200]}")

    except Exception as exc:
        t_end = time.perf_counter()
        elapsed = t_end - t_start
        result["latency_s"] = round(elapsed, 3)

        err_str = str(exc)
        result["error"] = err_str[:500]

        print(f"  Status         : FAILED")
        print(f"  Latency        : {elapsed:.3f}s")
        print(f"  Error          : {err_str[:500]}")

    return result


async def main():
    print("=" * 70)
    print("  Context Window Test — LiteLLM 1M Token Support")
    print("=" * 70)
    print(f"\n  Model          : {settings.litellm_model}")
    print(f"  API base       : {settings.litellm_api_base}")
    print(f"  Max tokens cfg : {settings.llm_max_tokens}")
    print(f"  Tiers to test  : {[f'{t:,}' for t in TOKEN_TIERS]}")
    print(f"  Max completion  : 100 (kept small to reduce cost)")

    results: list[dict] = []
    consecutive_failures = 0

    for tier in TOKEN_TIERS:
        r = await test_tier(tier)
        results.append(r)

        if not r["success"]:
            consecutive_failures += 1
            if consecutive_failures >= 2:
                print(f"\n  ** 2 consecutive failures — stopping early **")
                break
        else:
            consecutive_failures = 0

    print(f"\n{'=' * 70}")
    print("  SUMMARY")
    print("=" * 70)
    print(f"  {'Tier':>12}  {'Status':>8}  {'Prompt Tok':>12}  {'Latency':>10}  Error")
    print(f"  {'-' * 12}  {'-' * 8}  {'-' * 12}  {'-' * 10}  {'-' * 30}")
    for r in results:
        tier_str = f"{r['tier']:>10,}"
        status = "OK" if r["success"] else "FAIL"
        ptok = f"{r['prompt_tokens']:>10,}" if r["prompt_tokens"] else "       N/A"
        lat = f"{r['latency_s']:>8.3f}s" if r["latency_s"] else "       N/A"
        err = (r["error"] or "")[:30]
        print(f"  {tier_str}  {status:>8}  {ptok}  {lat}  {err}")

    max_ok = 0
    for r in results:
        if r["success"] and r["prompt_tokens"]:
            max_ok = max(max_ok, r["prompt_tokens"])

    print(f"\n  Max successful prompt tokens: {max_ok:,}")
    if max_ok >= 900_000:
        print("  Verdict: 1M context window IS supported")
    elif max_ok >= 100_000:
        print(f"  Verdict: Large context supported (~{max_ok:,} tokens), but <1M")
    else:
        print(f"  Verdict: Context window appears limited to ~{max_ok:,} tokens")

    print("=" * 70)


def _run():
    if platform.system() == "Windows":
        import logging
        logging.getLogger("asyncio").setLevel(logging.CRITICAL)
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())


if __name__ == "__main__":
    _run()
