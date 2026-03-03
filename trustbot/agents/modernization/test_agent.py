"""
Agent 6: Code Test Agent

Analyzes legacy codebase to extract functional specifications, generates
test files (Jest/Vitest for React, xUnit/NUnit for .NET), executes them
via BuildTool, and reports results.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

import litellm

from trustbot.config import settings
from trustbot.index.code_index import CodeIndex
from trustbot.models.modernization import (
    ModernizationConfig,
    Phase1Result,
    Phase2Result,
    TestCategory,
    TestFileOutput,
    TestResult,
    TestSpec,
)
from trustbot.tools.build_tool import BuildTool

logger = logging.getLogger("trustbot.agents.modernization.test")


class TestAgent:
    """Code Test Agent -- generates and runs tests on the modernized codebase."""

    def __init__(self, code_index: CodeIndex, build_tool: BuildTool) -> None:
        self._index = code_index
        self._tool = build_tool
        self._log_cb = None

    def _log(self, msg: str, level: str = "info"):
        if self._log_cb:
            self._log_cb(msg, level)

    async def run(
        self,
        phase1: Phase1Result,
        phase2: Phase2Result,
        config: ModernizationConfig,
        progress_callback=None,
        log_callback=None,
    ) -> TestResult:
        self._log_cb = log_callback
        if progress_callback:
            progress_callback(0.0, "Analyzing legacy codebase for test specs...")

        specs = await self._extract_test_specs(phase1, config)

        if progress_callback:
            progress_callback(0.3, "Generating test files...")

        test_files = await self._generate_test_files(specs, phase2, config)
        self._write_test_files(test_files, config)

        if progress_callback:
            progress_callback(0.6, "Running frontend tests...")

        frontend_results = await self._run_frontend_tests(config, log_callback)

        if progress_callback:
            progress_callback(0.8, "Running backend tests...")

        backend_results = await self._run_backend_tests(config, log_callback)

        total = frontend_results["total"] + backend_results["total"]
        passed = frontend_results["passed"] + backend_results["passed"]
        failed = frontend_results["failed"] + backend_results["failed"]
        failing_details = frontend_results["details"] + backend_results["details"]

        summary = self._generate_summary(specs, test_files, total, passed, failed, failing_details)

        return TestResult(
            specs=specs,
            generated_test_files=test_files,
            total_tests=total,
            passed=passed,
            failed=failed,
            coverage_pct=(passed / total * 100) if total > 0 else 0.0,
            failing_details=failing_details,
            summary_markdown=summary,
        )

    async def _extract_test_specs(
        self, phase1: Phase1Result, config: ModernizationConfig,
    ) -> list[TestSpec]:
        """Extract functional specifications from the legacy codebase via LLM."""
        inventory = phase1.inventory

        functions_summary = []
        for item in inventory.backend_files[:30]:
            conn = self._index.get_cache_conn()
            rows = conn.execute(
                "SELECT function_name FROM code_index WHERE file_path = ?",
                (item.file_path,),
            ).fetchall()
            funcs = [r[0] for r in rows]
            if funcs:
                functions_summary.append(f"- {item.file_path}: {', '.join(funcs[:10])}")

        if not functions_summary:
            return []

        prompt = (
            "Analyze these legacy backend functions and generate test specifications.\n\n"
            f"Functions:\n{'chr(10)'.join(functions_summary[:40])}\n\n"
            "For each function, generate a test spec in this format:\n"
            "TEST: <test_name> | CATEGORY: <functional/sanity/integration/unit> | "
            "FUNCTION: <source_function> | FILE: <source_file> | "
            "BEHAVIOR: <expected behavior description>\n\n"
            "Generate functional tests for critical business logic, "
            "sanity tests for basic operations, and integration tests for "
            "cross-component interactions. Output only TEST lines."
        )

        try:
            response = await litellm.acompletion(
                model=settings.litellm_model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.2,
                max_tokens=3000,
                **settings.get_litellm_kwargs(),
            )
            text = response.choices[0].message.content or ""
            return self._parse_test_specs(text)
        except Exception as e:
            logger.warning("LLM test spec extraction failed: %s", str(e)[:200])
            self._log("LLM test spec extraction failed, using fallback", "warning")
            return self._fallback_test_specs(phase1)

    def _parse_test_specs(self, text: str) -> list[TestSpec]:
        specs = []
        for line in text.strip().splitlines():
            line = line.strip()
            if not line.startswith("TEST:"):
                continue
            try:
                parts = {}
                for segment in line.split("|"):
                    segment = segment.strip()
                    if ":" in segment:
                        key, val = segment.split(":", 1)
                        parts[key.strip().upper()] = val.strip()

                cat_str = parts.get("CATEGORY", "functional").lower()
                try:
                    category = TestCategory(cat_str)
                except ValueError:
                    category = TestCategory.FUNCTIONAL

                specs.append(TestSpec(
                    test_name=parts.get("TEST", "unnamed_test"),
                    category=category,
                    source_function=parts.get("FUNCTION", ""),
                    source_file=parts.get("FILE", ""),
                    expected_behavior=parts.get("BEHAVIOR", ""),
                ))
            except Exception:
                continue
        return specs

    def _fallback_test_specs(self, phase1: Phase1Result) -> list[TestSpec]:
        """Generate basic test specs without LLM."""
        specs = []
        for item in phase1.inventory.backend_files[:20]:
            specs.append(TestSpec(
                test_name=f"test_{Path(item.file_path).stem}_sanity",
                category=TestCategory.SANITY,
                source_file=item.file_path,
                expected_behavior="Module loads and basic operations work",
            ))
        for comp in phase1.inventory.component_suggestions[:20]:
            specs.append(TestSpec(
                test_name=f"test_{comp.component_name}_renders",
                category=TestCategory.SANITY,
                source_function=comp.component_name,
                expected_behavior="Component renders without errors",
            ))
        return specs

    async def _generate_test_files(
        self,
        specs: list[TestSpec],
        phase2: Phase2Result,
        config: ModernizationConfig,
    ) -> list[TestFileOutput]:
        """Generate test files from specifications (frontend + backend in parallel)."""
        frontend_specs = [s for s in specs if s.category in (TestCategory.SANITY, TestCategory.UNIT)]
        backend_specs = [s for s in specs if s.category in (TestCategory.FUNCTIONAL, TestCategory.INTEGRATION)]

        coros = []
        if frontend_specs:
            coros.append(self._generate_frontend_tests(frontend_specs, config))
        if backend_specs:
            coros.append(self._generate_backend_tests(backend_specs, config))

        results = await asyncio.gather(*coros) if coros else []
        return [r for r in results if r is not None]

    async def _generate_frontend_tests(
        self, specs: list[TestSpec], config: ModernizationConfig,
    ) -> TestFileOutput | None:
        spec_list = "\n".join(
            f"- {s.test_name}: {s.expected_behavior}" for s in specs[:20]
        )
        prompt = (
            "Generate a Vitest test file for React components.\n\n"
            f"Test specifications:\n{spec_list}\n\n"
            "Requirements:\n"
            "- Use Vitest + React Testing Library\n"
            "- Each test should be independent\n"
            "- Include proper imports\n"
            "- Use describe/it blocks\n\n"
            "Output ONLY the test code."
        )

        try:
            response = await litellm.acompletion(
                model=settings.litellm_model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.2,
                max_tokens=3000,
                **settings.get_litellm_kwargs(),
            )
            code = self._extract_code(response.choices[0].message.content or "")
            return TestFileOutput(
                file_path="frontend/src/__tests__/components.test.tsx",
                content=code,
                test_count=len(specs),
                framework="vitest",
            )
        except Exception as e:
            logger.warning("Frontend test generation failed: %s", str(e)[:200])
            self._log("Frontend test generation failed", "warning")
            return None

    async def _generate_backend_tests(
        self, specs: list[TestSpec], config: ModernizationConfig,
    ) -> TestFileOutput | None:
        spec_list = "\n".join(
            f"- {s.test_name}: {s.expected_behavior}" for s in specs[:20]
        )

        if config.target_backend.startswith("aspnet"):
            framework_hint = "xUnit with Moq"
            ext = ".cs"
        elif config.target_backend == "fastapi":
            framework_hint = "pytest"
            ext = ".py"
        else:
            framework_hint = "Jest"
            ext = ".ts"

        prompt = (
            f"Generate a {framework_hint} test file for backend services.\n\n"
            f"Test specifications:\n{spec_list}\n\n"
            "Requirements:\n"
            f"- Use {framework_hint}\n"
            "- Mock external dependencies\n"
            "- Test both success and error cases\n\n"
            "Output ONLY the test code."
        )

        try:
            response = await litellm.acompletion(
                model=settings.litellm_model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.2,
                max_tokens=3000,
                **settings.get_litellm_kwargs(),
            )
            code = self._extract_code(response.choices[0].message.content or "")
            return TestFileOutput(
                file_path=f"backend/Tests/ServiceTests{ext}",
                content=code,
                test_count=len(specs),
                framework=framework_hint.split()[0].lower(),
            )
        except Exception as e:
            logger.warning("Backend test generation failed: %s", str(e)[:200])
            self._log("Backend test generation failed", "warning")
            return None

    def _write_test_files(self, test_files: list[TestFileOutput], config: ModernizationConfig) -> None:
        output_dir = Path(config.output_directory)
        for tf in test_files:
            target = output_dir / tf.file_path
            os.makedirs(target.parent, exist_ok=True)
            target.write_text(tf.content, encoding="utf-8")
            logger.info("Wrote test file: %s", tf.file_path)
            self._log(f"Wrote test file: {tf.file_path}")

    async def _run_frontend_tests(self, config: ModernizationConfig, log_callback=None) -> dict:
        frontend_dir = str(Path(config.output_directory) / "frontend")
        result = await self._tool.npm_test(frontend_dir, log_callback=log_callback)
        return self._parse_test_output(result.stdout + "\n" + result.stderr)

    async def _run_backend_tests(self, config: ModernizationConfig, log_callback=None) -> dict:
        backend_dir = str(Path(config.output_directory) / "backend")
        if config.target_backend.startswith("aspnet"):
            result = await self._tool.dotnet_test(backend_dir, log_callback=log_callback)
        else:
            result = await self._tool.npm_test(backend_dir, log_callback=log_callback)
        return self._parse_test_output(result.stdout + "\n" + result.stderr)

    def _parse_test_output(self, output: str) -> dict:
        """Parse test output to extract pass/fail counts."""
        total = 0
        passed = 0
        failed = 0
        details = []

        for line in output.splitlines():
            lower = line.lower().strip()
            if "tests passed" in lower or "passing" in lower:
                try:
                    num = int("".join(c for c in lower.split("pass")[0] if c.isdigit()) or "0")
                    passed += num
                    total += num
                except ValueError:
                    pass
            elif "tests failed" in lower or "failing" in lower or "failed" in lower:
                try:
                    num = int("".join(c for c in lower.split("fail")[0] if c.isdigit()) or "0")
                    failed += num
                    total += num
                except ValueError:
                    pass
                details.append(line.strip())
            elif "error" in lower and "test" in lower:
                details.append(line.strip())

        return {"total": total, "passed": passed, "failed": failed, "details": details[:20]}

    def _extract_code(self, text: str) -> str:
        if "```" in text:
            parts = text.split("```")
            if len(parts) >= 3:
                block = parts[1]
                nl = block.find("\n")
                if nl != -1:
                    block = block[nl + 1:]
                return block.strip()
        return text.strip()

    def _generate_summary(
        self,
        specs: list[TestSpec],
        test_files: list[TestFileOutput],
        total: int,
        passed: int,
        failed: int,
        failing_details: list[str],
    ) -> str:
        lines = [
            "# Test Report",
            "",
            "## Summary",
            f"- **Test specifications**: {len(specs)}",
            f"- **Test files generated**: {len(test_files)}",
            f"- **Tests run**: {total}",
            f"- **Passed**: {passed}",
            f"- **Failed**: {failed}",
            f"- **Pass rate**: {(passed / total * 100) if total > 0 else 0:.1f}%",
            "",
            "## Test Categories",
        ]
        cat_counts = {}
        for s in specs:
            cat_counts[s.category.value] = cat_counts.get(s.category.value, 0) + 1
        for cat, count in cat_counts.items():
            lines.append(f"- **{cat}**: {count} specs")

        if failing_details:
            lines.extend(["", "## Failing Tests", ""])
            for d in failing_details[:10]:
                lines.append(f"- {d}")

        return "\n".join(lines)
