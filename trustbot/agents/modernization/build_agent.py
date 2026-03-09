"""
Agent 5: Code Build Agent

Scaffolds project configuration (package.json, tsconfig, .csproj),
runs builds for both frontend and backend, parses errors, feeds them
to the LLM for fixes, and retries in a loop.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

import litellm

from trustbot.config import settings
from trustbot.prompts import get_prompt
from trustbot.models.modernization import (
    BuildAttempt,
    BuildResult,
    CodeGenResult,
    ModernizationConfig,
)
from trustbot.tools.build_tool import BuildTool

logger = logging.getLogger("trustbot.agents.modernization.build")

_PACKAGE_JSON_TEMPLATE = {
    "name": "modernized-frontend",
    "private": True,
    "version": "1.0.0",
    "type": "module",
    "scripts": {
        "dev": "vite",
        "build": "tsc && vite build",
        "preview": "vite preview",
        "test": "vitest run",
    },
    "dependencies": {
        "react": "^18.2.0",
        "react-dom": "^18.2.0",
    },
    "devDependencies": {
        "@types/react": "^18.2.0",
        "@types/react-dom": "^18.2.0",
        "@vitejs/plugin-react": "^4.0.0",
        "typescript": "^5.3.0",
        "vite": "^5.0.0",
        "vitest": "^1.0.0",
    },
}

_TSCONFIG_TEMPLATE = {
    "compilerOptions": {
        "target": "ES2020",
        "useDefineForClassFields": True,
        "lib": ["ES2020", "DOM", "DOM.Iterable"],
        "module": "ESNext",
        "skipLibCheck": True,
        "moduleResolution": "bundler",
        "allowImportingTsExtensions": True,
        "resolveJsonModule": True,
        "isolatedModules": True,
        "noEmit": True,
        "jsx": "react-jsx",
        "strict": True,
        "noUnusedLocals": False,
        "noUnusedParameters": False,
        "noFallthroughCasesInSwitch": True,
    },
    "include": ["src"],
}

_VITE_CONFIG = """import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
})
"""

_INDEX_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Modernized App</title>
</head>
<body>
  <div id="root"></div>
  <script type="module" src="/src/main.tsx"></script>
</body>
</html>
"""

_MAIN_TSX = """import React from 'react'
import ReactDOM from 'react-dom/client'

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <div>Modernized Application</div>
  </React.StrictMode>,
)
"""


def _add_state_management_deps(pkg: dict, state_mgmt: str) -> None:
    if state_mgmt == "redux":
        pkg["dependencies"]["@reduxjs/toolkit"] = "^2.0.0"
        pkg["dependencies"]["react-redux"] = "^9.0.0"
    elif state_mgmt == "zustand":
        pkg["dependencies"]["zustand"] = "^4.4.0"


def _add_css_framework_deps(pkg: dict, css_fw: str) -> None:
    if css_fw == "tailwind":
        pkg["devDependencies"]["tailwindcss"] = "^3.4.0"
        pkg["devDependencies"]["postcss"] = "^8.4.0"
        pkg["devDependencies"]["autoprefixer"] = "^10.4.0"
    elif css_fw == "mui":
        pkg["dependencies"]["@mui/material"] = "^5.15.0"
        pkg["dependencies"]["@emotion/react"] = "^11.11.0"
        pkg["dependencies"]["@emotion/styled"] = "^11.11.0"
    elif css_fw == "bootstrap":
        pkg["dependencies"]["react-bootstrap"] = "^2.10.0"
        pkg["dependencies"]["bootstrap"] = "^5.3.0"


class BuildAgent:
    """Code Build Agent -- scaffolds, builds, and iteratively fixes projects."""

    def __init__(self, build_tool: BuildTool) -> None:
        self._tool = build_tool

    async def run(
        self,
        codegen: CodeGenResult,
        config: ModernizationConfig,
        progress_callback=None,
        log_callback=None,
    ) -> BuildResult:
        output_dir = Path(config.output_directory)
        frontend_dir = output_dir / "frontend"
        backend_dir = output_dir / "backend"

        if progress_callback:
            progress_callback(0.0, "Scaffolding project configuration...")

        self._scaffold_frontend(frontend_dir, config)
        self._scaffold_backend(backend_dir, config)

        attempts: list[BuildAttempt] = []
        max_retries = config.max_build_retries
        frontend_success = False
        backend_success = False

        if progress_callback:
            progress_callback(0.1, "Installing frontend dependencies...")

        install_result = await self._tool.npm_install(str(frontend_dir), log_callback=log_callback)
        if not install_result.success:
            logger.warning("npm install failed: %s", install_result.stderr[:300])

        for iteration in range(1, max_retries + 1):
            if progress_callback:
                progress_callback(
                    0.1 + 0.4 * (iteration / max_retries),
                    f"Frontend build attempt {iteration}/{max_retries}...",
                )

            if not frontend_success:
                result = await self._tool.npm_build(str(frontend_dir), log_callback=log_callback)
                attempt = BuildAttempt(
                    iteration=iteration,
                    target="frontend",
                    command=result.command,
                    success=result.success,
                    stdout=result.stdout[:2000],
                    stderr=result.stderr[:2000],
                    errors_found=result.stderr.count("error") if result.stderr else 0,
                )

                if result.success:
                    frontend_success = True
                else:
                    fixes = await self._llm_fix_errors(
                        "frontend", result.stderr, frontend_dir, config
                    )
                    attempt.fixes_applied = fixes

                attempts.append(attempt)

            if frontend_success:
                break

        if progress_callback:
            progress_callback(0.55, "Restoring backend dependencies...")

        restore_result = await self._tool.dotnet_restore(str(backend_dir), log_callback=log_callback)

        for iteration in range(1, max_retries + 1):
            if progress_callback:
                progress_callback(
                    0.55 + 0.35 * (iteration / max_retries),
                    f"Backend build attempt {iteration}/{max_retries}...",
                )

            if not backend_success:
                result = await self._tool.dotnet_build(str(backend_dir), log_callback=log_callback)
                attempt = BuildAttempt(
                    iteration=iteration,
                    target="backend",
                    command=result.command,
                    success=result.success,
                    stdout=result.stdout[:2000],
                    stderr=result.stderr[:2000],
                    errors_found=result.stderr.count("error") if result.stderr else 0,
                )

                if result.success:
                    backend_success = True
                else:
                    fixes = await self._llm_fix_errors(
                        "backend", result.stderr, backend_dir, config
                    )
                    attempt.fixes_applied = fixes

                attempts.append(attempt)

            if backend_success:
                break

        fe_errors = [a.stderr for a in attempts if a.target == "frontend" and not a.success]
        be_errors = [a.stderr for a in attempts if a.target == "backend" and not a.success]

        summary = self._generate_summary(
            attempts, frontend_success, backend_success, config
        )

        return BuildResult(
            frontend_success=frontend_success,
            backend_success=backend_success,
            total_iterations=len(attempts),
            attempts=attempts,
            final_frontend_errors=fe_errors[-1:],
            final_backend_errors=be_errors[-1:],
            summary_markdown=summary,
        )

    def _scaffold_frontend(self, frontend_dir: Path, config: ModernizationConfig) -> None:
        """Generate package.json, tsconfig.json, vite.config.ts, and index.html."""
        pkg = json.loads(json.dumps(_PACKAGE_JSON_TEMPLATE))
        _add_state_management_deps(pkg, config.state_management)
        _add_css_framework_deps(pkg, config.css_framework)

        if not (frontend_dir / "package.json").exists():
            (frontend_dir / "package.json").write_text(
                json.dumps(pkg, indent=2), encoding="utf-8"
            )

        if not (frontend_dir / "tsconfig.json").exists():
            (frontend_dir / "tsconfig.json").write_text(
                json.dumps(_TSCONFIG_TEMPLATE, indent=2), encoding="utf-8"
            )

        if not (frontend_dir / "vite.config.ts").exists():
            (frontend_dir / "vite.config.ts").write_text(_VITE_CONFIG, encoding="utf-8")

        if not (frontend_dir / "index.html").exists():
            (frontend_dir / "index.html").write_text(_INDEX_HTML, encoding="utf-8")

        src = frontend_dir / "src"
        os.makedirs(src, exist_ok=True)
        if not (src / "main.tsx").exists():
            (src / "main.tsx").write_text(_MAIN_TSX, encoding="utf-8")

    def _scaffold_backend(self, backend_dir: Path, config: ModernizationConfig) -> None:
        """Generate .csproj and Program.cs for .NET, or equivalent for other backends."""
        if config.target_backend.startswith("aspnet"):
            self._scaffold_dotnet(backend_dir, config)
        elif config.target_backend == "nodejs-express":
            self._scaffold_node_backend(backend_dir, config)
        elif config.target_backend == "fastapi":
            self._scaffold_fastapi_backend(backend_dir, config)

    def _scaffold_dotnet(self, backend_dir: Path, config: ModernizationConfig) -> None:
        csproj = backend_dir / "ModernizedApp.csproj"
        if not csproj.exists():
            csproj.write_text(
                '<Project Sdk="Microsoft.NET.Sdk.Web">\n'
                "  <PropertyGroup>\n"
                "    <TargetFramework>net8.0</TargetFramework>\n"
                "    <Nullable>enable</Nullable>\n"
                "    <ImplicitUsings>enable</ImplicitUsings>\n"
                "  </PropertyGroup>\n"
                "</Project>\n",
                encoding="utf-8",
            )
        program_cs = backend_dir / "Program.cs"
        if not program_cs.exists():
            program_cs.write_text(
                "var builder = WebApplication.CreateBuilder(args);\n"
                "builder.Services.AddControllers();\n"
                "builder.Services.AddEndpointsApiExplorer();\n"
                "builder.Services.AddSwaggerGen();\n\n"
                "var app = builder.Build();\n"
                "if (app.Environment.IsDevelopment())\n"
                "{\n"
                "    app.UseSwagger();\n"
                "    app.UseSwaggerUI();\n"
                "}\n"
                "app.UseHttpsRedirection();\n"
                "app.UseAuthorization();\n"
                "app.MapControllers();\n"
                "app.Run();\n",
                encoding="utf-8",
            )

    def _scaffold_node_backend(self, backend_dir: Path, config: ModernizationConfig) -> None:
        pkg_path = backend_dir / "package.json"
        if not pkg_path.exists():
            pkg = {
                "name": "modernized-backend",
                "version": "1.0.0",
                "scripts": {"build": "tsc", "start": "node dist/index.js", "test": "jest"},
                "dependencies": {"express": "^4.18.0"},
                "devDependencies": {
                    "typescript": "^5.3.0",
                    "@types/express": "^4.17.0",
                    "@types/node": "^20.0.0",
                },
            }
            pkg_path.write_text(json.dumps(pkg, indent=2), encoding="utf-8")

    def _scaffold_fastapi_backend(self, backend_dir: Path, config: ModernizationConfig) -> None:
        req_path = backend_dir / "requirements.txt"
        if not req_path.exists():
            req_path.write_text("fastapi>=0.104.0\nuvicorn>=0.24.0\npydantic>=2.5.0\n", encoding="utf-8")

    async def _llm_fix_errors(
        self,
        target: str,
        error_output: str,
        project_dir: Path,
        config: ModernizationConfig,
    ) -> list[str]:
        """Use LLM to suggest and apply fixes for build errors."""
        truncated_errors = error_output[:3000]
        prompt = get_prompt(
            "modernization.build_fix",
            target=target,
            truncated_errors=truncated_errors,
        )

        try:
            response = await litellm.acompletion(
                model=settings.litellm_model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.1,
                max_tokens=settings.llm_max_tokens,
                **settings.get_litellm_kwargs(),
            )
            text = response.choices[0].message.content or ""
            return self._apply_fixes(text, project_dir)
        except Exception as e:
            logger.warning("LLM fix attempt failed: %s", str(e)[:200])
            return []

    def _apply_fixes(self, llm_output: str, project_dir: Path) -> list[str]:
        """Parse LLM fix output and apply file changes."""
        fixes_applied = []
        current_file = None
        current_fix = None

        lines = llm_output.split("\n")
        i = 0
        while i < len(lines):
            line = lines[i].strip()
            if line.startswith("FILE:"):
                current_file = line[5:].strip()
                current_fix = None
            elif line.startswith("FIX:"):
                current_fix = line[4:].strip()
            elif line.startswith("CODE:") or line == "```":
                if current_file:
                    code_lines = []
                    i += 1
                    if i < len(lines) and lines[i].strip().startswith("```"):
                        i += 1
                    while i < len(lines) and not lines[i].strip().startswith("```"):
                        code_lines.append(lines[i])
                        i += 1
                    code_content = "\n".join(code_lines)
                    target_path = project_dir / current_file
                    try:
                        os.makedirs(target_path.parent, exist_ok=True)
                        target_path.write_text(code_content, encoding="utf-8")
                        fix_desc = f"Fixed {current_file}: {current_fix or 'applied correction'}"
                        fixes_applied.append(fix_desc)
                        logger.info("Applied fix: %s", fix_desc)
                    except OSError as e:
                        logger.warning("Could not apply fix to %s: %s", current_file, e)
            i += 1

        return fixes_applied

    def _generate_summary(
        self,
        attempts: list[BuildAttempt],
        frontend_ok: bool,
        backend_ok: bool,
        config: ModernizationConfig,
    ) -> str:
        lines = [
            "# Build Report",
            "",
            f"- **Frontend**: {'SUCCESS' if frontend_ok else 'FAILED'}",
            f"- **Backend**: {'SUCCESS' if backend_ok else 'FAILED'}",
            f"- **Total build attempts**: {len(attempts)}",
            f"- **Max retries configured**: {config.max_build_retries}",
            "",
            "## Attempt Log",
            "",
            "| # | Target | Success | Errors | Fixes Applied |",
            "|---|--------|---------|--------|---------------|",
        ]
        for a in attempts:
            lines.append(
                f"| {a.iteration} | {a.target} | "
                f"{'Yes' if a.success else 'No'} | {a.errors_found} | "
                f"{len(a.fixes_applied)} |"
            )
        return "\n".join(lines)
