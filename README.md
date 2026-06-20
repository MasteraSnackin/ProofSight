# ProofSight

Local-first AI health and safety inspection agent for Raspberry Pi 5.

## Description

ProofSight is a Raspberry Pi 5 inspection appliance that captures workplace evidence from a webcam, checks whether the image is usable, analyses visible health and safety risks, and produces inspection reports, traces, action items and dashboard views.

The current deployment keeps camera capture, image validation, evidence storage, reports, traces, dashboard and Pi-local `moondream` vision on the Raspberry Pi. HSE reasoning and report decisions are configured to use LM Studio on a MacBook over Tailscale. If LM Studio is unreachable, ProofSight records `model_error` rather than inventing findings.

ProofSight is an inspection assistant, not a replacement for a competent human inspector. Reports are draft outputs and should be reviewed before formal compliance, legal or enforcement use.

## Table of Contents

- [Features](#features)
- [Tech Stack](#tech-stack)
- [Architecture Overview](#architecture-overview)
- [Installation](#installation)
- [Usage](#usage)
- [Configuration](#configuration)
- [Screenshots or Demo](#screenshots-or-demo)
- [API and CLI Reference](#api-and-cli-reference)
- [Tests](#tests)
- [Roadmap](#roadmap)
- [Contributing](#contributing)
- [Licence](#licence)
- [Contact or Support](#contact-or-support)

## Features

- Webcam evidence capture through V4L2 and `ffmpeg`.
- Local image trust gate that rejects dark, blank, obstructed or suspiciously small evidence.
- Pi-local vision description through Ollama `moondream`.
- HSE reasoning and action-plan generation through LM Studio over Tailscale.
- Markdown inspection reports with validation, summary, findings and action-plan sections.
- SQLite inspection memory with inspection, action item and review tables.
- JSON trace file for each inspection.
- Partner-aligned artifact streams:
  - Captur-style local evidence validation.
  - Cognee-style JSONL memory ingest queue.
  - Overmind-style JSONL trace stream.
- Operations dashboard on port `8787`.
- Reporting dashboard with inspection counts, evidence quality, review state and CSV export.
- Audit-pack ZIP export containing evidence, report, trace and manifest.
- Human review controls for approving, rejecting or requesting a retake.
- Systemd user services for the inspection monitor and dashboard.
- Static public landing page configured for Vercel deployment.

## Tech Stack

| Layer | Technology |
|---|---|
| Runtime | Python 3.13 |
| Hardware target | Raspberry Pi 5 |
| Camera | Logitech Brio / V4L2 device at `/dev/video0` |
| Capture tools | `ffmpeg`, `v4l2-ctl` |
| Image validation | Pillow |
| Local vision server | Ollama at `http://127.0.0.1:11434` |
| Vision model | `moondream` |
| Reasoning server | LM Studio at `http://100.106.72.5:1234/v1` over Tailscale |
| Reasoning/report model | `local-model` placeholder until LM Studio exposes the exact model ID |
| Storage | SQLite and local files |
| Dashboard | Python standard library `ThreadingHTTPServer` |
| Service manager | systemd user services |
| Public landing page | Static HTML in `public/index.html`, Vercel config in `vercel.json` |
| Node tooling | Node.js 22.22.3, npm 10.9.8 for the static Vercel build script |

## Architecture Overview

```mermaid
flowchart LR
  Operator[Operator] --> Dashboard[ProofSight Dashboard :8787]
  Dashboard --> Agent[ProofSight CLI / Monitor]
  Agent --> Camera[Webcam /dev/video0]
  Agent --> Gate[Local Image Trust Gate]
  Gate --> Vision[Ollama moondream on Pi]
  Vision --> Reasoning[LM Studio on MacBook]
  Reasoning --> Report[Markdown Report]
  Agent --> Store[(SQLite DB)]
  Agent --> Files[Evidence, Reports, Traces]
  Files --> Dashboard
  Store --> Dashboard
  Public[Public Viewer] --> Vercel[Vercel Static Landing Page]
  Vercel --> Repo[GitHub Repository]
```

The inspection appliance runs on the Raspberry Pi. It captures evidence, validates the image locally, sends only usable evidence into the model pipeline, stores reports and traces on disk, and exposes local dashboard views. The Vercel site is only a public project page; it does not expose the Pi camera, SQLite database or local dashboard controls.

See [`ARCHITECTURE.md`](ARCHITECTURE.md) for the full system architecture, data flow, data model and trade-offs.

## Installation

This project is currently deployed in:

```bash
/home/dave/hse-pi-agent
```

Required system tools:

```bash
python3
ffmpeg
v4l2-ctl
systemctl --user
ollama
```

Required Python packages used by the application:

```text
PyYAML
Pillow
```

There is currently no committed `requirements.txt` or `pyproject.toml`. If recreating the project on a fresh machine, install Python dependencies in a virtual environment or through the system package manager used by the target device.

Clone and verify the repository:

```bash
git clone git@github.com:MasteraSnackin/ProofSight.git
cd ProofSight
python3 -m py_compile proofsight.py vasper_qa.py dashboard.py partners.py camera_test.py
```

For the existing Pi deployment:

```bash
cd /home/dave/hse-pi-agent
python3 -m py_compile proofsight.py vasper_qa.py dashboard.py partners.py camera_test.py
```

Ensure Ollama is running and the required local vision model is available:

```bash
systemctl --user status ollama.service
ollama list
```

Expected Pi-local model:

```text
moondream
```

The reasoning/report model is served by LM Studio on the MacBook. Replace `local-model` in `config.yaml` with the exact model ID returned by `http://100.106.72.5:1234/v1/models` once LM Studio is reachable.

## Usage

Run one inspection:

```bash
cd /home/dave/hse-pi-agent
./proofsight.py inspect --location "Warehouse aisle 1"
```

Use an existing image instead of capturing from the webcam:

```bash
./proofsight.py inspect \
  --image /home/dave/hse-pi-agent/evidence/example.jpg \
  --location "Existing evidence validation image"
```

Force analysis even when the trust gate rejects the image:

```bash
./proofsight.py inspect --location "Test area" --force
```

Show partner and sponsor adapter status:

```bash
./proofsight.py partners
```

Run repeated inspections:

```bash
./proofsight.py monitor --interval 300 --location "ProofSight webcam zone"
```

Start or inspect the systemd service:

```bash
systemctl --user daemon-reload
systemctl --user enable --now proofsight.service
systemctl --user status proofsight.service
journalctl --user -u proofsight.service -f
```

Start or inspect the dashboard service:

```bash
systemctl --user enable --now proofsight-dashboard.service
systemctl --user status proofsight-dashboard.service
journalctl --user -u proofsight-dashboard.service -f
```

Build the static public landing page:

```bash
npm run build
```

Deploy the public landing page to Vercel after authenticating the Vercel CLI:

```bash
npx vercel@54.14.2 login
npx vercel@54.14.2 deploy --prod --yes
```

## Configuration

Main configuration file:

```text
/home/dave/hse-pi-agent/config.yaml
```

Current model configuration:

```yaml
models:
  scenario: B_pi_camera_macbook_lmstudio
  provider: lmstudio
  ollama_base_url: http://127.0.0.1:11434
  vision: moondream
  lmstudio_base_url: http://100.106.72.5:1234/v1
  reasoning: local-model
  report: local-model
```

Camera configuration:

```yaml
camera:
  device: /dev/video0
  width: 1280
  height: 720
  framerate: 10
  warm_frames: 20
  power_line_frequency: 1
```

Validation configuration:

```yaml
validation:
  min_mean_brightness: 30
  min_file_size_bytes: 25000
  reject_blank_or_dark: true
```

File storage paths:

```yaml
actions:
  evidence_dir: /home/dave/hse-pi-agent/evidence
  reports_dir: /home/dave/hse-pi-agent/reports
  db_path: /home/dave/hse-pi-agent/data/proofsight.db
  trace_dir: /home/dave/hse-pi-agent/traces
```

Optional environment variables recognised by the partner adapter layer:

| Variable | Purpose | Required |
|---|---|---|
| `PROOFSIGHT_CAPTUR_COMMAND` | Optional external Captur SDK/CLI wrapper command | No |
| `PROOFSIGHT_OVERMIND_ENDPOINT` | Optional endpoint for exporting traces | No |
| `PROOFSIGHT_EXO_BASE_URL` | Optional Exo Labs or distributed inference endpoint | No |

No API keys are required for the current local Ollama vision step. LM Studio normally accepts a dummy local bearer token, but it must be reachable from the Pi over Tailscale or ProofSight will record `model_error` for reasoning.

## Screenshots or Demo

Local dashboard URLs in the current Pi deployment:

```text
http://127.0.0.1:8787
http://<PI_TAILSCALE_IP>:8787
http://<PI_LAN_IP>:8787
```

Reporting dashboard:

```text
http://<PI_TAILSCALE_IP>:8787/reports
```

Health and API endpoints:

```text
http://127.0.0.1:8787/healthz
http://127.0.0.1:8787/api/status
http://127.0.0.1:8787/api/reports
http://127.0.0.1:8787/reports.csv
```

Public landing page:

```text
<ADD VERCEL URL>
```

Screenshots are not currently committed. Add dashboard screenshots to a future `docs/` or `assets/` directory if this project is prepared for public submission.

## API and CLI Reference

### CLI

```bash
./proofsight.py inspect --location "Warehouse aisle 1"
./proofsight.py inspect --image /path/to/image.jpg --location "Existing evidence"
./proofsight.py inspect --location "Test" --force
./proofsight.py monitor --interval 300 --location "Webcam area"
./proofsight.py partners
```

Compatibility command:

```bash
./vasper_qa.py inspect --location "Compatibility test"
```

### Dashboard routes

| Route | Purpose |
|---|---|
| `/` | Operations dashboard |
| `/reports` | Reporting dashboard |
| `/healthz` | Plain health check |
| `/api/status` | JSON status, camera health, latest inspections and partner status |
| `/api/reports` | JSON reporting dataset |
| `/reports.csv` | CSV export of inspections |
| `/evidence/<file>` | Evidence image access |
| `/report/<file>` | Markdown report viewer |
| `/trace/<file>` | JSON trace viewer |
| `/export/<inspection_id>` | Audit-pack ZIP export |

### Output files

| Directory | Contents |
|---|---|
| `evidence/` | Captured JPEG evidence |
| `reports/` | Markdown inspection reports |
| `traces/` | Per-inspection JSON traces and partner JSONL streams |
| `data/` | SQLite database |
| `exports/` | Audit-pack ZIP files |

## Tests

There is currently no formal test suite. Use the following smoke tests to verify the deployed system.

Compile Python files:

```bash
cd /home/dave/hse-pi-agent
python3 -m py_compile dashboard.py proofsight.py vasper_qa.py partners.py camera_test.py
```

Run the static landing page build check:

```bash
npm run build
```

Check services:

```bash
systemctl --user is-active proofsight.service
systemctl --user is-active proofsight-dashboard.service
systemctl --user is-active ollama.service
```

Check dashboard health:

```bash
curl http://127.0.0.1:8787/healthz
curl http://127.0.0.1:8787/api/status
curl http://127.0.0.1:8787/api/reports
```

Run a controlled inspection:

```bash
cd /home/dave/hse-pi-agent
./proofsight.py inspect --location "Smoke test"
```

A rejected dark image is a valid trust-gate result, not a software crash. The typical status for a dark or obstructed frame is:

```text
image_rejected
image_too_dark_or_obstructed
```

## Roadmap

- Add a committed dependency file, for example `requirements.txt` or `pyproject.toml`.
- Add automated unit tests for image validation, JSON parsing, report writing and dashboard routes.
- Add authentication for the dashboard before exposing it beyond trusted LAN or Tailscale networks.
- Replace the temporary `local-model` LM Studio model ID with the exact model ID once `/v1/models` is reachable.
- Complete Vercel authentication and replace `<ADD VERCEL URL>` with the live public landing page URL.
- Add a real official Cognee ingestion worker if Cognee is installed and configured.
- Add official Captur, Overmind or Exo integrations when tested SDKs or endpoints are available.
- Add dashboard screenshots and demo media.
- Improve camera diagnostics for privacy shutter, exposure and lighting issues.

## Contributing

This project is currently a local prototype rather than a mature public open-source project. If it is published for wider contribution, useful areas include:

- image validation tests
- dashboard UX improvements
- LM Studio provider adapter hardening
- official sponsor integrations
- documentation and deployment hardening
- dashboard authentication and access control

## Licence

<ADD LICENSE>

No licence file was found in the inspected project directory.

## Contact or Support

Maintainer: Dave Cheng

Contact: <ADD PUBLIC CONTACT>
