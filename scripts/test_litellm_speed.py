"""
LiteLLM Speed Benchmark

Simulates the kind of LLM calls Agent 1 (ArchitectAgent) makes:
  - Sends a code snippet for layer classification
  - Sends an architecture-generation prompt
Reports wall-clock time for each call and overall.
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

SAMPLE_CODE_SNIPPET = """\
using System;
using System.Data.SqlClient;
using System.Configuration;

namespace LegacyApp.DataAccess
{
    public class CustomerRepository
    {
        private readonly string _connString;

        public CustomerRepository()
        {
            _connString = ConfigurationManager.ConnectionStrings["DefaultConnection"].ConnectionString;
        }

        public Customer GetById(int id)
        {
            using (var conn = new SqlConnection(_connString))
            {
                conn.Open();
                var cmd = new SqlCommand("SELECT * FROM Customers WHERE Id = @Id", conn);
                cmd.Parameters.AddWithValue("@Id", id);
                using (var reader = cmd.ExecuteReader())
                {
                    if (reader.Read())
                    {
                        return new Customer
                        {
                            Id = (int)reader["Id"],
                            Name = reader["Name"].ToString(),
                            Email = reader["Email"].ToString(),
                            CreatedDate = (DateTime)reader["CreatedDate"]
                        };
                    }
                }
            }
            return null;
        }

        public void Save(Customer customer)
        {
            using (var conn = new SqlConnection(_connString))
            {
                conn.Open();
                var cmd = new SqlCommand(
                    "INSERT INTO Customers (Name, Email, CreatedDate) VALUES (@Name, @Email, @Date)", conn);
                cmd.Parameters.AddWithValue("@Name", customer.Name);
                cmd.Parameters.AddWithValue("@Email", customer.Email);
                cmd.Parameters.AddWithValue("@Date", DateTime.UtcNow);
                cmd.ExecuteNonQuery();
            }
        }
    }
}
"""


def _litellm_kwargs() -> dict:
    return settings.get_litellm_kwargs()


def _nonce() -> str:
    """Unique tag appended to prompts to defeat proxy-level caching."""
    return uuid.uuid4().hex[:8]


async def test_classify_code_snippet():
    """Simulate Agent 1 classifying a code file by layer."""
    nonce = _nonce()
    prompt = (
        "You are analyzing a legacy codebase for modernization.\n\n"
        "Classify the following code file into one of these layers:\n"
        "  presentation, business_logic, data_access, shared, configuration\n\n"
        f"```csharp\n{SAMPLE_CODE_SNIPPET}\n```\n\n"
        "Respond with:\n"
        "LAYER: <layer>\n"
        "CONFIDENCE: <high/medium/low>\n"
        f"REASONING: <one sentence explanation>\n[ref:{nonce}]"
    )

    print("\n--- Test 1: Code Classification (simulates Agent 1 _llm_classify_batch) ---")
    print(f"  Model       : {settings.litellm_model}")
    print(f"  API base    : {settings.litellm_api_base}")
    print(f"  Prompt size : {len(prompt)} chars")
    print(f"  Nonce       : {nonce}")

    t_start = time.perf_counter()
    response = await litellm.acompletion(
        model=settings.litellm_model,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.1,
        max_tokens=200,
        **_litellm_kwargs(),
    )
    t_end = time.perf_counter()

    text = response.choices[0].message.content or ""
    usage = response.usage
    elapsed = t_end - t_start

    print(f"  Elapsed     : {elapsed:.3f}s")
    print(f"  Tokens (in) : {usage.prompt_tokens if usage else '?'}")
    print(f"  Tokens (out): {usage.completion_tokens if usage else '?'}")
    tok_per_sec = (usage.completion_tokens / elapsed) if (usage and elapsed > 0) else 0
    print(f"  Tokens/sec  : {tok_per_sec:.1f}")
    print(f"  Response    :\n    {text.strip()[:300]}")

    return elapsed


async def test_architecture_generation():
    """Simulate Agent 1 generating an architecture document from code analysis."""
    nonce = _nonce()
    prompt = (
        "You are a software architect creating a modernization plan.\n\n"
        "## Legacy Codebase Analysis\n"
        "- Total files: 142\n"
        "- Total functions: 487\n"
        "- Total call edges: 312\n"
        "- Languages:\n"
        "  - C#: 320 functions\n"
        "  - JavaScript: 95 functions\n"
        "  - SQL: 72 functions\n"
        "- Layer distribution:\n"
        "  - presentation: 38 files\n"
        "  - business_logic: 45 files\n"
        "  - data_access: 27 files\n"
        "  - shared: 18 files\n"
        "  - configuration: 14 files\n"
        "- Cross-layer call relationships:\n"
        "  presentation -> business_logic: 89 edges\n"
        "  business_logic -> data_access: 67 edges\n"
        "  presentation -> data_access: 23 edges (tight coupling)\n\n"
        "## Sample Code (data access layer)\n"
        f"```csharp\n{SAMPLE_CODE_SNIPPET}\n```\n\n"
        "## Target Stack\n"
        "- Frontend: react-typescript\n"
        "- Backend: aspnet-core-webapi\n"
        "- Component strategy: one_to_one\n"
        "- State management: zustand\n"
        "- CSS framework: tailwindcss\n"
        "- API style: rest\n\n"
        "Generate a comprehensive **proposed to-be architecture** document in markdown. Include:\n"
        "1. Executive Summary\n"
        "2. Current State Analysis (AS-IS)\n"
        "3. Proposed Architecture (TO-BE) with clear frontend/backend separation\n"
        "4. Technology Stack Recommendations\n"
        "5. Component Architecture (how legacy maps to new)\n"
        "6. API Design Strategy\n"
        "7. Data Layer Strategy\n"
        "8. Migration Considerations\n"
        "9. Risk Assessment\n\n"
        f"Be specific and actionable. Reference the actual file counts and patterns found. [ref:{nonce}]"
    )

    print("\n--- Test 2: Architecture Document Generation (simulates Agent 1 _generate_architecture_doc) ---")
    print(f"  Model       : {settings.litellm_model}")
    print(f"  Prompt size : {len(prompt)} chars")
    print(f"  Nonce       : {nonce}")

    t_start = time.perf_counter()
    response = await litellm.acompletion(
        model=settings.litellm_model,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.3,
        max_tokens=settings.llm_max_tokens,
        **_litellm_kwargs(),
    )
    t_end = time.perf_counter()

    text = response.choices[0].message.content or ""
    usage = response.usage
    elapsed = t_end - t_start

    print(f"  Elapsed     : {elapsed:.3f}s")
    print(f"  Tokens (in) : {usage.prompt_tokens if usage else '?'}")
    print(f"  Tokens (out): {usage.completion_tokens if usage else '?'}")
    tok_per_sec = (usage.completion_tokens / elapsed) if (usage and elapsed > 0) else 0
    print(f"  Tokens/sec  : {tok_per_sec:.1f}")
    print(f"  Response preview ({len(text)} chars total):\n    {text.strip()[:400]}...")

    return elapsed


async def main():
    print("=" * 70)
    print("  LiteLLM Speed Benchmark -- simulating Agent 1 (ArchitectAgent)")
    print("=" * 70)
    print(f"\nConfig:")
    print(f"  LITELLM_MODEL          = {settings.litellm_model}")
    print(f"  LITELLM_API_BASE       = {settings.litellm_api_base}")
    print(f"  LITELLM_LLM_MAX_TOKENS = {settings.llm_max_tokens}")

    total_start = time.perf_counter()

    t1 = await test_classify_code_snippet()
    t2 = await test_architecture_generation()

    total_end = time.perf_counter()
    total = total_end - total_start

    print("\n" + "=" * 70)
    print("  SUMMARY")
    print("=" * 70)
    print(f"  Test 1 (classify snippet) : {t1:.3f}s")
    print(f"  Test 2 (arch doc gen)     : {t2:.3f}s")
    print(f"  Total wall-clock          : {total:.3f}s")
    print("=" * 70)


def _run():
    """Entry-point that handles the Windows ProactorEventLoop cleanup crash."""
    if platform.system() == "Windows":
        import logging
        logging.getLogger("asyncio").setLevel(logging.CRITICAL)

        # Use WindowsSelectorEventLoopPolicy to avoid the ProactorEventLoop
        # socket shutdown crash (ConnectionResetError / exit code -1073741819)
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    asyncio.run(main())


if __name__ == "__main__":
    _run()
