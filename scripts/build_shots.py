#!/usr/bin/env python3
import subprocess
import time
from pathlib import Path

SHOTS = [
    # Scene 1: The Bet (25.0s)
    {"id": "s1_shot1", "dur": 8.0, "sc": "s1_1", "title": "The Bet: Place your prediction"},
    {"id": "s1_shot2", "dur": 5.0, "sc": "s1_2", "title": "The Bet: Moving slider to 20%"},
    {"id": "s1_shot3", "dur": 5.0, "sc": "s1_3", "title": "The Bet: Guess comparison reveal"},
    {"id": "s1_shot4", "dur": 7.0, "sc": "s1_4", "title": "The Bet: 147 flips impact chart & coverage"},

    # Scene 2: The Plot Twist (30.0s)
    {"id": "s2_shot1", "dur": 7.0, "sc": "s2_1", "title": "The Plot Twist: Clauses overview"},
    {"id": "s2_shot2", "dur": 8.0, "sc": "s2_2", "title": "The Plot Twist: Clause 1.1 (48 changes)"},
    {"id": "s2_shot3", "dur": 7.0, "sc": "s2_3", "title": "The Plot Twist: 38 pre-existing deviations"},
    {"id": "s2_shot4", "dur": 8.0, "sc": "s2_4", "title": "The Evidence: Case review dialog & historical facts"},

    # Scene 3: No Spoilers from the Future (25.0s)
    {"id": "s3_shot1", "dur": 8.0, "sc": "term_1", "title": "Terminal: pit_check command & replay"},
    {"id": "s3_shot2", "dur": 9.0, "sc": "term_2", "title": "Terminal: 39 naive replay errors & future bias"},
    {"id": "s3_shot3", "dur": 8.0, "sc": "term_3", "title": "Terminal: manage.py coverage & provenance"},

    # Scene 4: Open the Machine (30.0s)
    {"id": "s4_shot1", "dur": 8.0, "sc": "s4_1", "title": "Under the Hood: Four core steps"},
    {"id": "s4_shot2", "dur": 12.0, "sc": "s4_2", "title": "Engine Room: Architecture diagram"},
    {"id": "s4_shot3", "dur": 10.0, "sc": "dag_view", "title": "Orchestration: Airflow DAG code"},

    # Scene 5: The Person Gets a Say (30.0s)
    {"id": "s5_shot1", "dur": 10.0, "sc": "s5_1", "title": "Human Decisions: 8 Precedents"},
    {"id": "s5_shot2", "dur": 10.0, "sc": "s5_2", "title": "Precedent Gate: Baseline comparison"},
    {"id": "s5_shot3", "dur": 10.0, "sc": "s5_3", "title": "Human Rationale: Resolution details"},

    # Scene 6: Let the Room Choose (25.0s)
    {"id": "s6_shot1", "dur": 7.0, "sc": "s6_1", "title": "Sweep: Threshold dial selection"},
    {"id": "s6_shot2", "dur": 10.0, "sc": "s6_2", "title": "Sweep: Computing trade-offs"},
    {"id": "s6_shot3", "dur": 8.0, "sc": "s6_3", "title": "Sweep: 25, 50, 75, 100, 150 GBP curve"},

    # Scene 7: Pay Off the Opening Question (15.0s)
    {"id": "s7_shot1", "dur": 7.0, "sc": "s7_1", "title": "Summary: Would you ship this rule?"},
    {"id": "s7_shot2", "dur": 8.0, "sc": "s7_2", "title": "Payoff: Final metrics & quickstart"},
]

def main():
    out_dir = Path("video_assets/shots")
    out_dir.mkdir(parents=True, exist_ok=True)

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
            setView('plain');
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
            setView('detail');
            hideExcept('#detail', 'h2[data-t="h.sweep"]', '#swbanded', '.cmp');
            document.getElementById('swvalues').value = '25,50,75,100,150';
            window.scrollTo(0, 0);
        }} else if (sc === 's6_2' || sc === 's6_3') {{
            setView('detail');
            hideExcept('#detail', 'h2[data-t="h.sweep"]', '#swbanded', '.cmp', '#sweep');
            document.getElementById('swvalues').value = '25,50,75,100,150';
            document.getElementById('swgo').click();
            await new Promise(r => setTimeout(r, 1200));
            window.scrollTo(0, 0);
        }} else if (sc === 's7_1') {{
            setView('plain');
            window.scrollTo(0, 0);
        }}
    }});
    </script>
    '''
    return html.replace('</body>', injected_script + '</body>')

root_app.mount('/ptm', ptm_app)

if __name__ == '__main__':
    uvicorn.run(root_app, host='127.0.0.1', port=8089, log_level='warning')
"""

    Path("scratch/server_prod.py").write_text(server_py)
    server_proc = subprocess.Popen([".venv/bin/python", "scratch/server_prod.py"])
    time.sleep(2)

    term_html = Path("scripts/templates/term_scene.html").resolve()
    dag_html = Path("scripts/templates/dag_scene.html").resolve()
    payoff_html = Path("scripts/templates/payoff_scene.html").resolve()

    chrome_bin = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

    for i, shot in enumerate(SHOTS, 1):
        shot_file = out_dir / f"{shot['id']}.png"
        print(f"[{i}/{len(SHOTS)}] Rendering {shot['id']}: {shot['title']}...")

        sc = shot["sc"]
        if sc == "term_1":
            target_url = f"file://{term_html}?step=1"
        elif sc == "term_2":
            target_url = f"file://{term_html}?step=2"
        elif sc == "term_3":
            target_url = f"file://{term_html}?step=3"
        elif sc == "dag_view":
            target_url = f"file://{dag_html}"
        elif sc == "s7_2":
            target_url = f"file://{payoff_html}"
        else:
            target_url = f"http://127.0.0.1:8089/ptm_capture?domain=expenses&version=v2&sc={sc}"

        cmd = [
            chrome_bin,
            "--headless=new",
            "--virtual-time-budget=3800",
            "--window-size=1920,1080",
            f"--screenshot={shot_file.resolve()}",
            target_url
        ]
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        size_kb = shot_file.stat().st_size / 1024
        print(f"  -> Generated {shot_file.name} ({size_kb:.1f} KB)")

    server_proc.terminate()
    print("\nAll 22 shots rendered successfully!")

if __name__ == "__main__":
    main()
