#!/usr/bin/env python3
"""ProofSight local dashboard v2.

Stdlib-only dashboard for Raspberry Pi:
- status + camera health
- recent inspections and reports
- action plan board
- human review controls
- audit-pack ZIP export
- sponsor/partner status
"""
from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import os
import sqlite3
import subprocess
import sys
import time
import zipfile
from collections import Counter
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import yaml

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.yaml"
sys.path.insert(0, str(ROOT))
from partners import partner_status  # noqa: E402


def now() -> str:
    return dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat()


def load_config() -> dict:
    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def db_path() -> Path:
    return Path(load_config()["actions"]["db_path"])


def db_connect() -> sqlite3.Connection:
    con = sqlite3.connect(db_path())
    con.row_factory = sqlite3.Row
    ensure_schema(con)
    return con


def ensure_schema(con: sqlite3.Connection) -> None:
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS reviews (
            inspection_id TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            note TEXT,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (inspection_id) REFERENCES inspections(id)
        )
        """
    )
    con.commit()


def run(cmd: list[str], timeout: int = 8) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, text=True, capture_output=True, timeout=timeout)
        return p.returncode, (p.stdout + p.stderr).strip()
    except Exception as exc:
        return 999, str(exc)


def service_state(name: str) -> str:
    rc, out = run(["systemctl", "--user", "is-active", name], timeout=5)
    return out.strip() if rc == 0 else f"inactive/error ({out.strip()})"


def read_text(path: Path, max_chars: int = 50000) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")[:max_chars]
    except Exception as exc:
        return f"Unable to read {path}: {exc}"


def parse_json_maybe(value: str | None):
    if not value:
        return None
    try:
        return json.loads(value)
    except Exception:
        return value


def latest_inspections(limit: int = 25) -> list[dict]:
    if not db_path().exists():
        return []
    with db_connect() as con:
        rows = con.execute(
            """
            SELECT i.*, r.status AS review_status, r.note AS review_note, r.updated_at AS review_updated_at
            FROM inspections i
            LEFT JOIN reviews r ON r.inspection_id = i.id
            ORDER BY i.created_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    out = []
    for row in rows:
        d = dict(row)
        d["image_validation_json"] = parse_json_maybe(d.get("image_validation_json"))
        d["findings_json"] = parse_json_maybe(d.get("findings_json"))
        out.append(d)
    return out


def actions(limit: int = 100) -> list[dict]:
    if not db_path().exists():
        return []
    with db_connect() as con:
        rows = con.execute(
            """
            SELECT a.*, i.created_at, i.location, i.evidence_path, i.report_path
            FROM action_items a
            LEFT JOIN inspections i ON i.id = a.inspection_id
            ORDER BY
              CASE lower(COALESCE(a.risk_level,'')) WHEN 'high' THEN 0 WHEN 'medium' THEN 1 WHEN 'low' THEN 2 ELSE 3 END,
              i.created_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    out = []
    for row in rows:
        d = dict(row)
        d["details_json"] = parse_json_maybe(d.get("details_json"))
        out.append(d)
    return out


def dashboard_stats(inspections: list[dict], acts: list[dict]) -> dict:
    rejected = sum(1 for i in inspections if i.get("status") == "image_rejected")
    valid = sum(1 for i in inspections if i.get("valid_image"))
    review_needed = sum(1 for i in inspections if not i.get("review_status") and i.get("status") != "image_rejected")
    open_actions = sum(1 for a in acts if (a.get("status") or "open").lower() in {"open", "in_progress"})
    high_actions = sum(1 for a in acts if (a.get("risk_level") or "").lower() == "high")
    return {"valid_images": valid, "rejected_images": rejected, "review_needed": review_needed, "open_actions": open_actions, "high_actions": high_actions}


def camera_health(latest: dict | None) -> dict:
    if not latest:
        return {"state": "unknown", "reason": "no inspections yet", "brightness": None, "good": None}
    val = latest.get("image_validation_json") or {}
    mean = val.get("mean_rgb") or []
    brightness = round(sum(mean) / len(mean), 2) if mean else None
    valid = bool(latest.get("valid_image"))
    return {
        "state": "usable" if valid else "needs attention",
        "reason": val.get("reason", "unknown"),
        "brightness": brightness,
        "resolution": val.get("resolution"),
        "good": valid,
    }


def safe_relative_file(base: Path, requested: str) -> Path | None:
    p = (base / requested).resolve()
    try:
        p.relative_to(base.resolve())
    except ValueError:
        return None
    return p if p.exists() and p.is_file() else None


def badge(text: str, good: bool | None = None) -> str:
    cls = "badge"
    if good is True:
        cls += " good"
    elif good is False:
        cls += " bad"
    return f'<span class="{cls}">{html.escape(str(text))}</span>'


def button(action: str, label: str, fields: dict[str, str], cls: str = "") -> str:
    hidden = "".join(f'<input type="hidden" name="{html.escape(k)}" value="{html.escape(v)}">' for k, v in fields.items())
    return f'<form method="post" action="{html.escape(action)}" class="inline">{hidden}<button class="{cls}" type="submit">{html.escape(label)}</button></form>'


def render_report(text: str) -> str:
    return "<pre class=report>" + html.escape(text) + "</pre>"


def reporting_dataset(limit: int = 500) -> dict:
    inspections = latest_inspections(limit)
    acts = actions(500)
    status_counts = Counter(i.get("status") or "unknown" for i in inspections)
    review_counts = Counter(i.get("review_status") or "unreviewed" for i in inspections)
    location_counts = Counter(i.get("location") or "Unspecified" for i in inspections)
    valid_count = sum(1 for i in inspections if i.get("valid_image"))
    rejected_count = sum(1 for i in inspections if i.get("status") == "image_rejected")
    finding_count = 0
    for i in inspections:
        findings = i.get("findings_json") or {}
        if isinstance(findings, dict):
            finding_count += len(findings.get("findings") or [])
    return {
        "generated_at": now(),
        "total_inspections": len(inspections),
        "valid_images": valid_count,
        "rejected_images": rejected_count,
        "finding_count": finding_count,
        "open_actions": sum(1 for a in acts if (a.get("status") or "open").lower() in {"open", "in_progress"}),
        "status_counts": dict(status_counts),
        "review_counts": dict(review_counts),
        "top_locations": location_counts.most_common(12),
        "inspections": inspections,
        "actions": acts,
        "memory": memory_summary(inspections, acts),
    }



def finding_blob(finding: dict) -> str:
    parts = [finding.get(k) for k in [
        "title", "hazard", "evidence", "risk_level", "immediate_action", "corrective_action", "status",
    ]]
    return " ".join(str(x) for x in parts if x).lower()


def hazard_category(finding: dict) -> str:
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


def jsonl_line_count(path: Path) -> int:
    try:
        if not path.exists():
            return 0
        with path.open("r", encoding="utf-8", errors="replace") as f:
            return sum(1 for line in f if line.strip())
    except Exception:
        return 0


def memory_summary(inspections: list[dict] | None = None, acts: list[dict] | None = None) -> dict:
    inspections = inspections if inspections is not None else latest_inspections(500)
    acts = acts if acts is not None else actions(500)
    cfg = load_config()
    trace_dir = Path(cfg["actions"]["trace_dir"])
    category_counts: Counter[str] = Counter()
    recurring_locations: Counter[str] = Counter()
    memory_context_hits = 0
    for i in inspections:
        findings = i.get("findings_json") or {}
        if not isinstance(findings, dict):
            continue
        if (findings.get("memory_context") or {}).get("similar_previous_findings"):
            memory_context_hits += 1
        for f in findings.get("findings") or []:
            category_counts[hazard_category(f)] += 1
            if i.get("location"):
                recurring_locations[i.get("location")] += 1
    open_recurring_actions = 0
    for a in acts:
        status = (a.get("status") or "open").lower()
        details = a.get("details_json") or {}
        if status in {"open", "in_progress"} and isinstance(details, dict):
            if category_counts.get(hazard_category(details), 0) > 1:
                open_recurring_actions += 1
    return {
        "memory_layer": "sqlite_plus_cognee_style_jsonl",
        "remembered_inspections": len(inspections),
        "cognee_queue_records": jsonl_line_count(trace_dir / "cognee_ingest_queue.jsonl"),
        "overmind_trace_records": jsonl_line_count(trace_dir / "overmind_traces.jsonl"),
        "top_hazard_categories": category_counts.most_common(8),
        "top_locations": recurring_locations.most_common(8),
        "memory_context_hits": memory_context_hits,
        "open_recurring_actions": open_recurring_actions,
    }

def csv_cell(value) -> str:
    s = "" if value is None else str(value)
    return '"' + s.replace('"', '""') + '"'


def reports_csv() -> bytes:
    data = reporting_dataset(1000)
    headers = ["id", "created_at", "location", "status", "valid_image", "review_status", "findings_count", "report_path", "evidence_path"]
    lines = [",".join(headers)]
    for i in data["inspections"]:
        findings = i.get("findings_json") or {}
        count = len(findings.get("findings") or []) if isinstance(findings, dict) else 0
        row = [i.get("id"), i.get("created_at"), i.get("location"), i.get("status"), i.get("valid_image"), i.get("review_status") or "unreviewed", count, i.get("report_path"), i.get("evidence_path")]
        lines.append(",".join(csv_cell(v) for v in row))
    return ("\n".join(lines) + "\n").encode("utf-8")


def bar(label: str, value: int, max_value: int) -> str:
    pct = 0 if max_value <= 0 else int((value / max_value) * 100)
    return f'<div class="barrow"><span>{html.escape(label)}</span><b>{value}</b><div class="bar"><i style="width:{pct}%"></i></div></div>'


def render_reports_dashboard() -> str:
    data = reporting_dataset(500)
    max_status = max(data["status_counts"].values() or [1])
    max_loc = max([v for _, v in data["top_locations"]] or [1])
    status_bars = "".join(bar(k, v, max_status) for k, v in sorted(data["status_counts"].items()))
    review_bars = "".join(bar(k, v, max(data["review_counts"].values() or [1])) for k, v in sorted(data["review_counts"].items()))
    loc_bars = "".join(bar(k, v, max_loc) for k, v in data["top_locations"])
    memory = data.get("memory") or {}
    max_haz = max([v for _, v in memory.get("top_hazard_categories", [])] or [1])
    memory_bars = "".join(bar(k, v, max_haz) for k, v in memory.get("top_hazard_categories", [])) or '<p class=muted>No hazard memory yet.</p>'
    rows = []
    for i in data["inspections"][:120]:
        findings = i.get("findings_json") or {}
        count = len(findings.get("findings") or []) if isinstance(findings, dict) else 0
        rows.append(f"""
        <tr>
          <td><strong>{html.escape(i.get('id') or '')}</strong><br><span class=muted>{html.escape(i.get('created_at') or '')}</span></td>
          <td>{html.escape(i.get('location') or '')}</td>
          <td>{badge(i.get('status') or 'unknown', i.get('status') != 'image_rejected')}</td>
          <td>{badge('valid' if i.get('valid_image') else 'rejected', bool(i.get('valid_image')))}</td>
          <td>{badge(i.get('review_status') or 'unreviewed', i.get('review_status') == 'approved')}</td>
          <td>{count}</td>
          <td><a href="/report/{html.escape(Path(i.get('report_path') or '').name)}">report</a> · <a href="/export/{html.escape(i.get('id') or '')}">ZIP</a></td>
        </tr>
        """)
    return f"""<!doctype html><html lang=en><head><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1"><title>ProofSight Reporting</title>
<style>
body{{margin:0;font-family:Inter,ui-sans-serif,system-ui,-apple-system,Segoe UI,sans-serif;background:#071018;color:#e9f4ff}} a{{color:#67e8f9;text-decoration:none}} header{{padding:22px;background:#0e1b26;border-bottom:1px solid #1d3b52}} .wrap{{max-width:1500px;margin:auto;padding:18px}} .grid{{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}} .panel{{background:#0e1b26;border:1px solid #1d3b52;border-radius:16px;padding:16px;margin-bottom:18px}} .metric{{background:#122435;border:1px solid #1d3b52;border-radius:14px;padding:14px}} .metric b{{font-size:30px}} .muted{{color:#8aa7bb;font-size:13px}} .badge{{display:inline-block;padding:4px 8px;border-radius:999px;background:#334155;color:#e2e8f0;font-size:12px}} .badge.good{{background:rgba(34,197,94,.18);color:#86efac;border:1px solid rgba(34,197,94,.35)}} .badge.bad{{background:rgba(239,68,68,.18);color:#fca5a5;border:1px solid rgba(239,68,68,.35)}} table{{width:100%;border-collapse:collapse}} td,th{{padding:8px;border-bottom:1px solid #1d3b52;text-align:left;vertical-align:top}} .barrow{{display:grid;grid-template-columns:1fr 42px 2fr;gap:10px;align-items:center;margin:8px 0}} .bar{{height:10px;background:#172a3a;border-radius:999px;overflow:hidden}} .bar i{{display:block;height:100%;background:#67e8f9}} .nav{{display:flex;gap:16px;margin-top:8px}} @media(max-width:900px){{.grid{{grid-template-columns:1fr}}.barrow{{grid-template-columns:1fr}}}}
</style></head><body><header><h1>ProofSight Reporting Dashboard</h1><div class=muted>Evidence quality, inspection status, review progress, and downloadable report data.</div><div class=nav><a href="/">Operations dashboard</a><a href="/api/reports">Reports API</a><a href="/reports.csv">Download CSV</a></div></header><div class=wrap>
<section class=grid><div class=metric><div class=muted>Total inspections</div><b>{data['total_inspections']}</b></div><div class=metric><div class=muted>Valid images</div><b>{data['valid_images']}</b></div><div class=metric><div class=muted>Rejected images</div><b>{data['rejected_images']}</b></div><div class=metric><div class=muted>Open actions</div><b>{data['open_actions']}</b></div></section>
<section class=grid style="margin-top:18px"><div class=panel><h2>Status breakdown</h2>{status_bars}</div><div class=panel><h2>Review state</h2>{review_bars}</div><div class=panel><h2>Top locations</h2>{loc_bars}</div><div class=panel><h2>Memory</h2><p><b>{memory.get('remembered_inspections',0)}</b> inspections remembered</p><p><b>{memory.get('cognee_queue_records',0)}</b> Cognee-style records queued</p><p><b>{memory.get('open_recurring_actions',0)}</b> open recurring actions</p>{memory_bars}</div></section>
<section class=panel><h2>Exports</h2><p><a href="/reports.csv">Download inspection CSV</a> · <a href="/api/reports">Open JSON report API</a></p><p class=muted>Each row links to an audit ZIP containing evidence, report, trace and manifest.</p></section>
<section class=panel><h2>Inspection report register</h2><table><thead><tr><th>ID/date</th><th>Location</th><th>Status</th><th>Evidence</th><th>Review</th><th>Findings</th><th>Links</th></tr></thead><tbody>{''.join(rows)}</tbody></table></section>
</div></body></html>"""


def create_audit_zip(inspection_id: str) -> Path:
    cfg = load_config()
    out_dir = ROOT / "exports"
    out_dir.mkdir(exist_ok=True)
    zip_path = out_dir / f"{inspection_id}-audit-pack.zip"
    with db_connect() as con:
        row = con.execute("SELECT * FROM inspections WHERE id=?", (inspection_id,)).fetchone()
    if not row:
        raise FileNotFoundError(f"Unknown inspection {inspection_id}")
    d = dict(row)
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as z:
        manifest = {"inspection_id": inspection_id, "created_at": d.get("created_at"), "location": d.get("location"), "status": d.get("status"), "generated_at": now()}
        z.writestr("manifest.json", json.dumps(manifest, indent=2))
        for label, path_s in [("evidence", d.get("evidence_path")), ("report", d.get("report_path")), ("trace", d.get("trace_path"))]:
            if path_s and Path(path_s).exists():
                suffix = Path(path_s).suffix or ".dat"
                z.write(path_s, f"{label}{suffix}")
        z.writestr("inspection_row.json", json.dumps({k: d[k] for k in d}, indent=2, default=str))
    return zip_path


def set_review(inspection_id: str, status: str, note: str = "") -> None:
    with db_connect() as con:
        con.execute(
            "INSERT INTO reviews (inspection_id,status,note,updated_at) VALUES (?,?,?,?) ON CONFLICT(inspection_id) DO UPDATE SET status=excluded.status,note=excluded.note,updated_at=excluded.updated_at",
            (inspection_id, status, note, now()),
        )
        con.commit()


def set_action_status(action_id: str, status: str) -> None:
    with db_connect() as con:
        con.execute("UPDATE action_items SET status=? WHERE id=?", (status, action_id))
        con.commit()


def render_full_index(message: str = "") -> str:
    cfg = load_config()
    # Keep the landing dashboard deliberately light for mobile / Telegram in-app browsers.
    # Full history remains available via /reports and /api/status.
    inspections = latest_inspections(12)
    stats_inspections = latest_inspections(100)
    acts = actions(25)
    latest = inspections[0] if inspections else None
    stats = dashboard_stats(stats_inspections, actions(100))
    cam = camera_health(latest)
    partners = partner_status(cfg)
    memory = memory_summary(stats_inspections, actions(100))
    latest_report = Path(latest["report_path"]) if latest and latest.get("report_path") else None
    latest_report_text = read_text(latest_report, max_chars=8000) if latest_report and latest_report.exists() else "No reports yet."
    proof_state = service_state("proofsight.service")
    dash_state = service_state("proofsight-dashboard.service")
    ollama_state = service_state("ollama.service")

    latest_img_html = "<div class=empty>No evidence image yet.</div>"
    if latest and latest.get("evidence_path"):
        img_name = Path(latest["evidence_path"]).name
        latest_img_html = f'<img class="latest-img" src="/evidence/{html.escape(img_name)}" alt="Latest evidence" loading="lazy" decoding="async">'

    action_rows = []
    for a in acts:
        img = Path(a.get("evidence_path") or "").name
        rep = Path(a.get("report_path") or "").name
        action_rows.append(f"""
        <tr>
          <td><strong>{html.escape(a.get('title') or a.get('id') or '')}</strong><br><span class=muted>{html.escape(a.get('location') or '')}</span></td>
          <td>{badge(a.get('risk_level') or 'unknown', (a.get('risk_level') or '').lower() != 'high')}</td>
          <td>{html.escape(a.get('responsible_role') or '')}<br><span class=muted>{html.escape(a.get('deadline') or '')}</span></td>
          <td>{badge(a.get('status') or 'open', (a.get('status') or 'open').lower() == 'closed')}</td>
          <td class="actions">
            {button('/action-status','Start',{'action_id':a.get('id',''),'status':'in_progress'})}
            {button('/action-status','Close',{'action_id':a.get('id',''),'status':'closed'}, 'secondary')}
            {button('/action-status','Reopen',{'action_id':a.get('id',''),'status':'open'}, 'secondary')}
            <a href="/evidence/{html.escape(img)}">image</a> <a href="/report/{html.escape(rep)}">report</a>
          </td>
        </tr>
        """)

    inspection_cards = []
    for i in inspections:
        val = i.get("image_validation_json") or {}
        findings = i.get("findings_json") or {}
        count = len(findings.get("findings") or []) if isinstance(findings, dict) else 0
        image_name = Path(i.get("evidence_path") or "").name
        report_name = Path(i.get("report_path") or "").name
        trace_name = Path(i.get("trace_path") or "").name
        review = i.get("review_status") or "needs_review"
        inspection_cards.append(f"""
        <article class="inspection">
          <div class="row between"><strong>{html.escape(i['id'])}</strong>{badge(i.get('status','unknown'), i.get('status') != 'image_rejected')}</div>
          <div class="muted">{html.escape(i.get('created_at') or '')} · {html.escape(i.get('location') or '')}</div>
          <div>Evidence: {badge('valid' if i.get('valid_image') else val.get('reason','invalid'), bool(i.get('valid_image')))} · Findings: {count} · Review: {badge(review, review == 'approved')}</div>
          <div class="links">
            <a href="/evidence/{html.escape(image_name)}">image</a>
            <a href="/report/{html.escape(report_name)}">report</a>
            <a href="/trace/{html.escape(trace_name)}">trace</a>
            <a href="/export/{html.escape(i['id'])}">audit ZIP</a>
          </div>
          <div class="row mini">
            {button('/review','Approve',{'inspection_id':i['id'],'status':'approved'}, 'secondary')}
            {button('/review','Reject',{'inspection_id':i['id'],'status':'rejected'}, 'danger')}
            {button('/review','Retake needed',{'inspection_id':i['id'],'status':'retake_required'}, 'secondary')}
          </div>
        </article>
        """)

    partner_rows = "".join(f"<tr><td>{html.escape(k)}</td><td>{badge(v.get('mode'), bool(v.get('active')))}</td><td>{html.escape(v.get('notes',''))}</td></tr>" for k, v in partners.items())
    memory_rows = "".join(f"<tr><td>{html.escape(k)}</td><td>{v}</td></tr>" for k, v in memory.get("top_hazard_categories", [])) or '<tr><td colspan=2 class=muted>No hazard memory yet.</td></tr>'

    return f"""<!doctype html><html lang=en><head><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1"><title>ProofSight Dashboard v2</title>
<style>
:root{{--bg:#071018;--panel:#0e1b26;--text:#e9f4ff;--muted:#8aa7bb;--accent:#67e8f9;--good:#22c55e;--bad:#ef4444;--warn:#f59e0b}}
*{{box-sizing:border-box}} body{{margin:0;font-family:Inter,ui-sans-serif,system-ui,-apple-system,Segoe UI,sans-serif;background:radial-gradient(circle at top left,#143853,#071018 45%);color:var(--text)}} a{{color:var(--accent);text-decoration:none}} header{{padding:22px;border-bottom:1px solid #183348;background:rgba(7,16,24,.9);position:sticky;top:0;z-index:3;backdrop-filter:blur(8px)}} h1{{margin:0;font-size:30px}} h2{{margin:0 0 12px;font-size:18px}} .sub,.muted{{color:var(--muted)}} .wrap{{max-width:1500px;margin:0 auto;padding:18px}} .grid{{display:grid;grid-template-columns:1.1fr .9fr;gap:18px}} .panel{{background:linear-gradient(180deg,rgba(18,36,53,.97),rgba(14,27,38,.97));border:1px solid #1d3b52;border-radius:18px;padding:16px;box-shadow:0 20px 60px rgba(0,0,0,.25)}} .status-grid,.metric-grid{{display:grid;grid-template-columns:repeat(5,1fr);gap:10px;margin-top:14px}} .status,.metric{{background:rgba(0,0,0,.18);border:1px solid #1d3b52;border-radius:14px;padding:11px}} .metric b{{font-size:24px}} .badge{{display:inline-block;padding:4px 8px;border-radius:999px;background:#334155;color:#e2e8f0;font-size:12px;margin:2px}} .badge.good{{background:rgba(34,197,94,.18);color:#86efac;border:1px solid rgba(34,197,94,.35)}} .badge.bad{{background:rgba(239,68,68,.18);color:#fca5a5;border:1px solid rgba(239,68,68,.35)}} .row{{display:flex;gap:10px;align-items:center;flex-wrap:wrap}} .between{{justify-content:space-between}} .latest-img{{width:100%;max-height:430px;object-fit:contain;background:#020617;border-radius:14px;border:1px solid #1d3b52}} .inspection{{padding:12px;border:1px solid #1d3b52;border-radius:14px;margin-bottom:10px;background:rgba(0,0,0,.16)}} .links{{display:flex;gap:12px;margin-top:8px;font-size:13px;flex-wrap:wrap}} .report{{white-space:pre-wrap;max-height:520px;overflow:auto;background:#020617;border:1px solid #1d3b52;color:#dbeafe;border-radius:14px;padding:14px;font-size:13px;line-height:1.45}} table{{width:100%;border-collapse:collapse}} td,th{{padding:8px;border-bottom:1px solid #1d3b52;text-align:left;vertical-align:top}} button{{background:var(--accent);color:#06202a;border:0;border-radius:12px;padding:9px 12px;font-weight:700;cursor:pointer}} button.secondary{{background:#1f3b50;color:#dff7ff;border:1px solid #2c536b}} button.danger{{background:rgba(239,68,68,.85);color:white}} input{{background:#020617;color:var(--text);border:1px solid #1d3b52;border-radius:10px;padding:10px;min-width:250px}} .nav{{display:flex;gap:16px;margin-top:8px;flex-wrap:wrap}} .notice{{margin:12px 0;padding:12px;border-radius:12px;background:rgba(245,158,11,.14);border:1px solid rgba(245,158,11,.35);color:#fde68a}} .empty{{padding:24px;text-align:center;color:var(--muted);border:1px dashed #1d3b52;border-radius:14px}} form.inline{{display:inline}} .mini{{margin-top:8px}} .actions{{min-width:260px}} @media(max-width:950px){{.grid,.status-grid,.metric-grid{{grid-template-columns:1fr}}header{{position:static}}}}
</style></head><body>
<header><h1>ProofSight Dashboard v2</h1><div class=sub>Telegram-operated local trusted HSE inspection agent · Pi camera/Ollama vision · LM Studio reasoning · evidence → memory → report → action plan → review</div><div class=nav><a href="/">Refresh now</a><a href="/reports">Reporting dashboard</a><a href="/reports.csv">CSV export</a><a href="/api/status">Status API</a></div><div class=status-grid><div class=status><div class=muted>Agent</div>{badge(proof_state, proof_state=='active')}</div><div class=status><div class=muted>Dashboard</div>{badge(dash_state, dash_state=='active')}</div><div class=status><div class=muted>Ollama</div>{badge(ollama_state, ollama_state=='active')}</div><div class=status><div class=muted>Camera</div>{badge(cam['state'], cam['good'])}<br><span class=muted>{html.escape(str(cam.get('reason')))} · brightness {html.escape(str(cam.get('brightness')))}</span></div><div class=status><div class=muted>Model mode</div>{badge(cfg.get('models',{}).get('scenario','unknown'), True)}</div></div></header>
<div class=wrap>{f'<div class=notice>{html.escape(message)}</div>' if message else ''}
<section class=panel><h2>Risk / operations summary</h2><div class=metric-grid><div class=metric><div class=muted>Valid images</div><b>{stats['valid_images']}</b></div><div class=metric><div class=muted>Rejected images</div><b>{stats['rejected_images']}</b></div><div class=metric><div class=muted>Needs review</div><b>{stats['review_needed']}</b></div><div class=metric><div class=muted>Open actions</div><b>{stats['open_actions']}</b></div><div class=metric><div class=muted>High risk actions</div><b>{stats['high_actions']}</b></div></div></section>
<div class=grid style="margin-top:18px"><section class=panel><h2>Latest evidence + controls</h2>{latest_img_html}<form method=post action=/run class=row style="margin-top:14px"><input name=location placeholder=Location value="Dashboard quick scan"><button type=submit>Run quick scan</button><button class=secondary name=force value=1 type=submit>Force analyse</button><a href=/api/status>API status</a></form><p class=muted>Trusted evidence gate rejects dark/blank frames instead of inventing hazards.</p></section><section class=panel><h2>Latest report</h2>{render_report(latest_report_text)}</section></div>
<div class=grid style="margin-top:18px"><section class=panel><h2>Action plan board</h2>{'<table><thead><tr><th>Action</th><th>Risk</th><th>Owner/deadline</th><th>Status</th><th>Controls</th></tr></thead><tbody>'+''.join(action_rows)+'</tbody></table>' if action_rows else '<div class=empty>No action items yet. When ProofSight finds a hazard, it will appear here.</div>'}</section><section class=panel><h2>Recent inspections + human review <span class=muted>(latest 12)</span></h2>{''.join(inspection_cards) if inspection_cards else '<div class=empty>No inspections yet.</div>'}<p class=muted><a href="/reports">Open full reporting dashboard</a> for complete history.</p></section></div>
<section class=panel style="margin-top:18px"><h2>Memory / Cognee-style recall</h2><div class=metric-grid><div class=metric><div class=muted>Remembered inspections</div><b>{memory.get('remembered_inspections',0)}</b></div><div class=metric><div class=muted>Cognee queue records</div><b>{memory.get('cognee_queue_records',0)}</b></div><div class=metric><div class=muted>Overmind trace records</div><b>{memory.get('overmind_trace_records',0)}</b></div><div class=metric><div class=muted>Memory context hits</div><b>{memory.get('memory_context_hits',0)}</b></div><div class=metric><div class=muted>Open recurring actions</div><b>{memory.get('open_recurring_actions',0)}</b></div></div><table style="margin-top:12px"><thead><tr><th>Hazard category</th><th>Count</th></tr></thead><tbody>{memory_rows}</tbody></table></section>
<section class=panel style="margin-top:18px"><h2>Partner / sponsor adapters</h2><table><thead><tr><th>Partner</th><th>Mode</th><th>Notes</th></tr></thead><tbody>{partner_rows}</tbody></table></section>
</div></body></html>"""


def render_index(message: str = "") -> str:
    """Ultra-light mobile dashboard for Telegram/in-app browsers.

    The full control surface remains at /full. Root must stay tiny and avoid
    embedded images/forms so it opens reliably on phones and slow links.
    """
    cfg = load_config()
    inspections = latest_inspections(5)
    latest = inspections[0] if inspections else None
    stats = dashboard_stats(latest_inspections(100), actions(100))
    cam = camera_health(latest)
    proof_state = service_state("proofsight.service")
    dash_state = service_state("proofsight-dashboard.service")
    ollama_state = service_state("ollama.service")
    latest_bits = "<p>No inspections yet.</p>"
    if latest:
        image_name = Path(latest.get("evidence_path") or "").name
        report_name = Path(latest.get("report_path") or "").name
        trace_name = Path(latest.get("trace_path") or "").name
        latest_bits = f"""
        <div class=card>
          <h2>Latest inspection</h2>
          <p><b>{html.escape(latest.get('id') or '')}</b></p>
          <p>{badge(latest.get('status','unknown'), latest.get('status') != 'image_rejected')} · {html.escape(latest.get('created_at') or '')}</p>
          <p>{html.escape(latest.get('location') or '')}</p>
          <p><a href="/evidence/{html.escape(image_name)}">open evidence image</a> · <a href="/report/{html.escape(report_name)}">open report</a> · <a href="/trace/{html.escape(trace_name)}">trace</a></p>
        </div>
        """
    recent = "".join(
        f"<li>{html.escape(i.get('created_at') or '')} — {badge(i.get('status','unknown'), i.get('status') != 'image_rejected')} — {html.escape(i.get('location') or '')}</li>"
        for i in inspections
    )
    return f"""<!doctype html><html lang=en><head><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1"><title>ProofSight Mobile Dashboard</title>
<style>
body{{margin:0;font-family:system-ui,-apple-system,Segoe UI,sans-serif;background:#071018;color:#e9f4ff}} a{{color:#67e8f9}} header,.wrap{{max-width:900px;margin:auto;padding:16px}} header{{border-bottom:1px solid #1d3b52}} h1{{margin:0 0 6px;font-size:26px}} .muted{{color:#8aa7bb}} .grid{{display:grid;grid-template-columns:repeat(2,1fr);gap:10px}} .card{{background:#0e1b26;border:1px solid #1d3b52;border-radius:14px;padding:14px;margin:12px 0}} .metric b{{font-size:24px}} .badge{{display:inline-block;padding:3px 7px;border-radius:999px;background:#334155;color:#e2e8f0;font-size:12px}} .badge.good{{background:rgba(34,197,94,.18);color:#86efac;border:1px solid rgba(34,197,94,.35)}} .badge.bad{{background:rgba(239,68,68,.18);color:#fca5a5;border:1px solid rgba(239,68,68,.35)}} li{{margin:8px 0}} @media(max-width:650px){{.grid{{grid-template-columns:1fr}}}}
</style></head><body><header><h1>ProofSight Mobile Dashboard</h1><p class=muted>Lightweight status page. Full controls are separate so this page opens reliably on phones.</p><p><a href="/">Refresh</a> · <a href="/full">Full dashboard</a> · <a href="/reports">Reports</a> · <a href="/api/status">API</a></p></header><main class=wrap>{f'<div class=card>{html.escape(message)}</div>' if message else ''}
<section class=grid><div class=card><div class=muted>Agent</div>{badge(proof_state, proof_state=='active')}</div><div class=card><div class=muted>Dashboard</div>{badge(dash_state, dash_state=='active')}</div><div class=card><div class=muted>Ollama</div>{badge(ollama_state, ollama_state=='active')}</div><div class=card><div class=muted>Camera</div>{badge(cam['state'], cam['good'])}<br><span class=muted>{html.escape(str(cam.get('reason')))} · brightness {html.escape(str(cam.get('brightness')))}</span></div></section>
<section class=grid><div class=card metric><div class=muted>Valid images</div><b>{stats['valid_images']}</b></div><div class=card metric><div class=muted>Rejected images</div><b>{stats['rejected_images']}</b></div><div class=card metric><div class=muted>Needs review</div><b>{stats['review_needed']}</b></div><div class=card metric><div class=muted>Open actions</div><b>{stats['open_actions']}</b></div></section>
{latest_bits}
<div class=card><h2>Recent inspections</h2><ul>{recent}</ul></div>
<div class=card><h2>Controls</h2><p><a href="/full">Open full dashboard for run/review buttons</a></p><p class=muted>The mobile page intentionally avoids embedded images and many forms.</p></div>
</main></body></html>"""


class Handler(BaseHTTPRequestHandler):
    server_version = "ProofSightDashboard/2.0"

    def log_message(self, fmt: str, *args) -> None:
        print(f"{self.address_string()} - {fmt % args}", flush=True)

    def send_bytes(self, body: bytes, content_type: str, status: int = 200, extra_headers: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def send_text(self, text: str, content_type: str = "text/html; charset=utf-8", status: int = 200) -> None:
        self.send_bytes(text.encode("utf-8"), content_type, status)

    def post_body(self) -> dict[str, list[str]]:
        length = int(self.headers.get("Content-Length", "0") or "0")
        body = self.rfile.read(length).decode("utf-8", errors="replace")
        return parse_qs(body)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        cfg = load_config()
        if path == "/":
            self.send_text(render_index())
            return
        if path == "/full":
            self.send_text(render_full_index())
            return
        if path == "/healthz":
            self.send_text("ok\n", "text/plain; charset=utf-8")
            return
        if path == "/reports":
            self.send_text(render_reports_dashboard())
            return
        if path == "/api/reports":
            self.send_text(json.dumps(reporting_dataset(1000), indent=2, ensure_ascii=False), "application/json; charset=utf-8")
            return
        if path == "/reports.csv":
            self.send_bytes(reports_csv(), "text/csv; charset=utf-8", extra_headers={"Content-Disposition": 'attachment; filename="proofsight-inspections.csv"'})
            return
        if path == "/api/status":
            payload = {
                "proofsight_service": service_state("proofsight.service"),
                "dashboard_service": service_state("proofsight-dashboard.service"),
                "ollama_service": service_state("ollama.service"),
                "camera_health": camera_health(latest_inspections(1)[0] if latest_inspections(1) else None),
                "stats": dashboard_stats(latest_inspections(100), actions(200)),
                "actions": actions(50),
                "partners": partner_status(cfg),
                "memory": memory_summary(latest_inspections(100), actions(200)),
                "latest": latest_inspections(10),
            }
            self.send_text(json.dumps(payload, indent=2, ensure_ascii=False), "application/json; charset=utf-8")
            return
        if path.startswith("/evidence/"):
            f = safe_relative_file(Path(cfg["actions"]["evidence_dir"]), unquote(path.removeprefix("/evidence/")))
            self.send_bytes(f.read_bytes(), "image/jpeg") if f else self.send_text("not found", "text/plain", 404)
            return
        if path.startswith("/report/"):
            f = safe_relative_file(Path(cfg["actions"]["reports_dir"]), unquote(path.removeprefix("/report/")))
            self.send_text(render_report(read_text(f))) if f else self.send_text("not found", "text/plain", 404)
            return
        if path.startswith("/trace/"):
            f = safe_relative_file(Path(cfg["actions"]["trace_dir"]), unquote(path.removeprefix("/trace/")))
            self.send_text("<pre>" + html.escape(read_text(f)) + "</pre>") if f else self.send_text("not found", "text/plain", 404)
            return
        if path.startswith("/export/"):
            inspection_id = unquote(path.removeprefix("/export/"))
            try:
                z = create_audit_zip(inspection_id)
                self.send_bytes(z.read_bytes(), "application/zip", extra_headers={"Content-Disposition": f'attachment; filename="{z.name}"'})
            except Exception as exc:
                self.send_text(f"export failed: {html.escape(str(exc))}", "text/plain", 500)
            return
        self.send_text("not found", "text/plain", HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        fields = self.post_body()
        try:
            if path == "/run":
                location = fields.get("location", ["Dashboard quick scan"])[0]
                force = fields.get("force", [""])[0]
                cmd = [sys.executable, str(ROOT / "proofsight.py"), "inspect", "--location", location]
                if force:
                    cmd.append("--force")
                start = time.time()
                rc, out = run(cmd, timeout=420)
                self.send_text(render_index(f"Inspection exited {rc} in {time.time()-start:.1f}s. {out[:700]}"))
                return
            if path == "/review":
                set_review(fields["inspection_id"][0], fields["status"][0], fields.get("note", [""])[0])
                self.send_text(render_index(f"Review updated: {fields['inspection_id'][0]} → {fields['status'][0]}"))
                return
            if path == "/action-status":
                set_action_status(fields["action_id"][0], fields["status"][0])
                self.send_text(render_index(f"Action updated: {fields['action_id'][0]} → {fields['status'][0]}"))
                return
        except Exception as exc:
            self.send_text(render_index(f"Error: {exc}"), status=500)
            return
        self.send_text("not found", "text/plain", 404)


def main() -> int:
    ap = argparse.ArgumentParser(description="ProofSight local dashboard")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8787)
    args = ap.parse_args()
    # Ensure schema before serving.
    if db_path().exists():
        with db_connect():
            pass
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"ProofSight dashboard v2 listening on http://{args.host}:{args.port}", flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
