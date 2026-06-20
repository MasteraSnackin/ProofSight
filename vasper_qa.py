#!/usr/bin/env python3
"""ProofSight — local trusted health & safety inspection agent for Raspberry Pi 5.

Runs fully on-device:
- webcam capture via ffmpeg/v4l2
- photo validation with Pillow
- local Ollama moondream vision
- local Ollama qwen/gemma reasoning
- SQLite inspection memory
- Markdown report/action plan
"""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import json
import re
import sqlite3
import subprocess
import sys
import time
import urllib.request
import uuid
from pathlib import Path
from typing import Any

import yaml
from PIL import Image, ImageStat

from partners import partner_status, write_partner_artifacts

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.yaml"


def now_utc() -> str:
    return dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat()


def load_config() -> dict[str, Any]:
    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def sh(cmd: list[str], timeout: int = 120, check: bool = True) -> subprocess.CompletedProcess[str]:
    p = subprocess.run(cmd, text=True, capture_output=True, timeout=timeout)
    if check and p.returncode != 0:
        raise RuntimeError(f"Command failed: {' '.join(cmd)}\nSTDOUT:\n{p.stdout}\nSTDERR:\n{p.stderr}")
    return p


def ensure_dirs(cfg: dict[str, Any]) -> None:
    for key in ["evidence_dir", "reports_dir", "trace_dir"]:
        Path(cfg["actions"][key]).mkdir(parents=True, exist_ok=True)
    Path(cfg["actions"]["db_path"]).parent.mkdir(parents=True, exist_ok=True)


def init_db(cfg: dict[str, Any]) -> None:
    db = Path(cfg["actions"]["db_path"])
    with sqlite3.connect(db) as con:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS inspections (
                id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                location TEXT,
                evidence_path TEXT,
                valid_image INTEGER,
                image_validation_json TEXT,
                vision_text TEXT,
                findings_json TEXT,
                report_path TEXT,
                trace_path TEXT,
                status TEXT NOT NULL
            )
            """
        )
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS action_items (
                id TEXT PRIMARY KEY,
                inspection_id TEXT NOT NULL,
                title TEXT,
                risk_level TEXT,
                responsible_role TEXT,
                deadline TEXT,
                status TEXT,
                details_json TEXT,
                FOREIGN KEY (inspection_id) REFERENCES inspections(id)
            )
            """
        )


def configure_camera(cfg: dict[str, Any]) -> None:
    device = cfg["camera"]["device"]
    controls = [
        f"power_line_frequency={cfg['camera'].get('power_line_frequency', 1)}",
        "focus_automatic_continuous=1",
        "auto_exposure=3",
        "white_balance_automatic=1",
    ]
    for ctrl in controls:
        sh(["v4l2-ctl", "-d", device, f"--set-ctrl={ctrl}"], timeout=10, check=False)


def capture_image(cfg: dict[str, Any], inspection_id: str) -> Path:
    cam = cfg["camera"]
    out = Path(cfg["actions"]["evidence_dir"]) / f"{inspection_id}.jpg"
    configure_camera(cfg)
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "warning",
        "-f", "v4l2",
        "-input_format", "mjpeg",
        "-video_size", f"{cam['width']}x{cam['height']}",
        "-framerate", str(cam.get("framerate", 10)),
        "-i", cam["device"],
        "-vf", f"select='gte(n,{cam.get('warm_frames', 20)})'",
        "-frames:v", "1",
        "-q:v", "2",
        str(out),
        "-y",
    ]
    sh(cmd, timeout=120)
    return out


def validate_image(path: Path, cfg: dict[str, Any]) -> dict[str, Any]:
    val_cfg = cfg["validation"]
    result: dict[str, Any] = {
        "path": str(path),
        "exists": path.exists(),
        "size_bytes": path.stat().st_size if path.exists() else 0,
    }
    if not path.exists():
        result.update({"valid": False, "reason": "capture_file_missing"})
        return result

    im = Image.open(path).convert("RGB")
    stat = ImageStat.Stat(im)
    mean_rgb = [round(x, 2) for x in stat.mean]
    extrema = stat.extrema
    max_mean = max(stat.mean)
    uniqueish = any(lo != hi for lo, hi in extrema)
    too_dark = max_mean < val_cfg["min_mean_brightness"]
    too_small = result["size_bytes"] < val_cfg["min_file_size_bytes"]
    valid = not (too_dark or too_small or not uniqueish)
    reason = "ok"
    if too_dark:
        reason = "image_too_dark_or_obstructed"
    elif not uniqueish:
        reason = "image_blank_uniform_pixels"
    elif too_small:
        reason = "image_file_suspiciously_small"

    result.update(
        {
            "resolution": list(im.size),
            "mean_rgb": mean_rgb,
            "extrema": extrema,
            "too_dark": too_dark,
            "too_small": too_small,
            "uniform_pixels": not uniqueish,
            "valid": valid,
            "reason": reason,
            "trust_layer": "captur_style_local_validation",
        }
    )
    return result


def ollama_generate(cfg: dict[str, Any], model: str, prompt: str, images: list[Path] | None = None, num_predict: int = 512) -> dict[str, Any]:
    """Call Pi-local Ollama native /api/generate.

    Used for Scenario A and always used for the current moondream vision step.
    """
    base = cfg["models"]["ollama_base_url"].rstrip("/")
    payload: dict[str, Any] = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": 0, "num_predict": num_predict},
    }
    if images:
        encoded = []
        for p in images:
            with p.open("rb") as f:
                encoded.append(base64.b64encode(f.read()).decode())
        payload["images"] = encoded
    start = time.time()
    req = urllib.request.Request(
        f"{base}/api/generate",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=300) as r:
        data = json.loads(r.read().decode())
    data["elapsed_s"] = round(time.time() - start, 2)
    data["provider"] = "ollama"
    data["model"] = model
    return data


def lmstudio_chat(cfg: dict[str, Any], model: str, prompt: str, num_predict: int = 512) -> dict[str, Any]:
    """Call LM Studio's OpenAI-compatible chat completions API.

    LM Studio must be reachable from the Pi over LAN/Tailscale, e.g.
    http://100.106.72.5:1234/v1. This function intentionally does not support
    images; ProofSight keeps moondream vision on Pi-local Ollama and sends only
    text observations to LM Studio for reasoning/report decisions.
    """
    models_cfg = cfg.get("models") or {}
    base = models_cfg.get("lmstudio_base_url", "http://127.0.0.1:1234/v1").rstrip("/")
    payload: dict[str, Any] = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": "You are ProofSight, a cautious UK-style health and safety inspection assistant. Return concise, evidence-led outputs and never invent hazards.",
            },
            {"role": "user", "content": prompt},
        ],
        "temperature": 0,
        "max_tokens": num_predict,
        "stream": False,
    }
    start = time.time()
    req = urllib.request.Request(
        f"{base}/chat/completions",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "Authorization": "Bearer lm-studio"},
    )
    with urllib.request.urlopen(req, timeout=300) as r:
        raw = json.loads(r.read().decode())
    content = raw.get("choices", [{}])[0].get("message", {}).get("content", "")
    return {
        "response": content,
        "raw": raw,
        "elapsed_s": round(time.time() - start, 2),
        "provider": "lmstudio",
        "base_url": base,
        "model": model,
    }


def reasoning_generate(cfg: dict[str, Any], model: str, prompt: str, num_predict: int = 512) -> dict[str, Any]:
    """Generate text for non-vision reasoning using the configured provider."""
    provider = (cfg.get("models") or {}).get("provider", "ollama")
    if provider == "lmstudio":
        return lmstudio_chat(cfg, model, prompt, num_predict=num_predict)
    return ollama_generate(cfg, model, prompt, num_predict=num_predict)


def strip_model_noise(text: str) -> str:
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()
    text = re.sub(r"^```(?:json)?", "", text.strip())
    text = re.sub(r"```$", "", text.strip())
    return text.strip()


def extract_json(text: str) -> Any:
    clean = strip_model_noise(text)
    try:
        return json.loads(clean)
    except Exception:
        # Try first {...} or [...] block.
        m = re.search(r"(\{.*\}|\[.*\])", clean, flags=re.S)
        if m:
            return json.loads(m.group(1))
        raise


def vision_describe(cfg: dict[str, Any], image_path: Path) -> dict[str, Any]:
    prompt = (
        "You are ProofSight, a health and safety inspection vision assistant. "
        "Describe the image objectively. Focus on visible workplace safety hazards: blocked exits, trailing cables, poor housekeeping, spills, damaged equipment, unsafe stacking, PPE issues, electrical hazards, fire risks, trip hazards, and obstruction. "
        "If no hazards are visible, say that. Do not invent details you cannot see."
    )
    return ollama_generate(cfg, cfg["models"]["vision"], prompt, images=[image_path], num_predict=220)


def classify_findings(cfg: dict[str, Any], location: str, validation: dict[str, Any], vision_text: str) -> dict[str, Any]:
    prompt = f"""
You are ProofSight, a UK-style health and safety inspection assistant.
Convert the visual observation into STRICT JSON only. Do not include markdown.

Location: {location}
Image validation: {json.dumps(validation, ensure_ascii=False)}
Vision observation: {vision_text}

Return this schema:
{{
  "summary": "one sentence summary",
  "findings": [
    {{
      "title": "short finding title",
      "hazard": "hazard description",
      "evidence": "what in the image supports this",
      "people_at_risk": ["staff", "visitors", "contractors"],
      "severity": "low|medium|high",
      "likelihood": "unlikely|possible|likely",
      "risk_level": "low|medium|high",
      "immediate_action": "specific immediate control",
      "corrective_action": "specific permanent or preventative action",
      "responsible_role": "role, not named person",
      "deadline": "timeframe such as immediate/24 hours/7 days",
      "status": "open",
      "review_required": true
    }}
  ],
  "no_visible_hazard": false,
  "confidence": "low|medium|high"
}}

Rules:
- If the observation is blank, dark, unclear, or says no hazards are visible, return findings: [] and no_visible_hazard: true.
- Do not make legal claims or cite regulations unless explicitly visible/known.
- Keep actions practical and inspectable.
""".strip()
    try:
        data = reasoning_generate(cfg, cfg["models"]["reasoning"], prompt, num_predict=700)
    except Exception as exc:
        return {
            "summary": f"Reasoning provider failed: {exc}",
            "findings": [],
            "no_visible_hazard": True,
            "confidence": "low",
            "provider_error": True,
            "model_provider": (cfg.get("models") or {}).get("provider", "unknown"),
            "model_name": (cfg.get("models") or {}).get("reasoning", "unknown"),
            "model_base_url": (cfg.get("models") or {}).get("lmstudio_base_url") or (cfg.get("models") or {}).get("ollama_base_url"),
        }
    try:
        parsed = extract_json(data.get("response", ""))
    except Exception as exc:
        parsed = {
            "summary": "Model returned unparseable JSON; human review required.",
            "findings": [],
            "no_visible_hazard": True,
            "confidence": "low",
            "parse_error": str(exc),
            "raw_response": data.get("response", ""),
        }
    parsed["model_elapsed_s"] = data.get("elapsed_s")
    parsed["model_provider"] = data.get("provider")
    parsed["model_name"] = data.get("model")
    if data.get("base_url"):
        parsed["model_base_url"] = data.get("base_url")
    return parsed



def finding_blob(finding: dict[str, Any]) -> str:
    """Compact searchable text for one finding/action."""
    parts = [
        finding.get("title"),
        finding.get("hazard"),
        finding.get("evidence"),
        finding.get("risk_level"),
        finding.get("immediate_action"),
        finding.get("corrective_action"),
        finding.get("responsible_role"),
        finding.get("deadline"),
        finding.get("status"),
    ]
    return " ".join(str(x) for x in parts if x).lower()


def memory_terms(text: str) -> set[str]:
    """Extract stable-ish terms for local recurrence matching.

    This is deliberately simple and offline: it gives ProofSight useful memory
    behaviour now, while the Cognee JSONL stream remains ready for official
    ingestion later.
    """
    stop = {
        "and", "the", "with", "from", "that", "this", "into", "near", "area",
        "risk", "hazard", "action", "visible", "possible", "inspection", "image",
        "evidence", "medium", "low", "high", "open", "review", "required",
    }
    words = set(re.findall(r"[a-z][a-z0-9_-]{2,}", text.lower()))
    return {w for w in words if w not in stop}


def hazard_category(finding: dict[str, Any]) -> str:
    blob = finding_blob(finding)
    categories = [
        ("trip_hazard", ["trip", "cable", "walkway", "floor", "obstruction", "trailing"]),
        ("fire_or_exit_risk", ["fire", "exit", "escape", "blocked", "evacuation"]),
        ("electrical_hazard", ["electrical", "plug", "socket", "charger", "extension"]),
        ("housekeeping", ["clutter", "housekeeping", "bag", "items", "stored"]),
        ("slip_hazard", ["spill", "wet", "slip", "liquid"]),
        ("ergonomic_or_workstation", ["chair", "desk", "screen", "workstation", "posture"]),
    ]
    for name, tokens in categories:
        if any(t in blob for t in tokens):
            return name
    return "general_hse_observation"


def build_memory_context(cfg: dict[str, Any], location: str, findings: dict[str, Any], limit: int = 5) -> dict[str, Any]:
    """Return local Cognee-style memory context from prior inspections.

    SQLite remains the local source of truth. This function gives reports a
    concrete "similar previous findings" section and creates data that Cognee
    can later ingest as graph/vector memory.
    """
    db = Path(cfg["actions"]["db_path"])
    current_findings = findings.get("findings") or []
    query_text = " ".join([location] + [finding_blob(f) for f in current_findings])
    query_terms = memory_terms(query_text)
    context: dict[str, Any] = {
        "memory_layer": "cognee_style_local_recall",
        "query_terms": sorted(query_terms)[:30],
        "similar_previous_findings": [],
        "recurring_issue": False,
        "suggested_escalation": "No related previous findings found in local memory.",
    }
    if not db.exists() or not query_terms:
        return context

    try:
        with sqlite3.connect(db) as con:
            con.row_factory = sqlite3.Row
            rows = con.execute(
                """
                SELECT id, created_at, location, status, findings_json, report_path
                FROM inspections
                ORDER BY created_at DESC
                LIMIT 250
                """
            ).fetchall()
    except Exception as exc:
        context["error"] = f"local memory lookup failed: {exc}"
        return context

    scored: list[tuple[int, dict[str, Any]]] = []
    for row in rows:
        try:
            prior = json.loads(row["findings_json"] or "{}")
        except Exception:
            prior = {}
        for f in prior.get("findings") or []:
            prior_text = " ".join([str(row["location"] or ""), finding_blob(f)])
            prior_terms = memory_terms(prior_text)
            overlap = query_terms & prior_terms
            if not overlap:
                continue
            score = len(overlap)
            same_location = (location or "").strip().lower() and (location or "").strip().lower() in (row["location"] or "").strip().lower()
            if same_location:
                score += 2
            scored.append((score, {
                "inspection_id": row["id"],
                "created_at": row["created_at"],
                "location": row["location"],
                "status": row["status"],
                "title": f.get("title") or f.get("hazard") or "Untitled finding",
                "risk_level": f.get("risk_level"),
                "action_status": f.get("status", "open"),
                "hazard_category": hazard_category(f),
                "overlap_terms": sorted(overlap)[:12],
                "report_path": row["report_path"],
            }))

    seen: set[tuple[str, str]] = set()
    similar: list[dict[str, Any]] = []
    for _score, item in sorted(scored, key=lambda x: x[0], reverse=True):
        key = (item["inspection_id"], item["title"])
        if key in seen:
            continue
        seen.add(key)
        similar.append(item)
        if len(similar) >= limit:
            break

    context["similar_previous_findings"] = similar
    context["recurring_issue"] = len(similar) >= 2
    if similar:
        context["suggested_escalation"] = (
            "Related previous findings exist. Check whether earlier actions were closed; "
            "if repeated, escalate from temporary housekeeping to a permanent control."
        )
    return context

def write_report(cfg: dict[str, Any], inspection_id: str, created_at: str, location: str, image: Path, validation: dict[str, Any], vision_text: str, findings: dict[str, Any], memory_context: dict[str, Any] | None = None) -> Path:
    report_path = Path(cfg["actions"]["reports_dir"]) / f"{inspection_id}.md"
    lines = [
        f"# ProofSight Health & Safety Inspection Report",
        "",
        f"**Inspection ID:** `{inspection_id}`",
        f"**Created:** {created_at}",
        f"**Site:** {cfg['agent'].get('site_name', 'Unspecified site')}",
        f"**Location:** {location}",
        f"**Inspector:** {cfg['agent'].get('inspector_name', 'ProofSight')}",
        f"**Evidence:** `{image}`",
        "",
        "## Image Trust / Validation",
        "",
        f"- **Valid:** {validation.get('valid')}",
        f"- **Reason:** {validation.get('reason')}",
        f"- **Resolution:** {validation.get('resolution')}",
        f"- **Mean RGB:** {validation.get('mean_rgb')}",
        f"- **Trust layer:** {validation.get('trust_layer')}",
        "",
        "## Vision Observation",
        "",
        vision_text.strip() or "No useful vision description returned.",
        "",
        "## Summary",
        "",
        findings.get("summary", "No summary."),
        "",
        "## Findings and Action Plan",
        "",
    ]
    fs = findings.get("findings") or []
    if not fs:
        lines += ["No actionable visible hazards were identified, or image quality was insufficient. Human review recommended if this was unexpected.", ""]
    for i, f in enumerate(fs, 1):
        lines += [
            f"### Finding {i}: {f.get('title', 'Untitled finding')}",
            "",
            f"- **Hazard:** {f.get('hazard', '')}",
            f"- **Evidence:** {f.get('evidence', '')}",
            f"- **People at risk:** {', '.join(f.get('people_at_risk', []) or [])}",
            f"- **Severity:** {f.get('severity', '')}",
            f"- **Likelihood:** {f.get('likelihood', '')}",
            f"- **Risk level:** **{f.get('risk_level', '')}**",
            f"- **Immediate action:** {f.get('immediate_action', '')}",
            f"- **Corrective action:** {f.get('corrective_action', '')}",
            f"- **Responsible role:** {f.get('responsible_role', '')}",
            f"- **Deadline:** {f.get('deadline', '')}",
            f"- **Status:** {f.get('status', 'open')}",
            "",
        ]
    memory_context = memory_context or findings.get("memory_context") or {}
    if memory_context:
        lines += [
            "## Memory Context",
            "",
            f"- **Memory layer:** {memory_context.get('memory_layer', 'local')}",
            f"- **Recurring issue:** {memory_context.get('recurring_issue', False)}",
            f"- **Suggested escalation:** {memory_context.get('suggested_escalation', '')}",
            "",
        ]
        similar = memory_context.get("similar_previous_findings") or []
        if similar:
            lines += ["| Date | Location | Previous finding | Risk | Status |", "|---|---|---|---|---|"]
            for item in similar[:5]:
                lines.append(
                    f"| {item.get('created_at', '')} | {item.get('location', '')} | {item.get('title', '')} | {item.get('risk_level', '')} | {item.get('action_status', item.get('status', ''))} |"
                )
            lines.append("")
        else:
            lines += ["No similar previous findings were found in local memory.", ""]

    lines += [
        "## Review Note",
        "",
        "This is an AI-generated local inspection draft. A competent human should review findings before relying on them for formal compliance decisions.",
        "",
    ]
    report_path.write_text("\n".join(lines), encoding="utf-8")
    return report_path


def save_trace(cfg: dict[str, Any], inspection_id: str, trace: dict[str, Any]) -> Path:
    path = Path(cfg["actions"]["trace_dir"]) / f"{inspection_id}.json"
    path.write_text(json.dumps(trace, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def save_db(cfg: dict[str, Any], inspection_id: str, created_at: str, location: str, image: Path, validation: dict[str, Any], vision_text: str, findings: dict[str, Any], report_path: Path, trace_path: Path, status: str) -> None:
    with sqlite3.connect(cfg["actions"]["db_path"]) as con:
        con.execute(
            "INSERT OR REPLACE INTO inspections VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                inspection_id, created_at, location, str(image), int(bool(validation.get("valid"))),
                json.dumps(validation, ensure_ascii=False), vision_text,
                json.dumps(findings, ensure_ascii=False), str(report_path), str(trace_path), status,
            ),
        )
        for f in findings.get("findings") or []:
            action_id = f"ACT-{uuid.uuid4().hex[:8].upper()}"
            con.execute(
                "INSERT INTO action_items VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    action_id, inspection_id, f.get("title"), f.get("risk_level"),
                    f.get("responsible_role"), f.get("deadline"), f.get("status", "open"),
                    json.dumps(f, ensure_ascii=False),
                ),
            )


def inspect_once(args: argparse.Namespace) -> int:
    cfg = load_config()
    ensure_dirs(cfg)
    init_db(cfg)
    location = args.location or cfg["inspection"].get("default_location", "Unspecified location")
    inspection_id = f"HSE-{dt.datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6].upper()}"
    created = now_utc()
    image = Path(args.image) if args.image else capture_image(cfg, inspection_id)
    validation = validate_image(image, cfg)

    vision_text = ""
    findings: dict[str, Any]
    if not validation.get("valid") and cfg["validation"].get("reject_blank_or_dark", True) and not args.force:
        findings = {
            "summary": f"Image rejected by local trust validation: {validation.get('reason')}",
            "findings": [],
            "no_visible_hazard": True,
            "confidence": "low",
        }
        status = "image_rejected"
    else:
        vision = vision_describe(cfg, image)
        vision_text = strip_model_noise(vision.get("response", ""))
        findings = classify_findings(cfg, location, validation, vision_text)
        if findings.get("provider_error"):
            status = "model_error"
        else:
            status = "review_required" if findings.get("findings") else "no_finding"

    memory_context = build_memory_context(cfg, location, findings)
    findings["memory_context"] = memory_context
    report = write_report(cfg, inspection_id, created, location, image, validation, vision_text, findings, memory_context)
    trace_obj = {
        "inspection_id": inspection_id,
        "created_at": created,
        "agent": cfg["agent"],
        "location": location,
        "image": str(image),
        "validation": validation,
        "vision_text": vision_text,
        "findings": findings,
        "memory_context": memory_context,
        "status": status,
    }
    trace_obj["partners"] = write_partner_artifacts(cfg, trace_obj)
    trace = save_trace(cfg, inspection_id, trace_obj)
    save_db(cfg, inspection_id, created, location, image, validation, vision_text, findings, report, trace, status)

    print(json.dumps({
        "inspection_id": inspection_id,
        "status": status,
        "valid_image": validation.get("valid"),
        "validation_reason": validation.get("reason"),
        "findings_count": len(findings.get("findings") or []),
        "evidence": str(image),
        "report": str(report),
        "trace": str(trace),
    }, indent=2))
    return 0 if status != "image_rejected" else 2


def monitor(args: argparse.Namespace) -> int:
    while True:
        try:
            inspect_once(args)
        except Exception as exc:
            print(json.dumps({"time": now_utc(), "error": str(exc)}, indent=2), file=sys.stderr)
        time.sleep(args.interval)


def show_partners(args: argparse.Namespace) -> int:
    cfg = load_config()
    print(json.dumps(partner_status(cfg), indent=2))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="ProofSight local trusted HSE inspection agent")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_inspect = sub.add_parser("inspect", help="run one inspection")
    p_inspect.add_argument("--location", default=None)
    p_inspect.add_argument("--image", default=None, help="use existing image instead of webcam capture")
    p_inspect.add_argument("--force", action="store_true", help="run vision/reasoning even if image validation rejects")
    p_monitor = sub.add_parser("monitor", help="run repeated inspections")
    p_monitor.add_argument("--interval", type=int, default=300)
    p_monitor.add_argument("--location", default=None)
    p_monitor.add_argument("--image", default=None)
    p_monitor.add_argument("--force", action="store_true")
    sub.add_parser("partners", help="show sponsor/partner integration status")
    args = ap.parse_args()
    if args.cmd == "inspect":
        return inspect_once(args)
    if args.cmd == "monitor":
        return monitor(args)
    if args.cmd == "partners":
        return show_partners(args)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
