#!/usr/bin/env python3
"""Render every still the demo video is cut from, with a headless browser.

Each shot is one frame: the dashboard driven into a particular state, or one of
the three standalone templates beside this file. The states are the JavaScript
injected by the capture server below, selected by the ``sc`` directive on each
shot in :mod:`storyboard` - which is also where the durations, the titles and
the narration live, in one table the other two scripts read as well.

**Platform.** The browser and the interpreter are both discovered rather than
assumed. This script used to name ``/Applications/Google Chrome.app`` and
``.venv/bin/python`` outright, which made it macOS-only in a repository whose
Makefile goes out of its way to work from Git Bash on Windows - and it failed
with a bare FileNotFoundError that named neither problem. :mod:`generate_audio`
is genuinely macOS-only, because ``say`` is; this is not, and should not have
read as though it were.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import storyboard
from storyboard import SHOTS, captures

#: Where the capture server listens. Local only - it serves the demo database.
PORT = 8089
HOST = "127.0.0.1"

#: Device pixel ratio of every capture. The video zooms into each still, and a
#: 1080p still zoomed to twice its size is a blur of the small text the shot is
#: about; captured at 2x, the zoom lands on real pixels.
SCALE = 2

#: How long to wait for that server before giving up, in seconds. Polled rather
#: than slept through: a fixed `time.sleep(2)` is either slower than it needs to
#: be or shorter than the machine needs, and on the short side every shot fails
#: with a connection error that looks like a browser problem.
SERVER_TIMEOUT = 30

#: Chrome, in the order worth trying. `shutil.which` covers a PATH install and
#: every Linux package name; the absolute paths are where the macOS and Windows
#: installers put it, neither of which puts it on PATH.
CHROME_CANDIDATES = (
    "google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "chrome",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
)


def find_chrome() -> str:
    """The browser to render with, or a refusal that says how to fix it."""
    override = os.environ.get("PTM_CHROME")
    if override:
        if not Path(override).exists() and not shutil.which(override):
            raise SystemExit(f"PTM_CHROME points at {override!r}, which is not there")
        return override
    for candidate in CHROME_CANDIDATES:
        found = shutil.which(candidate) if not os.path.isabs(candidate) else (
            candidate if Path(candidate).exists() else None)
        if found:
            return found
    raise SystemExit(
        "no Chrome or Chromium found. These shots are rendered headless, so one is "
        "needed; install it, or set PTM_CHROME to the executable.")


def find_python() -> str:
    """The interpreter to run the capture server with.

    A project venv if there is one - and both spellings of its layout, because
    a venv puts the interpreter in Scripts/ on Windows and bin/ everywhere
    else, which is the same fork the Makefile documents at the top. Otherwise
    whatever is running this, which is the right answer when the dependencies
    are installed globally or the venv is already active.
    """
    root = Path(__file__).resolve().parent.parent
    for relative in (".venv/Scripts/python.exe", ".venv/bin/python",
                     ".venv-af/Scripts/python.exe", ".venv-af/bin/python"):
        candidate = root / relative
        if candidate.exists():
            return str(candidate)
    return sys.executable


def wait_for_server(url: str, process: subprocess.Popen, timeout: int) -> None:
    """Block until the capture server answers, or say why it never will."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise SystemExit(
                f"the capture server exited with status {process.returncode} before it "
                f"served anything. Run it by hand to see why: it needs fastapi, uvicorn "
                f"and a seeded include/ptm.db.")
        try:
            with urllib.request.urlopen(url, timeout=1):
                return
        except (urllib.error.URLError, ConnectionError, OSError):
            time.sleep(0.25)
    raise SystemExit(f"the capture server did not answer {url} within {timeout}s")


def main() -> int:
    problems = storyboard.check()
    if problems:
        # The storyboard is the contract these frames are cut to. Rendering
        # twenty-two stills against a table that does not add up wastes the
        # render and produces a video whose narration drifts, so it is checked
        # before the first shot rather than discovered at the mux.
        for line in problems:
            print(f"ERROR {line}", file=sys.stderr)
        return 1

    out_dir = Path("video_assets/shots")
    out_dir.mkdir(parents=True, exist_ok=True)
    scratch = Path("scratch")
    # Created rather than assumed: scratch/ is gitignored, so it does not exist
    # in a fresh clone and this failed on the write below with a FileNotFoundError
    # naming a path nobody had been told to make.
    scratch.mkdir(parents=True, exist_ok=True)

    total_dur = sum(s["dur"] for s in SHOTS)
    print(f"Total video duration from shots: {total_dur}s ({total_dur/60:.2f} mins)")

    server_py = """
import sys, types, os
sys.path.insert(0, os.path.abspath('.'))
m1 = types.ModuleType('airflow')
m2 = types.ModuleType('airflow.plugins_manager')
m2.AirflowPlugin = type('AirflowPlugin', (), {})
sys.modules['airflow'] = m1
sys.modules['airflow.plugins_manager'] = m2

os.environ['PTM_INCLUDE_DIR'] = './include'
os.environ['PTM_DB'] = './include/ptm.db'
os.environ['PTM_OFFLINE'] = '1'
os.environ['PTM_ALLOW_ANONYMOUS'] = '1'

import ptm.store as store
store.init_db()

from plugins.policy_time_machine_plugin import app as ptm_app
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from pathlib import Path
import uvicorn

root_app = FastAPI()

@root_app.get('/ptm_capture', response_class=HTMLResponse)
def capture_page(sc: str = ''):
    html = Path('plugins/dashboard.html').read_text(encoding='utf-8')
    injected_script = f'''
    <script>
    window.addEventListener('load', async () => {{
        await new Promise(r => setTimeout(r, 1600));
        const sc = "{sc}";

        const s = document.createElement('style');
        s.textContent = `
            body {{ font-size: 16px !important; }}
            header {{ padding: 14px 40px !important; }}
            main {{ max-width: 1480px !important; padding: 24px 40px !important; margin: 0 auto !important; }}
            .hero h1 {{ font-size: 34px !important; }}
            .prediction {{ padding: 26px !important; margin-bottom: 24px !important; }}
            .story-number {{ font-size: 58px !important; }}
            .tile .v {{ font-size: 28px !important; }}
            th, td {{ font-size: 15px !important; padding: 12px 14px !important; }}
            .highlight-box {{ outline: 4px solid #77a7ff !important; background: rgba(119,167,255,0.25) !important; }}
            .warn-box {{ outline: 4px solid #f9c75d !important; background: rgba(249,199,93,0.25) !important; }}
            dialog#reviewdialog {{
                background: #0f172a !important;
                color: #eef3ff !important;
                border: 2px solid #77a7ff !important;
                border-radius: 12px !important;
                box-shadow: 0 25px 60px rgba(0,0,0,0.85) !important;
                padding: 28px !important;
            }}
            dialog#reviewdialog h2, dialog#reviewdialog h3, dialog#reviewdialog h4 {{
                color: #77a7ff !important;
            }}
            dialog#reviewdialog pre {{
                background: #080c16 !important;
                border: 1px solid rgba(255,255,255,0.15) !important;
                border-radius: 8px !important;
                padding: 12px !important;
                color: #cbd5e1 !important;
            }}
            dialog#reviewdialog article {{
                background: rgba(30, 41, 59, 0.7) !important;
                border: 1px solid rgba(255,255,255,0.12) !important;
                border-radius: 8px !important;
                padding: 14px !important;
            }}
            dialog#reviewdialog button {{
                background: #1e293b !important;
                color: #e2e8f0 !important;
                border: 1px solid rgba(255,255,255,0.2) !important;
                padding: 6px 14px !important;
                border-radius: 6px !important;
            }}
        `;
        document.head.appendChild(s);

        function hideExcept(...selectors) {{
            const keep = new Set();
            selectors.forEach(sel => {{
                document.querySelectorAll(sel).forEach(el => keep.add(el));
            }});
            document.querySelectorAll('main > section, main > div, #detail > *').forEach(el => {{
                if (!keep.has(el)) el.style.display = 'none';
                else el.style.display = '';
            }});
        }}

        if (sc === 's1_1') {{
            // Only the question: the bet is asked over this frame, so the
            // answer card below the slider must not be on it.
            setView('plain');
            hideExcept('section.intro', '#prediction');
            const g = document.getElementById('guess');
            if (g) {{ g.value = 50; document.getElementById('guess-value').textContent = '50%'; }}
            window.scrollTo(0, 0);
        }} else if (sc === 's1_2') {{
            setView('plain');
            const g = document.getElementById('guess');
            if (g) {{ g.value = 20; document.getElementById('guess-value').textContent = '20%'; }}
            window.scrollTo(0, 0);
        }} else if (sc === 's1_3') {{
            setView('plain');
            const g = document.getElementById('guess');
            if (g) {{ g.value = 20; document.getElementById('guess-value').textContent = '20%'; document.getElementById('guess-reveal').click(); }}
            window.scrollTo(0, 0);
        }} else if (sc === 's1_4') {{
            setView('plain');
            const g = document.getElementById('guess');
            if (g) {{ g.value = 20; document.getElementById('guess-reveal').click(); }}
            window.scrollTo(0, 310);
        }} else if (sc === 's2_1') {{
            setView('detail');
            hideExcept('#detail', '#tiles', 'h2[data-t="h.clauses"]', '#clauses');
            window.scrollTo(0, 0);
        }} else if (sc === 's2_2') {{
            setView('detail');
            hideExcept('#detail', 'h2[data-t="h.clauses"]', '#clauses');
            const rows = document.querySelectorAll('#clauses tr');
            rows.forEach(r => {{
                if (r.textContent.includes('1.1') || r.textContent.includes('receipt')) {{
                    r.classList.add('highlight-box');
                }}
            }});
            window.scrollTo(0, 0);
        }} else if (sc === 's2_3') {{
            setView('detail');
            hideExcept('#detail', '#tiles');
            const devTile = Array.from(document.querySelectorAll('.tile')).find(el => el.textContent.includes('Deviations'));
            if (devTile) devTile.classList.add('warn-box');
            window.scrollTo(0, 0);
        }} else if (sc === 's2_4') {{
            setView('detail');
            hideExcept('#detail', 'h2[data-t="h.flips"]', '#flips');
            await new Promise(r => setTimeout(r, 600));
            let idx = SHOWN_FLIPS.findIndex(f => f.case_id === 'exp-0084');
            if (idx < 0) idx = 0;
            await openReview(idx);
            await new Promise(r => setTimeout(r, 1200));
        }} else if (sc === 's4_1') {{
            setView('plain');
            hideExcept('#architecture');
            window.scrollTo(0, 0);
        }} else if (sc === 's4_2') {{
            setView('plain');
            hideExcept('#architecture');
            document.querySelector('.machine-map').open = true;
            window.scrollTo(0, 0);
        }} else if (sc === 's5_1' || sc === 's5_3') {{
            setView('detail');
            hideExcept('#detail', 'h2[data-t="h.precedents"]', '#precedents');
            if (sc === 's5_3') {{
                const rows = document.querySelectorAll('#precedents tr');
                if (rows.length > 2) rows[1].classList.add('highlight-box');
            }}
            window.scrollTo(0, 0);
        }} else if (sc === 's5_2') {{
            setView('plain');
            hideExcept('#plain', '#gatecard', 'h2[data-t="p.gate"]');
            window.scrollTo(0, 0);
        }} else if (sc === 's6_1') {{
            // The dials, with nothing chosen yet. s6_2 is this with the
            // candidate thresholds typed in and s6_3 is the answer, so the
            // three frames are three states of one panel rather than - as
            // s6_2 and s6_3 were, sharing a branch - one picture shown twice
            // under two different sentences of narration.
            setView('detail');
            hideExcept('#detail', 'h2[data-t="h.sweep"]', '#swbanded', '.cmp');
            document.getElementById('swvalues').value = '';
            window.scrollTo(0, 0);
        }} else if (sc === 's6_2') {{
            setView('detail');
            hideExcept('#detail', 'h2[data-t="h.sweep"]', '#swbanded', '.cmp');
            const v = document.getElementById('swvalues');
            v.value = '25,50,75,100,150';
            v.classList.add('highlight-box');
            window.scrollTo(0, 0);
        }} else if (sc === 's6_3') {{
            setView('detail');
            hideExcept('#detail', 'h2[data-t="h.sweep"]', '#swbanded', '.cmp', '#sweep');
            document.getElementById('swvalues').value = '25,50,75,100,150';
            document.getElementById('swgo').click();
            await new Promise(r => setTimeout(r, 1200));
            window.scrollTo(0, 0);
        }} else if (sc === 's7_1') {{
            // The summary the closing question is asked over. Without the
            // hideExcept this was the plain view untouched, which is byte for
            // byte the opening frame - so the video ended by replaying its own
            // first seven seconds under a different voiceover.
            setView('plain');
            hideExcept('#plain', '#story', '#tiles');
            const p = document.getElementById('prediction');
            if (p) p.style.display = 'none';
            window.scrollTo(0, 0);
        }}
    }});
    </script>
    '''
    return html.replace('</body>', injected_script + '</body>')

root_app.mount('/ptm', ptm_app)

if __name__ == '__main__':
    uvicorn.run(root_app, host=__PTM_HOST__, port=__PTM_PORT__, log_level='warning')
"""
    # Substituted rather than typed twice. The URL every shot is fetched from is
    # built from HOST and PORT above, and a server listening somewhere else is a
    # run where all twenty-two frames fail on a connection error.
    server_py = (server_py.replace("__PTM_HOST__", repr(HOST))
                 .replace("__PTM_PORT__", str(PORT)))

    server_file = scratch / "server_prod.py"
    # encoding named rather than left to the platform default, which is the
    # ANSI codepage on Windows: this source carries the dashboard's own
    # punctuation and would be written in a codec uvicorn then cannot read.
    server_file.write_text(server_py, encoding="utf-8")

    python_bin, chrome_bin = find_python(), find_chrome()
    print(f"server:  {python_bin}")
    print(f"browser: {chrome_bin}")

    base = f"http://{HOST}:{PORT}"
    server_proc = subprocess.Popen([python_bin, str(server_file)])
    rendered = 0
    try:
        wait_for_server(f"{base}/ptm/api/domains", server_proc, SERVER_TIMEOUT)

        term_html = Path("scripts/templates/term_scene.html").resolve()
        dag_html = Path("scripts/templates/dag_scene.html").resolve()
        payoff_html = Path("scripts/templates/payoff_scene.html").resolve()
        # as_uri() rather than an f-string: a Windows path is C:\... and
        # "file://C:\..." is not a URL, so the query string on the terminal
        # shots - which is the whole of how those three frames differ - was
        # never going to reach the page.
        standalone = {
            "term_1": f"{term_html.as_uri()}?step=1",
            "term_2": f"{term_html.as_uri()}?step=2",
            "term_3": f"{term_html.as_uri()}?step=3",
            "dag_view": dag_html.as_uri(),
            "s7_2": payoff_html.as_uri(),
        }

        # Cards are drawn by assemble_video, not captured; only stills are here.
        shots = captures()
        for i, shot in enumerate(shots, 1):
            shot_file = out_dir / f"{shot['id']}.png"
            print(f"[{i}/{len(shots)}] Rendering {shot['id']}: {shot['title']}...")

            target_url = standalone.get(
                shot["sc"],
                f"{base}/ptm_capture?domain=expenses&version=v2&sc={shot['sc']}")

            cmd = [
                chrome_bin,
                "--headless=new",
                "--virtual-time-budget=3800",
                "--window-size=1920,1080",
                f"--force-device-scale-factor={SCALE}",
                "--hide-scrollbars",
                f"--screenshot={shot_file.resolve()}",
                target_url,
            ]
            subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           check=True)
            size_kb = shot_file.stat().st_size / 1024
            print(f"  -> Generated {shot_file.name} ({size_kb:.1f} KB)")
            rendered += 1
    finally:
        # In a finally, because a shot failing under check=True used to leave
        # uvicorn holding the port - so the next run failed at startup for a
        # reason that had nothing to do with what was actually wrong.
        server_proc.terminate()
        try:
            server_proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server_proc.kill()

    # Counted, not remembered: this said "All 22 shots" beside a list that is
    # free to be any length.
    print()
    print(f"All {rendered} shot(s) rendered successfully!")
    duplicates = _identical_frames(out_dir)
    if duplicates:
        # Two shots that rendered the same bytes are two spans of the finished
        # video holding one still while the narration moves on. The storyboard
        # refuses two shots sharing a capture directive; this catches the other
        # way in, where two different directives happen to produce one frame.
        for left, right in duplicates:
            print(f"WARNING {left} and {right} are byte-identical", file=sys.stderr)
        return 1
    return 0


def _identical_frames(out_dir: Path) -> list[tuple[str, str]]:
    """Pairs of rendered shots with the same content, in play order."""
    import hashlib

    seen: dict[str, str] = {}
    found: list[tuple[str, str]] = []
    for shot in captures():
        path = out_dir / f"{shot['id']}.png"
        if not path.exists():
            continue
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest in seen:
            found.append((seen[digest], shot["id"]))
        else:
            seen[digest] = shot["id"]
    return found


if __name__ == "__main__":
    raise SystemExit(main())
