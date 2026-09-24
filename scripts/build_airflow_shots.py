#!/usr/bin/env python3
"""Capture the demo video's two stills of Airflow itself, from a running stack.

The video says Airflow does the work, so two of its frames are Airflow's own
screens rather than this project's dashboard: the replay DAG's runs, one per
month of the backfill, and a reviewer's form on a waiting human-in-the-loop
task. Neither exists until a stack has replayed history and queued a review,
so this is a separate step from :mod:`build_shots`, which needs nothing running:

    make up && make demo      # replay two years; the review queue follows
    python scripts/build_airflow_shots.py

The review is captured with a reason typed into the form and **not submitted**,
so running this never records a ruling. Needs Playwright (see
requirements-video.txt) and a Chrome it can drive.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.parse
import urllib.request
from pathlib import Path

import storyboard

BASE = os.environ.get("PTM_AIRFLOW_URL", "http://localhost:8080").rstrip("/")
OUT = Path("video_assets/shots")
DOMAIN = "expenses"

#: The reason typed into the captured form. Shown, never sent - and worded to
#: fit whichever case the queue puts first, because which one that is depends on
#: what has already been ruled on.
EXAMPLE_NOTE = "The policy as written gives the proposed outcome for this case."


def api(path: str) -> dict:
    with urllib.request.urlopen(f"{BASE}/api/v2/{path}", timeout=15) as response:
        return json.load(response)


def backfill_runs() -> int:
    runs = api(f"dags/replay_{DOMAIN}/dagRuns?limit=100")["dag_runs"]
    return sum(1 for r in runs if r["run_type"] == "backfill" and r["state"] == "success")


def waiting_review() -> tuple[str, int] | None:
    """The newest adjudicate run with a review still waiting, and one of its tasks."""
    runs = api(f"dags/adjudicate_{DOMAIN}/dagRuns?limit=20&order_by=-start_date")["dag_runs"]
    for run in runs:
        run_id = urllib.parse.quote(run["dag_run_id"], safe="")
        tasks = api(f"dags/adjudicate_{DOMAIN}/dagRuns/{run_id}/taskInstances?limit=100")
        waiting = sorted(t["map_index"] for t in tasks["task_instances"]
                         if t["task_id"] == "review" and t["state"] == "deferred")
        if waiting:
            return run["dag_run_id"], waiting[0]
    return None


def main() -> int:
    try:
        months = backfill_runs()
        review = waiting_review()
    except OSError as exc:
        raise SystemExit(f"no Airflow answering at {BASE} ({exc}); "
                         f"start it with make up") from None
    if not months:
        raise SystemExit(f"replay_{DOMAIN} has no successful backfill runs to show; "
                         f"run make demo first")
    if review is None:
        raise SystemExit(f"adjudicate_{DOMAIN} has no review waiting, so there is no form "
                         f"to capture. Trigger it: airflow dags trigger adjudicate_{DOMAIN}")

    from playwright.sync_api import sync_playwright

    run_id, index = review
    pages = {
        "af_runs": (f"{BASE}/dags/replay_{DOMAIN}/runs", None),
        "af_review": (f"{BASE}/dags/adjudicate_{DOMAIN}/runs/{run_id}/tasks/review/"
                      f"mapped/{index}/required_actions", EXAMPLE_NOTE),
    }
    shots = [s for s in storyboard.captures() if storyboard.is_airflow(s)]
    unknown = {s["sc"] for s in shots} - set(pages)
    if unknown:
        raise SystemExit(f"no capture defined for {sorted(unknown)}")

    OUT.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(channel=os.environ.get("PTM_PLAYWRIGHT_CHANNEL", "chrome"))
        page = browser.new_context(viewport={"width": 1920, "height": 1080},
                                   color_scheme="dark", device_scale_factor=2).new_page()
        for shot in shots:
            url, note = pages[shot["sc"]]
            page.goto(url)
            page.wait_for_load_state("networkidle")
            page.wait_for_timeout(1500)
            if note:
                page.locator("#element_note").fill(note)
            path = OUT / f"{shot['id']}.png"
            page.screenshot(path=str(path))
            print(f"{shot['id']}: {shot['title']} -> {path}")
        browser.close()
    print(f"({months} backfill runs; review task {index} of run {run_id})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
