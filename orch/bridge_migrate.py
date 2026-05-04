"""
orch bridge migrate-from-files — one-shot importer for the pre-redesign
file-based bridge state.

Scans every orch-managed project for the following layout:

  <project>/.orch/bridge_request                  (live request never claimed)
  <project>/.orch/bridge_request.processing       (claimed but never finished)
  <project>/.orch/bridge_requests/<id>.json       (archived request)
  <project>/.orch/bridge_responses/<id>.json      (archived response)

Imports each into SQLite, then moves the source files into
``<project>/.orch/_archive/bridge-files-pre-redesign/`` for forensic
reference. Idempotent — re-running on an already-migrated project finds no
files to import.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

from . import state
from .discovery import discover_projects
from .models import Project


ARCHIVE_NAME = "_archive/bridge-files-pre-redesign"


def _load_response_index(project: Project) -> dict[str, dict]:
    """Return {old_id: response_dict} from bridge_responses/."""
    out: dict[str, dict] = {}
    rdir = project.orch_dir / "bridge_responses"
    if not rdir.is_dir():
        return out
    for f in rdir.iterdir():
        if not f.is_file() or f.suffix != ".json":
            continue
        try:
            data = json.loads(f.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        old_id = data.get("id") or f.stem
        out[old_id] = data
    return out


def _import_one_request(
    project: Project,
    req_path: Path,
    response: dict | None,
    intent_status: str,
) -> str | None:
    """Insert one bridge record from a file. Returns new bridge id or None."""
    try:
        req = json.loads(req_path.read_text())
    except (OSError, json.JSONDecodeError):
        return None

    target = req.get("target") or req.get("target_project") or "?"
    intent = req.get("intent") or "inform"
    if intent not in state.VALID_INTENTS:
        intent = "inform"
    summary = (req.get("summary") or req_path.stem)[:200]
    context_text = req.get("context") or ""
    request_text = req.get("request") or ""
    relevant_files = req.get("relevant_files") or []
    if not isinstance(relevant_files, list):
        relevant_files = []

    sub = state.BridgeSubmission(
        source_project=project.name,
        source_path=str(project.path),
        target_project=target,
        intent=intent,
        summary=summary,
        context=context_text,
        request=request_text,
        relevant_files=[str(f) for f in relevant_files],
    )
    record = state.insert_bridge(sub)
    new_id = record["id"]
    state.add_event(
        new_id, "imported_from_file",
        {"old_id": req.get("id") or req_path.stem, "src_path": str(req_path)},
    )

    if response is not None:
        old_status = response.get("status", "completed")
        result = response.get("result", "")
        pr_url = response.get("pr_url")
        branch = response.get("branch")
        if old_status == "completed":
            state.mark_completed(new_id, result=result, pr_url=pr_url, branch=branch)
        else:
            state.mark_failed(
                new_id,
                error=f"pre-redesign {old_status}: {result[:300]}",
                error_class=state.ERROR_PERMANENT,
                next_retry_at=None,
            )
    elif intent_status == "live_unprocessed":
        state.mark_failed(
            new_id,
            error="unprocessed_pre_redesign",
            error_class=state.ERROR_PERMANENT,
            next_retry_at=None,
        )
    elif intent_status == "archived_no_response":
        state.mark_failed(
            new_id,
            error="archived_without_response_pre_redesign",
            error_class=state.ERROR_PERMANENT,
            next_retry_at=None,
        )
    return new_id


def _archive(orch_dir: Path, paths: list[Path]) -> int:
    if not paths:
        return 0
    archive_root = orch_dir / ARCHIVE_NAME
    archive_root.mkdir(parents=True, exist_ok=True)
    moved = 0
    for src in paths:
        if not src.exists():
            continue
        dst = archive_root / src.relative_to(orch_dir)
        dst.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.move(str(src), str(dst))
            moved += 1
        except OSError:
            pass
    return moved


def migrate_project(project: Project, *, dry_run: bool = False) -> dict:
    """Return a summary dict for one project."""
    orch_dir = project.orch_dir
    if not orch_dir.is_dir():
        return {"project": project.name, "imported": 0, "archived": 0, "skipped": True}

    response_index = _load_response_index(project)
    imported = 0
    to_archive: list[Path] = []

    # Archived requests with matching responses → completed/failed records.
    archive_dir = orch_dir / "bridge_requests"
    if archive_dir.is_dir():
        for req_file in archive_dir.iterdir():
            if not req_file.is_file() or req_file.suffix != ".json":
                continue
            old_id = req_file.stem
            response = response_index.get(old_id)
            status_kind = "archived_no_response" if response is None else "archived_with_response"
            if not dry_run:
                if _import_one_request(project, req_file, response, status_kind):
                    imported += 1
            else:
                imported += 1
            to_archive.append(req_file)

    # Live un-claimed request — not processed before redesign.
    live = orch_dir / "bridge_request"
    if live.is_file():
        if not dry_run:
            if _import_one_request(project, live, None, "live_unprocessed"):
                imported += 1
        else:
            imported += 1
        to_archive.append(live)

    # Claimed-but-never-finished — same fate; mark failed.
    proc = orch_dir / "bridge_request.processing"
    if proc.is_file():
        if not dry_run:
            if _import_one_request(project, proc, None, "live_unprocessed"):
                imported += 1
        else:
            imported += 1
        to_archive.append(proc)

    # Response files we couldn't pair with any request — also archive them.
    rdir = orch_dir / "bridge_responses"
    if rdir.is_dir():
        for f in rdir.iterdir():
            if f.is_file():
                to_archive.append(f)

    # Old depth marker — archive too (no need in SQLite world).
    depth_file = orch_dir / "_bridge_depth"
    if depth_file.is_file():
        to_archive.append(depth_file)

    archived = 0 if dry_run else _archive(orch_dir, to_archive)

    # Remove the now-empty bridge_requests/ and bridge_responses/ dirs.
    if not dry_run:
        for d in (archive_dir, rdir):
            if d.is_dir():
                try:
                    if not any(d.iterdir()):
                        d.rmdir()
                except OSError:
                    pass

    return {
        "project": project.name,
        "imported": imported,
        "archived": archived,
        "skipped": False,
    }


def cmd_migrate(args: argparse.Namespace) -> int:
    state.init_db()
    projects = discover_projects()
    if args.only:
        projects = [p for p in projects if p.name == args.only]
        if not projects:
            print(f"project not found: {args.only}", file=sys.stderr)
            return 1

    total_imported = 0
    total_archived = 0
    for p in projects:
        summary = migrate_project(p, dry_run=args.dry_run)
        if summary["skipped"]:
            continue
        if summary["imported"] or summary["archived"]:
            print(
                f"  {summary['project']}: imported {summary['imported']} "
                f"records, archived {summary['archived']} files"
            )
        total_imported += summary["imported"]
        total_archived += summary["archived"]
    print()
    word = "would import" if args.dry_run else "imported"
    print(f"  {word}: {total_imported} bridge records")
    if not args.dry_run:
        print(f"  archived: {total_archived} files")
    return 0


def add_subparser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "migrate-from-files",
        help="import pre-redesign .orch/bridge* files into SQLite",
    )
    p.add_argument("--only", help="limit to a single project name")
    p.add_argument("--dry-run", action="store_true", help="report without writing")
    p.set_defaults(func=cmd_migrate)
