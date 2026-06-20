#!/usr/bin/env python3
"""Optional official Cognee SDK/API adapter for ProofSight memory.

ProofSight keeps SQLite + JSONL as the durable local memory path. This adapter is
an optional bridge from ``traces/cognee_ingest_queue.jsonl`` to the real Cognee
SDK/API when the package and credentials are available.

It deliberately avoids importing ``cognee`` at module import time so the core Pi
agent keeps working without Cognee's large dependency set installed.
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import importlib
import importlib.util
import json
import os
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
DEFAULT_QUEUE = ROOT / "traces" / "cognee_ingest_queue.jsonl"
DEFAULT_STATUS = ROOT / "traces" / "cognee_ingest_status.json"
DEFAULT_DATASET = "proofsight_hse"


def now_utc() -> str:
    return dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat()


def cognee_package_available() -> bool:
    return importlib.util.find_spec("cognee") is not None


def iter_jsonl(path: Path, limit: int | None = None) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            if not line.strip():
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError as exc:
                records.append({"type": "invalid_jsonl", "error": str(exc), "raw": line[:500]})
            if limit and len(records) >= limit:
                break
    return records


def record_to_memory_text(record: dict[str, Any]) -> str:
    """Convert a ProofSight memory JSON object into Cognee-ingestable text."""
    findings = record.get("findings") or []
    finding_lines = []
    for idx, finding in enumerate(findings, 1):
        if not isinstance(finding, dict):
            continue
        bits = [
            f"title={finding.get('title')}",
            f"hazard={finding.get('hazard')}",
            f"category={finding.get('hazard_category')}",
            f"risk={finding.get('risk_level')}",
            f"action={finding.get('corrective_action') or finding.get('immediate_action')}",
            f"status={finding.get('action_status')}",
        ]
        finding_lines.append(f"Finding {idx}: " + "; ".join(x for x in bits if not x.endswith("=None")))

    memory_context = record.get("memory_context") or {}
    similar = memory_context.get("similar_previous_findings") or []
    similar_lines = []
    for item in similar[:8]:
        if isinstance(item, dict):
            similar_lines.append(
                "Similar previous finding: "
                f"inspection={item.get('inspection_id')}; "
                f"location={item.get('location')}; "
                f"score={item.get('score')}; "
                f"summary={item.get('summary') or item.get('title')}"
            )

    lines = [
        "ProofSight health and safety inspection memory record.",
        f"Inspection ID: {record.get('inspection_id')}",
        f"Created at: {record.get('created_at')}",
        f"Location: {record.get('location')}",
        f"Status: {record.get('status')}",
        f"Summary: {record.get('summary')}",
        f"Hazard categories: {', '.join(record.get('hazard_categories') or [])}",
    ]
    lines.extend(finding_lines)
    lines.extend(similar_lines)
    return "\n".join(line for line in lines if line and not line.endswith("None"))


def write_status(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    safe_payload = dict(payload)
    # Do not persist API keys or tokens. Ever. Oi, behave.
    safe_payload.pop("api_key", None)
    path.write_text(json.dumps(safe_payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


async def _connect_cognee(cognee_module: Any) -> None:
    service_url = os.getenv("COGNEE_SERVICE_URL")
    api_key = os.getenv("COGNEE_API_KEY")
    if service_url and hasattr(cognee_module, "serve"):
        await cognee_module.serve(url=service_url, api_key=api_key)


async def ingest_queue(
    queue_path: str | Path = DEFAULT_QUEUE,
    *,
    status_path: str | Path = DEFAULT_STATUS,
    dataset_name: str = DEFAULT_DATASET,
    limit: int | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    queue = Path(queue_path)
    status = Path(status_path)
    records = iter_jsonl(queue, limit=limit)
    texts = [record_to_memory_text(r) for r in records if r.get("type") != "invalid_jsonl"]

    result: dict[str, Any] = {
        "status": "pending",
        "checked_at": now_utc(),
        "queue_path": str(queue),
        "status_path": str(status),
        "dataset_name": dataset_name,
        "package_available": cognee_package_available(),
        "records_seen": len(records),
        "records_sendable": len(texts),
        "records_sent": 0,
        "service_url_configured": bool(os.getenv("COGNEE_SERVICE_URL")),
        "api_key_configured": bool(os.getenv("COGNEE_API_KEY")),
    }

    if dry_run:
        result.update({"status": "dry_run", "preview": texts[:3]})
        write_status(status, result)
        return result

    if not result["package_available"]:
        result.update({
            "status": "unavailable",
            "reason": "Python package 'cognee' is not installed. Install it in an isolated venv before enabling official ingestion.",
        })
        write_status(status, result)
        return result

    if not texts:
        result.update({"status": "ok", "reason": "No sendable Cognee memory records found."})
        write_status(status, result)
        return result

    try:
        cognee = importlib.import_module("cognee")
        await _connect_cognee(cognee)
        for text in texts:
            if hasattr(cognee, "remember"):
                await cognee.remember(text, dataset_name=dataset_name)
            else:
                # Stable/main fallback for older SDKs: add + cognify later.
                await cognee.add(text, dataset_name=dataset_name)
            result["records_sent"] += 1
        if not hasattr(cognee, "remember") and hasattr(cognee, "cognify"):
            await cognee.cognify(datasets=[dataset_name])
        result["status"] = "ok"
    except Exception as exc:  # pragma: no cover - exercised against real SDK/API
        result.update({"status": "error", "error_type": type(exc).__name__, "error": str(exc)})

    write_status(status, result)
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Ingest ProofSight Cognee JSONL memory into official Cognee SDK/API when configured.")
    parser.add_argument("--queue", default=str(DEFAULT_QUEUE), help="Path to cognee_ingest_queue.jsonl")
    parser.add_argument("--status", default=str(DEFAULT_STATUS), help="Path for status JSON output")
    parser.add_argument("--dataset", default=os.getenv("PROOFSIGHT_COGNEE_DATASET", DEFAULT_DATASET))
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--dry-run", action="store_true", help="Build memory text and status without importing/calling Cognee")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    result = asyncio.run(
        ingest_queue(args.queue, status_path=args.status, dataset_name=args.dataset, limit=args.limit, dry_run=args.dry_run)
    )
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if result.get("status") in {"ok", "dry_run", "unavailable"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
