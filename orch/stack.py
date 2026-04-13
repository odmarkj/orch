"""
Tech-stack detection for orch-managed projects.

Scans project files to detect languages, frameworks, and infrastructure.
Used by:
  - orch init (sibling project summaries)
  - Daily stack detection (writes .claude-docs/project-stack.md)
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path


# ── Tag → recommended best-practices files ───────────────────────────────────

STACK_RECOMMENDATIONS: dict[str, list[str]] = {
    "python": [
        "best-practices/python/core.md",
    ],
    "django": [
        "best-practices/python/core.md",
        "best-practices/python/django.md",
    ],
    "fastapi": [
        "best-practices/python/core.md",
        "best-practices/python/fastapi.md",
        "best-practices/universal/api-design.md",
    ],
    "javascript": [
        "best-practices/javascript/core.md",
    ],
    "typescript": [
        "best-practices/javascript/core.md",
        "best-practices/javascript/typescript.md",
    ],
    "nextjs": [
        "best-practices/javascript/core.md",
        "best-practices/javascript/typescript.md",
    ],
    "cloud": [
        "best-practices/cloud/terraform.md",
        "best-practices/cloud/kubernetes.md",
    ],
    "kubernetes": [
        "best-practices/cloud/kubernetes.md",
    ],
    "database": [
        "best-practices/database/postgresql.md",
    ],
    "llm": [
        "best-practices/llm/rag.md",
        "best-practices/llm/prompt-engineering.md",
    ],
}

# Descriptions for project-stack.md recommendations
FILE_DESCRIPTIONS: dict[str, str] = {
    "best-practices/python/core.md": "Python type safety, async, error handling, project structure",
    "best-practices/python/django.md": "Django 5.x ORM, DRF, Channels, testing, deployment",
    "best-practices/python/fastapi.md": "FastAPI patterns, Pydantic V2, dependency injection",
    "best-practices/javascript/core.md": "ES6+, async/await, Node.js patterns, testing",
    "best-practices/javascript/typescript.md": "Advanced types, generics, type-safe patterns",
    "best-practices/cloud/terraform.md": "IaC patterns, modules, state management",
    "best-practices/cloud/kubernetes.md": "Manifests, Helm, GitOps, service mesh",
    "best-practices/database/postgresql.md": "Data modeling, indexing, query optimization",
    "best-practices/llm/rag.md": "RAG architecture, embeddings, hybrid search",
    "best-practices/llm/prompt-engineering.md": "Prompt patterns, evaluation, structured output",
    "best-practices/universal/api-design.md": "REST/GraphQL patterns, versioning, pagination",
    "best-practices/universal/testing.md": "TDD, test pyramid, mocking, coverage",
    "best-practices/universal/security.md": "SAST, dependency scanning, auth patterns",
    "best-practices/universal/architecture.md": "System design, CQRS, microservices, DDD",
    "best-practices/universal/observability.md": "Tracing, SLOs, Prometheus, alerting",
    "best-practices/universal/cicd.md": "Pipelines, deployment strategies, secrets",
    "best-practices/universal/code-quality.md": "Refactoring, code review, anti-patterns",
}


# ── Detection ────────────────────────────────────────────────────────────────

def detect_stack(project_path: Path) -> dict:
    """
    Detect tech stack from project marker files.
    Returns dict with 'tags' (sorted list) and 'detected_at' (ISO date).
    """
    tags: set[str] = set()

    # Python
    if any((project_path / f).is_file() for f in ["pyproject.toml", "setup.py", "requirements.txt"]):
        tags.add("python")
        for f in ["pyproject.toml", "requirements.txt"]:
            p = project_path / f
            if p.is_file():
                content = p.read_text(errors="ignore").lower()
                if "django" in content:
                    tags.add("django")
                if "fastapi" in content:
                    tags.add("fastapi")

    # JavaScript / TypeScript
    if (project_path / "package.json").is_file():
        tags.add("javascript")
        try:
            pkg = json.loads((project_path / "package.json").read_text())
            deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
            if "typescript" in deps or (project_path / "tsconfig.json").is_file():
                tags.add("typescript")
            if "next" in deps:
                tags.add("nextjs")
            if "react" in deps:
                tags.add("react")
        except (json.JSONDecodeError, OSError):
            pass

    # Cloud / Infrastructure
    if any((project_path / f).is_file() for f in ["main.tf", "terraform.tf", "Dockerfile", "docker-compose.yml"]):
        tags.add("cloud")
    if any((project_path / d).is_dir() for d in ["k8s", "kubernetes", "helm"]):
        tags.add("kubernetes")

    # Database
    if any((project_path / d).is_dir() for d in ["migrations", "alembic", "prisma"]):
        tags.add("database")

    # LLM / AI
    for f in ["pyproject.toml", "requirements.txt", "package.json"]:
        p = project_path / f
        if p.is_file():
            content = p.read_text(errors="ignore").lower()
            if any(k in content for k in ["langchain", "openai", "anthropic", "chromadb", "pinecone", "llama-index"]):
                tags.add("llm")
                break

    # Rust
    if (project_path / "Cargo.toml").is_file():
        tags.add("rust")

    # Go
    if (project_path / "go.mod").is_file():
        tags.add("go")

    return {"tags": sorted(tags), "detected_at": date.today().isoformat()}


def stack_label(tags: list[str]) -> str:
    """Convert tags to a human-readable stack label for summaries."""
    parts = []
    if "nextjs" in tags:
        parts.append("Next.js")
    elif "react" in tags:
        parts.append("React")
    if "typescript" in tags and "nextjs" not in tags:
        parts.append("TypeScript")
    elif "javascript" in tags and "nextjs" not in tags and "react" not in tags:
        parts.append("Node.js")
    if "django" in tags:
        parts.append("Django")
    elif "fastapi" in tags:
        parts.append("FastAPI")
    elif "python" in tags and "django" not in tags and "fastapi" not in tags:
        parts.append("Python")
    if "rust" in tags:
        parts.append("Rust")
    if "go" in tags:
        parts.append("Go")
    if "kubernetes" in tags:
        parts.append("Kubernetes")
    elif "cloud" in tags:
        parts.append("Docker")
    if "llm" in tags:
        parts.append("AI/ML")
    return ", ".join(parts) if parts else "unknown"


# ── Project stack file generation ────────────────────────────────────────────

def generate_project_stack_md(project_path: Path) -> str | None:
    """
    Detect stack and return the content for .claude-docs/project-stack.md.
    Returns None if no stack detected (empty project).
    """
    result = detect_stack(project_path)
    tags = result["tags"]

    if not tags:
        return None

    # Collect recommended files (deduplicated, ordered)
    seen = set()
    recommendations = []
    # Stack-specific first
    for tag in tags:
        for f in STACK_RECOMMENDATIONS.get(tag, []):
            if f not in seen:
                seen.add(f)
                recommendations.append(f)
    # Always include relevant universal files
    for f in [
        "best-practices/universal/testing.md",
        "best-practices/universal/security.md",
        "best-practices/universal/architecture.md",
    ]:
        if f not in seen:
            seen.add(f)
            recommendations.append(f)

    lines = [
        "# Detected Project Stack",
        "",
        f"Last scanned: {result['detected_at']}",
        "",
        "## Technologies",
    ]
    for tag in tags:
        lines.append(f"- **{tag}**")

    lines += [
        "",
        "## Recommended Reference Files",
        "",
        "Based on the detected stack, prioritize these best-practices files:",
    ]
    for f in recommendations:
        desc = FILE_DESCRIPTIONS.get(f, "")
        lines.append(f"- `{f}` -- {desc}")

    return "\n".join(lines) + "\n"
