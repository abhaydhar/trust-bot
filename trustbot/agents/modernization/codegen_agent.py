"""
Agent 4: Code Generation Agent

Reads legacy source files, splits them into frontend (React/TypeScript) and
backend (target framework) using the layer classification, and writes
generated files to the output directory.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path, PurePosixPath

import litellm

from trustbot.config import settings
from trustbot.prompts import get_prompt
from trustbot.models.modernization import (
    CodeGenResult,
    GeneratedCodeArtifact,
    LayerClassification,
    ModernizationConfig,
    Phase1Result,
)

logger = logging.getLogger("trustbot.agents.modernization.codegen")

_BACKEND_TEMPLATE_MAP = {
    "aspnet-core-webapi": {
        "controller_ext": ".cs",
        "service_ext": ".cs",
        "namespace_prefix": "ModernizedApp",
    },
    "aspnet-minimal": {
        "controller_ext": ".cs",
        "service_ext": ".cs",
        "namespace_prefix": "ModernizedApp",
    },
    "nodejs-express": {
        "controller_ext": ".ts",
        "service_ext": ".ts",
        "namespace_prefix": "",
    },
    "fastapi": {
        "controller_ext": ".py",
        "service_ext": ".py",
        "namespace_prefix": "",
    },
}


class CodeGenAgent:
    """Code Generation Agent -- translates legacy code into modern frontend + backend."""

    def __init__(self, filesystem_tool=None) -> None:
        self._fs_tool = filesystem_tool

    async def run(
        self,
        phase1: Phase1Result,
        config: ModernizationConfig,
        progress_callback=None,
        log_callback=None,
    ) -> CodeGenResult:
        inventory = phase1.inventory
        architecture = phase1.architecture
        output_dir = Path(config.output_directory)
        frontend_dir = output_dir / "frontend"
        backend_dir = output_dir / "backend"

        os.makedirs(frontend_dir / "src" / "components", exist_ok=True)
        os.makedirs(frontend_dir / "src" / "pages", exist_ok=True)
        os.makedirs(frontend_dir / "src" / "hooks", exist_ok=True)
        os.makedirs(frontend_dir / "src" / "services", exist_ok=True)
        os.makedirs(backend_dir / "Controllers", exist_ok=True)
        os.makedirs(backend_dir / "Services", exist_ok=True)
        os.makedirs(backend_dir / "Models", exist_ok=True)

        def _log(msg: str, level: str = "info"):
            if log_callback:
                log_callback(msg, level)

        artifacts: list[GeneratedCodeArtifact] = []
        total_items = len(inventory.component_suggestions) + len(inventory.backend_files)
        processed = 0
        semaphore = asyncio.Semaphore(settings.max_concurrent_llm_calls)

        if progress_callback:
            progress_callback(0.0, "Generating frontend components...")

        async def _gen_component(comp):
            nonlocal processed
            source_content = await self._read_source_files(comp.source_files, config)
            async with semaphore:
                artifact = await self._generate_react_component(
                    comp.component_name,
                    comp.component_type,
                    source_content,
                    comp.props,
                    config,
                )
            if not artifact:
                _log(f"Failed to generate component: {comp.component_name}", "warning")
            processed += 1
            if progress_callback and total_items > 0:
                progress_callback(
                    0.1 + 0.5 * (processed / total_items),
                    f"Generated component: {comp.component_name}",
                )
            return artifact

        component_results = await asyncio.gather(
            *[_gen_component(comp) for comp in inventory.component_suggestions],
        )
        for artifact in component_results:
            if artifact:
                self._write_artifact(artifact, output_dir)
                artifacts.append(artifact)
                _log(f"Wrote {artifact.file_path} ({len(artifact.content)} chars)")

        if progress_callback:
            progress_callback(0.6, "Generating backend services...")

        async def _gen_backend(item):
            nonlocal processed
            source_content = await self._read_source_files([item.file_path], config)
            async with semaphore:
                backend_artifacts = await self._generate_backend_code(
                    item.file_path, source_content, config
                )
            if not backend_artifacts:
                _log(f"Failed to generate backend: {PurePosixPath(item.file_path).name}", "warning")
            processed += 1
            if progress_callback and total_items > 0:
                progress_callback(
                    0.1 + 0.5 * (processed / total_items),
                    f"Generated backend: {PurePosixPath(item.file_path).name}",
                )
            return backend_artifacts

        backend_results = await asyncio.gather(
            *[_gen_backend(item) for item in inventory.backend_files],
        )
        for backend_artifacts in backend_results:
            for art in backend_artifacts:
                self._write_artifact(art, output_dir)
                artifacts.append(art)
                _log(f"Wrote {art.file_path} ({len(art.content)} chars)")

        if progress_callback:
            progress_callback(0.85, "Generating shared DTOs...")

        dto_artifacts = await self._generate_shared_dtos(inventory, config)
        for art in dto_artifacts:
            self._write_artifact(art, output_dir)
            artifacts.append(art)
            _log(f"Wrote {art.file_path} ({len(art.content)} chars)")

        if progress_callback:
            progress_callback(0.95, "Writing summary...")

        summary = self._generate_summary(artifacts, config)

        return CodeGenResult(
            artifacts=artifacts,
            frontend_dir=str(frontend_dir),
            backend_dir=str(backend_dir),
            summary_markdown=summary,
        )

    async def _read_source_files(
        self, file_paths: list[str], config: ModernizationConfig,
    ) -> str:
        """Read source file contents from the legacy codebase."""
        contents = []
        codebase_root = Path(config.codebase_root)
        for fp in file_paths[:5]:
            full_path = codebase_root / fp
            try:
                text = full_path.read_text(encoding="utf-8", errors="replace")
                if len(text) > 8000:
                    text = text[:8000] + "\n// ... truncated ..."
                contents.append(f"=== {fp} ===\n{text}")
            except OSError:
                contents.append(f"=== {fp} ===\n(file not readable)")
        return "\n\n".join(contents)

    async def _generate_react_component(
        self,
        name: str,
        comp_type: str,
        source_content: str,
        props: list[str],
        config: ModernizationConfig,
    ) -> GeneratedCodeArtifact | None:
        """Generate a React component from legacy source."""
        props_str = ", ".join(props) if props else "none"

        prompt = get_prompt(
            "modernization.codegen_frontend",
            name=name,
            comp_type=comp_type,
            props_str=props_str,
            state_management=config.state_management,
            css_framework=config.css_framework,
            source_content=source_content[:6000],
        )

        try:
            response = await litellm.acompletion(
                model=settings.litellm_model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.2,
                max_tokens=settings.llm_max_tokens,
                **settings.get_litellm_kwargs(),
            )
            code = self._extract_code_block(response.choices[0].message.content or "")
            subdir = "pages" if comp_type == "page" else "components"
            return GeneratedCodeArtifact(
                file_path=f"frontend/src/{subdir}/{name}.tsx",
                content=code,
                layer=LayerClassification.PRESENTATION,
                source_files=[],
                language="typescript",
            )
        except Exception as e:
            logger.warning("Failed to generate component %s: %s", name, str(e)[:200])
            return None

    async def _generate_backend_code(
        self,
        source_file: str,
        source_content: str,
        config: ModernizationConfig,
    ) -> list[GeneratedCodeArtifact]:
        """Generate backend controller + service from legacy source."""
        stem = PurePosixPath(source_file).stem
        tmpl = _BACKEND_TEMPLATE_MAP.get(config.target_backend, _BACKEND_TEMPLATE_MAP["aspnet-core-webapi"])
        ext = tmpl["controller_ext"]
        ns = tmpl["namespace_prefix"]

        prompt = get_prompt(
            "modernization.codegen_backend",
            target_backend=config.target_backend,
            source_file=source_file,
            source_content=source_content[:6000],
            api_style=config.api_style.value,
        )

        try:
            response = await litellm.acompletion(
                model=settings.litellm_model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.2,
                max_tokens=settings.llm_max_tokens,
                **settings.get_litellm_kwargs(),
            )
            text = response.choices[0].message.content or ""
            blocks = self._extract_multiple_code_blocks(text)

            artifacts = []
            if len(blocks) >= 1:
                artifacts.append(GeneratedCodeArtifact(
                    file_path=f"backend/Controllers/{stem}Controller{ext}",
                    content=blocks[0],
                    layer=LayerClassification.BUSINESS_LOGIC,
                    source_files=[source_file],
                    language=ext.lstrip("."),
                ))
            if len(blocks) >= 2:
                artifacts.append(GeneratedCodeArtifact(
                    file_path=f"backend/Services/{stem}Service{ext}",
                    content=blocks[1],
                    layer=LayerClassification.BUSINESS_LOGIC,
                    source_files=[source_file],
                    language=ext.lstrip("."),
                ))
            return artifacts
        except Exception as e:
            logger.warning("Failed to generate backend for %s: %s", source_file, str(e)[:200])
            return []

    async def _generate_shared_dtos(
        self,
        inventory: FileInventory,
        config: ModernizationConfig,
    ) -> list[GeneratedCodeArtifact]:
        """Generate shared DTO/model files for the API contract."""
        endpoint_names = [ep.split("::")[-1] for ep in inventory.api_endpoints[:20]]
        if not endpoint_names:
            return []

        prompt = get_prompt(
            "modernization.codegen_shared",
            target_backend=config.target_backend,
            endpoint_list=", ".join(endpoint_names),
        )

        try:
            response = await litellm.acompletion(
                model=settings.litellm_model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.2,
                max_tokens=2000,
                **settings.get_litellm_kwargs(),
            )
            code = self._extract_code_block(response.choices[0].message.content or "")
            tmpl = _BACKEND_TEMPLATE_MAP.get(config.target_backend, _BACKEND_TEMPLATE_MAP["aspnet-core-webapi"])
            return [GeneratedCodeArtifact(
                file_path=f"backend/Models/Dtos{tmpl['service_ext']}",
                content=code,
                layer=LayerClassification.SHARED,
                language=tmpl["service_ext"].lstrip("."),
            )]
        except Exception as e:
            logger.warning("Failed to generate DTOs: %s", str(e)[:200])
            return []

    def _write_artifact(self, artifact: GeneratedCodeArtifact, output_dir: Path) -> None:
        """Write a generated artifact to disk."""
        target = output_dir / artifact.file_path
        os.makedirs(target.parent, exist_ok=True)
        target.write_text(artifact.content, encoding="utf-8")
        logger.info("Wrote %s (%d chars)", artifact.file_path, len(artifact.content))

    def _extract_code_block(self, text: str) -> str:
        """Extract code from a markdown code block, or return text as-is."""
        if "```" in text:
            parts = text.split("```")
            if len(parts) >= 3:
                block = parts[1]
                first_nl = block.find("\n")
                if first_nl != -1:
                    block = block[first_nl + 1:]
                return block.strip()
        return text.strip()

    def _extract_multiple_code_blocks(self, text: str) -> list[str]:
        """Extract multiple code blocks from LLM output."""
        blocks = []
        parts = text.split("```")
        for i in range(1, len(parts), 2):
            block = parts[i]
            first_nl = block.find("\n")
            if first_nl != -1:
                block = block[first_nl + 1:]
            blocks.append(block.strip())
        if not blocks:
            blocks.append(text.strip())
        return blocks

    def _generate_summary(
        self, artifacts: list[GeneratedCodeArtifact], config: ModernizationConfig,
    ) -> str:
        frontend_count = sum(1 for a in artifacts if a.layer == LayerClassification.PRESENTATION)
        backend_count = sum(1 for a in artifacts if a.layer == LayerClassification.BUSINESS_LOGIC)
        shared_count = sum(1 for a in artifacts if a.layer == LayerClassification.SHARED)
        total_loc = sum(a.content.count("\n") + 1 for a in artifacts)

        lines = [
            "# Code Generation Summary",
            "",
            f"- **Total files generated**: {len(artifacts)}",
            f"- **Frontend components**: {frontend_count}",
            f"- **Backend services/controllers**: {backend_count}",
            f"- **Shared models/DTOs**: {shared_count}",
            f"- **Total lines of code**: {total_loc:,}",
            "",
            "## Generated Files",
            "",
            "| File | Layer | Language | LOC |",
            "|------|-------|----------|-----|",
        ]
        for a in artifacts:
            loc = a.content.count("\n") + 1
            lines.append(f"| `{a.file_path}` | {a.layer.value} | {a.language} | {loc} |")
        return "\n".join(lines)
