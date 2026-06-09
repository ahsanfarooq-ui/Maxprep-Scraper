import re
import os
import sys
import json
import time
import signal
import subprocess
import streamlit as st

OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scraper_output")
os.makedirs(OUTPUT_DIR, exist_ok=True)

STATE_FILE = os.path.join(OUTPUT_DIR, ".scraper_state.json")
LOG_FILE   = os.path.join(OUTPUT_DIR, ".scraper.log")

STATE_NAMES = {
    "AL": "Alabama",       "AK": "Alaska",         "AZ": "Arizona",
    "AR": "Arkansas",      "CA": "California",     "CO": "Colorado",
    "CT": "Connecticut",   "DE": "Delaware",       "FL": "Florida",
    "GA": "Georgia",       "HI": "Hawaii",         "ID": "Idaho",
    "IL": "Illinois",      "IN": "Indiana",        "IA": "Iowa",
    "KS": "Kansas",        "KY": "Kentucky",       "LA": "Louisiana",
    "ME": "Maine",         "MD": "Maryland",       "MA": "Massachusetts",
    "MI": "Michigan",      "MN": "Minnesota",      "MS": "Mississippi",
    "MO": "Missouri",      "MT": "Montana",        "NE": "Nebraska",
    "NV": "Nevada",        "NH": "New Hampshire",  "NJ": "New Jersey",
    "NM": "New Mexico",    "NY": "New York",       "NC": "North Carolina",
    "ND": "North Dakota",  "OH": "Ohio",           "OK": "Oklahoma",
    "OR": "Oregon",        "PA": "Pennsylvania",   "RI": "Rhode Island",
    "SC": "South Carolina","SD": "South Dakota",   "TN": "Tennessee",
    "TX": "Texas",         "UT": "Utah",           "VT": "Vermont",
    "VA": "Virginia",      "WA": "Washington",     "WV": "West Virginia",
    "WI": "Wisconsin",     "WY": "Wyoming",        "DC": "District of Columbia",
}

SEASONS        = [f"{y}-{y+1}" for y in range(2029, 2019, -1)]
DEFAULT_SEASON = "2025-2026"
PHASE_LABELS   = [
    "Phase 1 — Fetching Schedules",
    "Phase 2 — Gap Analysis",
    "Phase 3 — Scraping Box Scores",
    "Phase 4 — Accumulating Stats",
    "Phase 5 — Finding Stats-Tab Teams",
    "Phase 6 — Updating Accumulated File",
]


# ── Log parser ────────────────────────────────────────────────────────────────
def parse_log(line, state):
    if "Phase 1:" in line:
        state["phase"] = 1
        m = re.search(r"Fetching\s+(\d+)\s+schedules", line)
        if m:
            state["total"] = int(m.group(1))

    m = re.search(r"Schedules:\s*(\d+)/(\d+)", line)
    if m:
        state["phase"] = 1
        state["done"]  = int(m.group(1))
        state["total"] = int(m.group(2))

    if "Phase 2:" in line:
        state["phase"] = 2
        state["done"]  = 0

    m = re.search(r"\[\s*(\d+)/\s*(\d+)\].*Full:\s*(\d+).*Part:\s*(\d+)\s*\|\s*(.+)", line)
    if m:
        state["phase"]   = 2
        state["done"]    = int(m.group(1))
        state["total"]   = int(m.group(2))
        state["full"]    = int(m.group(3))
        state["partial"] = int(m.group(4))
        state["team"]    = m.group(5).strip()

    if "Teams to process" in line or "Starting scraper" in line:
        state["phase"] = 3
        state["done"]  = 0
        m = re.search(r"(\d+)\s+\(full \+ partial\)", line)
        if m:
            state["total"] = int(m.group(1))

    m = re.search(r"Processing team\s+(\d+)/(\d+):\s*(.+)", line)
    if m:
        state["phase"] = 3
        state["done"]  = int(m.group(1))
        state["total"] = int(m.group(2))
        state["team"]  = m.group(3).strip()

    m = re.search(r"\[DONE\] Added\s+(\d+)\s+games for", line)
    if m:
        state["games"] = state.get("games", 0) + int(m.group(1))

    if "Running data accumulation" in line:
        state["phase"] = 4
        state["done"]  = 0

    m = re.search(r"Accumulating:\s*(\d+)/(\d+)", line)
    if m:
        state["phase"] = 4
        state["done"]  = int(m.group(1))
        state["total"] = int(m.group(2))

    # Phase 5: find_stats_only_teams.py
    if "STAGE 4:" in line or "find_stats_only_teams" in line:
        state["phase"] = 5
        state["done"]  = 0
    m = re.search(r"\[\s*(\d+)/\s*(\d+)\]\s+(?:FLAG|skip:|no-stats|CRASH|ok:)", line)
    if m:
        state["phase"] = 5
        state["done"]  = int(m.group(1))
        state["total"] = int(m.group(2))

    # Phase 6: accumulate_from_stats_tab.py + merge_stats_tab_into_accumulated.py
    if "STAGE 5:" in line or "accumulate_from_stats_tab" in line:
        state["phase"] = 6
        state["done"]  = 0
    m = re.search(r"\[\s*(\d+)/\s*(\d+)\]\s+(?:OK|skip:|CRASH)\s+\|", line)
    if m and state.get("phase", 0) >= 5:
        state["phase"] = 6
        state["done"]  = int(m.group(1))
        state["total"] = int(m.group(2))
    if "STAGE 6:" in line or "merge_stats_tab_into_accumulated" in line:
        state["phase"] = 6
    if "PIPELINE COMPLETE" in line:
        state["phase"] = 6
        state["done"]  = state.get("total", 1)

    return state


# ── Disk state helpers ────────────────────────────────────────────────────────
def load_disk_state():
    if not os.path.exists(STATE_FILE):
        return None
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None

def save_disk_state(d):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(d, f)

def clear_disk_state():
    for p in [STATE_FILE, LOG_FILE]:
        try:
            os.remove(p)
        except FileNotFoundError:
            pass

def is_pid_running(pid):
    """Reliable cross-platform PID check. Uses /proc on Linux (Streamlit Cloud)."""
    if pid is None:
        return False
    try:
        pid = int(pid)
        # Linux (Streamlit Cloud): /proc/<pid> disappears the moment process dies
        if os.path.exists(f"/proc/{pid}"):
            return True
        # Windows / macOS fallback
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True   # process exists but we can't signal it
    except Exception:
        return False


def stop_pid(pid):
    """Kill the scraper subprocess (and any children it spawned) cross-platform."""
    if pid is None:
        return False
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if sys.platform == "win32":
        # /T = also kill child processes, /F = force
        try:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                           check=False, capture_output=True)
            return True
        except Exception:
            return False
    # Unix: signal the whole process group (Popen used start_new_session=True)
    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM)
        return True
    except Exception:
        try:
            os.kill(pid, signal.SIGTERM)
            return True
        except Exception:
            return False

def tail_log(n=60):
    if not os.path.exists(LOG_FILE):
        return ""
    try:
        with open(LOG_FILE, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        return "\n".join(l.rstrip() for l in lines[-n:] if l.strip())
    except Exception:
        return ""

def parse_progress_from_log():
    prog = {"phase": 1, "done": 0, "total": 0,
            "full": 0, "partial": 0, "team": "", "games": 0}
    if not os.path.exists(LOG_FILE):
        return prog
    try:
        with open(LOG_FILE, encoding="utf-8", errors="replace") as f:
            for line in f:
                prog = parse_log(line.rstrip(), prog)
    except Exception:
        pass
    return prog


# ── Progress renderer ─────────────────────────────────────────────────────────
def render_progress(ph, state):
    phase = state["phase"]
    done  = state["done"]
    total = state["total"]
    pct   = done / total if total > 0 else 0.0

    with ph.container():
        # Two rows of three phase chips.
        row1 = st.columns(3)
        row2 = st.columns(3)
        cols = list(row1) + list(row2)
        for i, (col, label) in enumerate(zip(cols, PHASE_LABELS), 1):
            if i < phase:
                col.success(f"✅ {label}")
            elif i == phase:
                col.warning(f"⏳ {label}")
            else:
                col.info(f"🔒 {label}")

        label_idx = min(max(phase, 1), len(PHASE_LABELS)) - 1
        if total > 0:
            st.progress(pct, text=f"{PHASE_LABELS[label_idx]}: **{done} / {total} teams** ({pct*100:.1f}%)")
        else:
            st.progress(0.0, text=f"{PHASE_LABELS[label_idx]}: starting…")

        team = state.get("team", "")
        if team:
            st.caption(f"⚙️ Currently processing: **{team}**")

        if phase >= 2 and done > 0:
            m1, m2, m3, m4, m5 = st.columns(5)
            m1.metric("Teams Done",         done)
            m2.metric("Full Box Scores",    state["full"])
            m3.metric("Partial Box Scores", state["partial"])
            m4.metric("No Box Scores",      max(0, done - state["full"] - state["partial"]))
            m5.metric("Games Scraped",      state.get("games", 0))


# ── Download helper ───────────────────────────────────────────────────────────
def show_download(placeholder, filepath, label):
    if os.path.exists(filepath):
        with open(filepath, "r", encoding="utf-8") as f:
            data = f.read()
        placeholder.download_button(
            label=f"✅ {label}",
            data=data,
            file_name=os.path.basename(filepath),
            mime="application/json",
            use_container_width=True,
        )
        return True
    return False


# ══════════════════════════════════════════════════════════════════════════════
#  PAGE
# ══════════════════════════════════════════════════════════════════════════════

st.set_page_config(page_title="MaxPreps Basketball Scraper", page_icon="🏀", layout="wide")
st.title("🏀 MaxPreps Basketball Scraper")
st.markdown("Select your options and click **Start Scraping** to begin.")

with st.expander("📋 How the scraper works (click to expand)", expanded=False):
    st.markdown("""
    The scraper runs **6 phases** in sequence. A download button appears as soon as each file is ready.

    | Phase | What happens | Output File | Approx. Time |
    |-------|-------------|-------------|--------------|
    | **Phase 1** — Schedule Fetch | Fetches every team's game schedule using 20 parallel workers | *(no file)* | ~5 min |
    | **Phase 2** — Gap Analysis | Checks each game for box score availability. Classifies teams as Full / Partial / No stats | `{state}_data_gaps_{sport}_{season}.json` | ~15 min |
    | **Phase 3** — Box Score Scraping | Scrapes all available player box scores for every team | `{state}_box_scores_{sport}_{season}.json` | ~20 min |
    | **Phase 4** — Accumulation | Calculates season totals, per-game averages (PPG, RPG, APG etc.) and percentages for every player | `{state}_accumulated_stats_{sport}_{season}.json` | ~2 min |
    | **Phase 5** — Stats-Tab Teams | Identifies teams whose game-by-game data is missing but who have season totals on their Stats tab | `{state}_stats_only_teams_{sport}_{season}.json` | ~3 min |
    | **Phase 6** — Updated Accumulation | Scrapes those teams' Stats tab and merges into the accumulated file (without modifying the original) | `{state}_accumulated_stats_{sport}_{season}_updated.json` (plus `_stats_tab_accumulated_*.json` as a sidecar) | ~2 min |

    > **Total runtime:** 35–55 minutes depending on state size (TX ~1800 teams, smaller states ~300 teams).
    """)

st.divider()

# ── Load persisted state ──────────────────────────────────────────────────────
disk    = load_disk_state()
running = disk is not None and is_pid_running(disk.get("pid"))

# ── Dropdowns — only disabled while actively running ─────────────────────────
col1, col2, col3 = st.columns(3)
with col1:
    state_code = st.selectbox("State", options=list(STATE_NAMES.keys()),
                               format_func=lambda x: f"{x} — {STATE_NAMES[x]}", disabled=running)
with col2:
    sport = st.selectbox("Sport", options=["boys", "girls"],
                          format_func=lambda x: "Boys Basketball" if x == "boys" else "Girls Basketball",
                          disabled=running)
with col3:
    season = st.selectbox("Season", options=SEASONS,
                           index=SEASONS.index(DEFAULT_SEASON), disabled=running)

st.divider()

clear_previous = st.checkbox("🗑️ Clear previous data for this state/sport/season before starting",
                              value=False, disabled=running)

# ── Start button — enabled whenever scraper is not actively running ───────────
if st.button("▶ Start Scraping", type="primary", use_container_width=True, disabled=running):
    season_fn   = season.replace("-", "_")
    state_lower = state_code.lower()

    clear_disk_state()   # always wipe old log/state before new run

    if clear_previous:
        for fname in [
            f"{state_lower}_data_gaps_{sport}_{season_fn}.json",
            f"{state_lower}_box_scores_{sport}_{season_fn}.json",
            f"{state_lower}_accumulated_stats_{sport}_{season_fn}.json",
            f"{state_lower}_stats_only_teams_{sport}_{season_fn}.json",
            f"{state_lower}_stats_tab_accumulated_{sport}_{season_fn}.json",
            f"{state_lower}_stats_tab_accumulated_{sport}_{season_fn}_report.json",
            f"{state_lower}_accumulated_stats_{sport}_{season_fn}_updated.json",
        ]:
            fpath = os.path.join(OUTPUT_DIR, fname)
            if os.path.exists(fpath):
                os.remove(fpath)

    env = os.environ.copy()
    env["DATA_DIR"]         = OUTPUT_DIR
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"]       = "1"
    # Critical for live log streaming: forces every Python in the subprocess
    # tree to flush stdout immediately instead of block-buffering it to the
    # log file. Without this, the orchestrator's child stages (app.py,
    # scrape_box_scores.py, etc.) only appear in the log when each child
    # exits, which makes the live log feel frozen for minutes at a time.
    env["PYTHONUNBUFFERED"] = "1"

    log_f = open(LOG_FILE, "wb")
    # Launch the full 6-stage pipeline (run_full_pipeline.py) so all 6 outputs
    # land in OUTPUT_DIR via --output-dir. The previous Streamlit version only
    # ran app.py which covered stages 1–3.
    process = subprocess.Popen(
        [sys.executable, "-u", "run_full_pipeline.py",
         "--state", state_code, "--sport", sport, "--season", season,
         "--output-dir", OUTPUT_DIR],
        stdout=log_f,
        stderr=subprocess.STDOUT,
        env=env,
        start_new_session=True,
    )
    log_f.close()

    save_disk_state({
        "pid":           process.pid,
        "label":         f"{STATE_NAMES[state_code]} | {'Boys' if sport=='boys' else 'Girls'} Basketball | {season}",
        # Phases 1–4 (per-game pipeline)
        "gaps_file":     os.path.join(OUTPUT_DIR, f"{state_lower}_data_gaps_{sport}_{season_fn}.json"),
        "box_file":      os.path.join(OUTPUT_DIR, f"{state_lower}_box_scores_{sport}_{season_fn}.json"),
        "acc_file":      os.path.join(OUTPUT_DIR, f"{state_lower}_accumulated_stats_{sport}_{season_fn}.json"),
        # Phases 5–6 (stats-tab fallback pipeline)
        "stats_only_file":  os.path.join(OUTPUT_DIR, f"{state_lower}_stats_only_teams_{sport}_{season_fn}.json"),
        "stats_tab_file":   os.path.join(OUTPUT_DIR, f"{state_lower}_stats_tab_accumulated_{sport}_{season_fn}.json"),
        "updated_file":     os.path.join(OUTPUT_DIR, f"{state_lower}_accumulated_stats_{sport}_{season_fn}_updated.json"),
    })
    st.rerun()

# ── Dashboard: shown while running OR after completion ────────────────────────
if disk is not None:
    st.divider()

    if running:
        info_col, btn_col = st.columns([4, 1])
        with info_col:
            st.info(f"⏳ Scraping in progress: **{disk['label']}**")
        with btn_col:
            # Restart: stops the running scrape and returns to the selection screen.
            if st.button("🛑 Restart", type="secondary", use_container_width=True,
                         help="Stop the current scrape and pick a new state/sport/season."):
                stop_pid(disk.get("pid"))
                clear_disk_state()
                st.rerun()
    else:
        if os.path.exists(disk.get("updated_file", "")):
            st.success(f"🎉 Completed: **{disk['label']}** — Download your files below, then start a new scrape above.")
        elif os.path.exists(disk.get("acc_file", "")):
            st.warning(f"⚠️ Pipeline stopped after Phase 4: **{disk['label']}** — partial outputs available below.")
        else:
            st.warning(f"⚠️ Stopped/failed: **{disk['label']}** — Check logs below. You can start a new scrape above.")

    # Progress
    prog    = parse_progress_from_log()
    prog_ph = st.empty()
    render_progress(prog_ph, prog)

    st.divider()

    # Output files — 6 files across 2 rows of 3
    st.subheader("Output Files")

    # Row 1: Phases 2, 3, 4 (per-game pipeline)
    fc1, fc2, fc3 = st.columns(3)
    with fc1:
        st.markdown("**Phase 2 — Data Gaps**")
        gaps_ph = st.empty()
        if not show_download(gaps_ph, disk["gaps_file"], "Download Data Gaps"):
            gaps_ph.warning("⏳ Generating...")
    with fc2:
        st.markdown("**Phase 3 — Box Scores**")
        box_ph = st.empty()
        if not show_download(box_ph, disk["box_file"], "Download Box Scores"):
            box_ph.info("🔒 Waiting...")
    with fc3:
        st.markdown("**Phase 4 — Accumulated Stats**")
        acc_ph = st.empty()
        if not show_download(acc_ph, disk["acc_file"], "Download Accumulated Stats"):
            acc_ph.info("🔒 Waiting...")

    # Row 2: Phases 5, 6 (stats-tab fallback)
    fc4, fc5, fc6 = st.columns(3)
    with fc4:
        st.markdown("**Phase 5 — Stats-Tab Teams**")
        sot_ph = st.empty()
        if not show_download(sot_ph, disk.get("stats_only_file", ""),
                             "Download Stats-Tab Teams"):
            sot_ph.info("🔒 Waiting...")
    with fc5:
        st.markdown("**Phase 5 — Stats-Tab Records**")
        stab_ph = st.empty()
        if not show_download(stab_ph, disk.get("stats_tab_file", ""),
                             "Download Stats-Tab Records"):
            stab_ph.info("🔒 Waiting...")
    with fc6:
        st.markdown("**Phase 6 — Updated Accumulated**")
        upd_ph = st.empty()
        if not show_download(upd_ph, disk.get("updated_file", ""),
                             "Download Updated Accumulated"):
            upd_ph.info("🔒 Waiting...")

    # Logs
    with st.expander("📄 Logs", expanded=running):
        st.text_area("", value=tail_log(), height=300, label_visibility="collapsed")

    # After completion — show reset button
    if not running:
        st.divider()
        if st.button("🔄 Scrape Another State / Sport / Season",
                     type="primary", use_container_width=True):
            clear_disk_state()
            st.rerun()

    # Auto-refresh while running
    if running:
        time.sleep(1.5)
        st.rerun()
