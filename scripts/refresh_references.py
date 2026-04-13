#!/usr/bin/env python3
"""
Refresh the pre-bundled reference content in orch/templates/reference/.

This script fetches content from upstream repositories, compacts it, and
writes the results to the templates directory. Run manually when upstream
repos have significant updates.

Usage:
    python scripts/refresh_references.py

Sources:
    1. Agent-Skills-for-Context-Engineering (context engineering patterns)
    2. awesome-agent-skills (curated agent skills by org and domain)
    3. awesome-claude-code (CLAUDE.md and workflow patterns)
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

TEMPLATES_REF = Path(__file__).parent.parent / "orch" / "templates" / "reference"

REPOS = {
    "context-engineering": "https://github.com/muratcankoylan/Agent-Skills-for-Context-Engineering.git",
    "agent-skills": "https://github.com/VoltAgent/awesome-agent-skills.git",
    "claude-code": "https://github.com/hesreallyhim/awesome-claude-code.git",
}


def _clone(url: str, dest: Path) -> None:
    """Shallow clone a repo."""
    subprocess.run(
        ["git", "clone", "--depth", "1", url, str(dest)],
        capture_output=True,
        check=True,
    )


def _refresh_context_engineering(repo_dir: Path) -> None:
    """Extract and compact context engineering skill files."""
    out_dir = TEMPLATES_REF / "context-engineering"
    out_dir.mkdir(parents=True, exist_ok=True)

    skills_dir = repo_dir / "skills"
    if not skills_dir.is_dir():
        print(f"  Warning: {skills_dir} not found, skipping context-engineering")
        return

    for skill_file in sorted(skills_dir.glob("*.md")):
        # Compact: read, strip excessive whitespace, write
        content = skill_file.read_text()
        out_file = out_dir / skill_file.name
        out_file.write_text(content)
        print(f"  Wrote {out_file.relative_to(TEMPLATES_REF)}")


def _refresh_agent_skills(repo_dir: Path) -> None:
    """Extract agent skills README and organize by org/domain."""
    out_dir = TEMPLATES_REF / "agent-skills"
    out_dir.mkdir(parents=True, exist_ok=True)

    readme = repo_dir / "README.md"
    if readme.exists():
        content = readme.read_text()
        (out_dir / "full-index.md").write_text(content)
        print(f"  Wrote agent-skills/full-index.md ({len(content)} bytes)")


def _refresh_claude_code(repo_dir: Path) -> None:
    """Extract patterns from awesome-claude-code."""
    out_dir = TEMPLATES_REF / "patterns"
    out_dir.mkdir(parents=True, exist_ok=True)

    readme = repo_dir / "README.md"
    if readme.exists():
        content = readme.read_text()
        (out_dir / "full-index.md").write_text(content)
        print(f"  Wrote patterns/full-index.md ({len(content)} bytes)")


def main() -> None:
    print("Refreshing reference content...\n")

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)

        for name, url in REPOS.items():
            print(f"Cloning {name}...")
            dest = tmp / name
            try:
                _clone(url, dest)
            except subprocess.CalledProcessError as e:
                print(f"  Failed to clone {url}: {e}")
                continue

            if name == "context-engineering":
                _refresh_context_engineering(dest)
            elif name == "agent-skills":
                _refresh_agent_skills(dest)
            elif name == "claude-code":
                _refresh_claude_code(dest)

    print("\nDone. Review and compact the output in orch/templates/reference/")
    print("Then commit the changes.")


if __name__ == "__main__":
    main()
