#!/usr/bin/env python3
"""Capture and sanity-test the Logitech Brio webcam for ProofSight.

No external packages required except ffmpeg being installed. Uses Pillow if present
for better image statistics; falls back to file-size checks otherwise.
"""
from __future__ import annotations

import argparse
import base64
import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

DEFAULT_DEVICE = "/dev/video0"
DEFAULT_OUT = "/home/dave/hse-pi-agent/evidence/webcam_latest.jpg"


def run(cmd: list[str], timeout: int = 90) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, text=True, capture_output=True, timeout=timeout)


def configure(device: str) -> None:
    # UK mains anti-flicker, autofocus, auto exposure. Ignore unsupported controls.
    controls = [
        "power_line_frequency=1",  # 50 Hz
        "focus_automatic_continuous=1",
        "auto_exposure=3",
        "white_balance_automatic=1",
    ]
    for ctrl in controls:
        run(["v4l2-ctl", "-d", device, f"--set-ctrl={ctrl}"], timeout=10)


def capture(device: str, out: Path, warm_frames: int = 20) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "warning",
        "-f",
        "v4l2",
        "-input_format",
        "mjpeg",
        "-video_size",
        "1280x720",
        "-framerate",
        "10",
        "-i",
        device,
        "-vf",
        f"select='gte(n,{warm_frames})'",
        "-frames:v",
        "1",
        "-q:v",
        "2",
        str(out),
        "-y",
    ]
    p = run(cmd, timeout=90)
    if p.returncode != 0:
        raise RuntimeError(f"ffmpeg capture failed\nSTDOUT:\n{p.stdout}\nSTDERR:\n{p.stderr}")


def image_stats(path: Path) -> dict:
    result = {"path": str(path), "exists": path.exists(), "size_bytes": path.stat().st_size if path.exists() else 0}
    if not path.exists():
        return result
    try:
        from PIL import Image, ImageStat  # type: ignore

        im = Image.open(path).convert("RGB")
        stat = ImageStat.Stat(im)
        result.update(
            {
                "resolution": list(im.size),
                "mean_rgb": [round(x, 2) for x in stat.mean],
                "extrema": stat.extrema,
                "blank_or_too_dark": max(stat.mean) < 30,
            }
        )
    except Exception as exc:
        result.update({"image_stats_error": str(exc), "blank_or_too_dark": result["size_bytes"] < 25000})
    return result


def ask_moondream(path: Path) -> dict:
    with path.open("rb") as f:
        b64 = base64.b64encode(f.read()).decode()
    payload = {
        "model": "moondream",
        "prompt": "Describe this webcam image for a camera installation test. If it is blank, dark, covered, or unusable, say so clearly.",
        "images": [b64],
        "stream": False,
        "options": {"temperature": 0, "num_predict": 100},
    }
    start = time.time()
    req = urllib.request.Request(
        "http://127.0.0.1:11434/api/generate",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=180) as r:
        data = json.loads(r.read().decode())
    return {"elapsed_s": round(time.time() - start, 2), "response": data.get("response", "").strip()}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--device", default=DEFAULT_DEVICE)
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--skip-vision", action="store_true")
    args = ap.parse_args()

    out = Path(args.out)
    configure(args.device)
    capture(args.device, out)
    stats = image_stats(out)
    report = {"camera_device": args.device, "capture": stats}
    if not args.skip_vision and stats.get("exists"):
        try:
            report["moondream"] = ask_moondream(out)
        except Exception as exc:
            report["moondream_error"] = str(exc)
    print(json.dumps(report, indent=2))
    return 1 if stats.get("blank_or_too_dark") else 0


if __name__ == "__main__":
    raise SystemExit(main())
