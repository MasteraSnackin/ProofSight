#!/usr/bin/env python3
"""ProofSight partner integration layer.

This module keeps sponsor usage honest:
- Captur: local Captur-style trust gate is implemented now; official SDK can be
  added behind the same status/output shape when available for this device.
- Cognee: SQLite remains canonical memory; records are queued for Cognee ingest,
  and an official Cognee adapter can consume that queue once configured.
- Overmind: trace JSONL is emitted for improvement/failure-pattern analysis.
- Exo Labs: local Ollama is active; Exo distributed inference is exposed as a
  configured-but-not-active adapter until an Exo endpoint/cluster exists.
- Cosine: engineering partner lane is tracked in metadata; no runtime SDK.
"""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
from typing import Any


def _partner_cfg(cfg: dict[str, Any], name: str) -> dict[str, Any]:
    return ((cfg.get("partners") or {}).get(name) or {})


def _actions(cfg: dict[str, Any]) -> dict[str, Any]:
    return cfg.get("actions") or {}


def partner_status(cfg: dict[str, Any]) -> dict[str, Any]:
    """Return a machine-readable status for all sponsor integrations."""
    actions = _actions(cfg)
    trace_dir = Path(actions.get("trace_dir", "/home/dave/hse-pi-agent/traces"))
    db_path = Path(actions.get("db_path", "/home/dave/hse-pi-agent/data/proofsight.db"))

    cognee_available = importlib.util.find_spec("cognee") is not None
    exo_endpoint = os.getenv("PROOFSIGHT_EXO_BASE_URL") or _partner_cfg(cfg, "exo").get("base_url")
    captur_command = os.getenv("PROOFSIGHT_CAPTUR_COMMAND") or _partner_cfg(cfg, "captur").get("command")
    overmind_endpoint = os.getenv("PROOFSIGHT_OVERMIND_ENDPOINT") or _partner_cfg(cfg, "overmind").get("endpoint")

    return {
        "captur": {
            "mode": "local_trust_gate" if not captur_command else "external_command_configured",
            "active": True,
            "official_sdk_active": bool(captur_command),
            "notes": "Local image validation rejects dark/blank/unusable evidence; official Captur SDK/CLI can replace this when available.",
        },
        "cognee": {
            "mode": "sqlite_plus_ingest_queue",
            "active": True,
            "python_package_available": cognee_available,
            "canonical_db": str(db_path),
            "ingest_queue": str(trace_dir / "cognee_ingest_queue.jsonl"),
            "notes": "Structured memory is stored locally now; Cognee ingest queue is emitted for future official Cognee processing.",
        },
        "overmind": {
            "mode": "local_trace_jsonl" if not overmind_endpoint else "endpoint_configured",
            "active": True,
            "endpoint_configured": bool(overmind_endpoint),
            "trace_stream": str(trace_dir / "overmind_traces.jsonl"),
            "notes": "Every inspection emits a trace suitable for failure-pattern/improvement analysis.",
        },
        "exo": {
            "mode": "ollama_local" if not exo_endpoint else "exo_endpoint_configured",
            "active": bool(exo_endpoint),
            "endpoint_configured": bool(exo_endpoint),
            "notes": "Local Ollama is active. Exo Labs distributed inference should be enabled only when a real Exo cluster endpoint exists.",
        },
        "cosine": {
            "mode": "engineering_lane_metadata",
            "active": False,
            "notes": "Cosine/Lumen is not a runtime dependency; use as coding/review partner if access is available.",
        },
    }


def write_partner_artifacts(cfg: dict[str, Any], inspection_record: dict[str, Any]) -> dict[str, Any]:
    """Emit partner-facing artifacts for one inspection.

    Returns a compact object to embed in the main trace.
    """
    actions = _actions(cfg)
    trace_dir = Path(actions.get("trace_dir", "/home/dave/hse-pi-agent/traces"))
    trace_dir.mkdir(parents=True, exist_ok=True)

    statuses = partner_status(cfg)

    cognee_record = {
        "type": "proofsight_inspection_memory",
        "inspection_id": inspection_record.get("inspection_id"),
        "created_at": inspection_record.get("created_at"),
        "location": inspection_record.get("location"),
        "status": inspection_record.get("status"),
        "image": inspection_record.get("image"),
        "validation": inspection_record.get("validation"),
        "summary": (inspection_record.get("findings") or {}).get("summary"),
        "findings": (inspection_record.get("findings") or {}).get("findings") or [],
    }
    cognee_queue = Path(statuses["cognee"]["ingest_queue"])
    with cognee_queue.open("a", encoding="utf-8") as f:
        f.write(json.dumps(cognee_record, ensure_ascii=False) + "\n")

    overmind_record = {
        "type": "proofsight_agent_trace",
        "inspection_id": inspection_record.get("inspection_id"),
        "created_at": inspection_record.get("created_at"),
        "status": inspection_record.get("status"),
        "failure_pattern_candidate": inspection_record.get("status") in {"image_rejected", "no_finding"},
        "validation_reason": (inspection_record.get("validation") or {}).get("reason"),
        "model_outputs": {
            "vision_text": inspection_record.get("vision_text"),
            "findings": inspection_record.get("findings"),
        },
        "human_review_required": True,
    }
    overmind_stream = Path(statuses["overmind"]["trace_stream"])
    with overmind_stream.open("a", encoding="utf-8") as f:
        f.write(json.dumps(overmind_record, ensure_ascii=False) + "\n")

    return {
        "status": statuses,
        "artifacts": {
            "cognee_ingest_record_appended": str(cognee_queue),
            "overmind_trace_appended": str(overmind_stream),
        },
    }
