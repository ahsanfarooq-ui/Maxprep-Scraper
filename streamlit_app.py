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
    "Phase 1 — Gap Finder",
    "Phase 2 — Stats Section (every team)",
    "Phase 3 — Box Scores",
    "Phase 4 — Final Accumulation",
]


def short_season(season):
    """'2025-2026' -> '25_26'."""
    parts = season.replace('_', '-').split('-')
    if len(parts) == 2 and all(len(p) == 4 and p.isdigit() for p in parts):
        return f"{parts[0][-2:]}_{parts[1][-2:]}"
    return season.replace('-', '_')


# ── Log parser ────────────────────────────────────────────────────────────────
# Maps the orchestrator's own stage banners + each child script's progress lines
# onto the four-phase UI. Banners are stable across runs; progress regexes
# match the [N/M] format every child uses.
def parse_log(line, state):
    # Stage banners from APP/pipeline.py — definitive phase markers.
    if "STAGE 1/4" in line:
        state["phase"] = 1
        state["done"]  = 0
    elif "STAGE 2/4" in line:
        state["phase"] = 2
        state["done"]  = 0
    elif "STAGE 3/4" in line:
        state["phase"] = 3
        state["done"]  = 0
    elif "STAGE 4/4" in line:
        state["phase"] = 4
        state["done"]  = 0

    # Phase 1 — gap finder
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

    m = re.search(r"\[\s*(\d+)/\s*(\d+)\].*Full:\s*(\d+).*Part:\s*(\d+)\s*\|\s*(.+)", line)
    if m:
        state["phase"]   = 1
        state["done"]    = int(m.group(1))
        state["total"]   = int(m.group(2))
        state["full"]    = int(m.group(3))
        state["partial"] = int(m.group(4))
        state["team"]    = m.group(5).strip()

    # Phase 2 — stats-tab (accumulate_from_stats_tab.py)
    # `[ 12/326] OK     | Team Name      GP=27  players=11`
    m = re.search(r"\[\s*(\d+)/\s*(\d+)\]\s+(?:OK|skip:|CRASH)\s+\|", line)
    if m and state.get("phase", 0) == 2:
        state["done"]  = int(m.group(1))
        state["total"] = int(m.group(2))

    # Phase 3 — box scores
    m = re.search(r"Processing team\s+(\d+)/(\d+):\s*(.+)", line)
    if m:
        state["phase"] = 3
        state["done"]  = int(m.group(1))
        state["total"] = int(m.group(2))
        state["team"]  = m.group(3).strip()

    m = re.search(r"\[DONE\] Added\s+(\d+)\s+games for", line)
    if m:
        state["games"] = state.get("games", 0) + int(m.group(1))

    # Phase 4 — final accumulation
    if "4a) per-game accumulator" in line:
        state["phase"] = 4
    m = re.search(r"Accumulating:\s*(\d+)/(\d+)", line)
    if m:
        state["phase"] = 4
        state["done"]  = int(m.group(1))
        state["total"] = int(m.group(2))

    if "PIPELINE COMPLETE" in line:
        state["phase"] = 4
        state["done"]  = state.get("total", 1) or 1

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
        if os.path.exists(f"/proc/{pid}"):
            return True
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except Exception:
        return False


def stop_pid(pid):
    if pid is None:
        return False
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if sys.platform == "win32":
        try:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                           check=False, capture_output=True)
            return True
        except Exception:
            return False
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
        # Four phase chips in one row.
        cols = st.columns(4)
        for i, (col, label) in enumerate(zip(cols, PHASE_LABELS), 1):
            if i < phase:
                col.success(f"✅ {label}")
            elif i == phase:
                col.warning(f"⏳ {label}")
            else:
                col.info(f"🔒 {label}")

        label_idx = min(max(phase, 1), len(PHASE_LABELS)) - 1
        if total > 0:
            st.progress(pct, text=f"{PHASE_LABELS[label_idx]}: **{done} / {total}** ({pct*100:.1f}%)")
        else:
            st.progress(0.0, text=f"{PHASE_LABELS[label_idx]}: starting…")

        team = state.get("team", "")
        if team:
            st.caption(f"⚙️ Currently processing: **{team}**")

        if phase >= 1 and done > 0:
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
    The pipeline runs **4 phases** in sequence. A download button appears as soon as each file is ready.

    | Phase | What happens | Output File |
    |-------|-------------|-------------|
    | **Phase 1** — Gap Finder | Fetches every team's schedule, classifies each game's box-score availability (Full / Partial / No stats). | `{state}_data_gaps_{sport}_{season}.json` |
    | **Phase 2** — Stats Section | Hits the print-stats endpoint for **every** team the gap finder discovered. Saves any team whose coach has uploaded season totals + per-player rows. | `{state}_all_stats_tab_{sport}_{season}.json` |
    | **Phase 3** — Box Scores | Scrapes per-game player box scores from every available game page. | `{state}_box_scores_{sport}_{season}.json` |
    | **Phase 4** — Final Accumulation | Per-game accumulator runs, then stats-tab data overlays it (stats-tab wins for any team that has data). TotalGamesChecked is set to `max(gap, box_count, GP)` so `GP ≤ TGC` always holds. | `Final_{state}_accumulated_{sport}_{ss}.json` |

    > **Total runtime:** typically 5–15 min for AR/LA/NM/OK, 30–50 min for TX.
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
    ss          = short_season(season)
    state_lower = state_code.lower()

    clear_disk_state()

    if clear_previous:
        for fname in [
            f"{state_lower}_data_gaps_{sport}_{season_fn}.json",
            f"{state_lower}_all_stats_tab_{sport}_{season_fn}.json",
            f"{state_lower}_all_stats_tab_{sport}_{season_fn}_report.json",
            f"{state_lower}_box_scores_{sport}_{season_fn}.json",
            f"{state_lower}_accumulated_stats_{sport}_{season_fn}.json",
            f"Final_{state_lower}_accumulated_{sport}_{ss}.json",
        ]:
            fpath = os.path.join(OUTPUT_DIR, fname)
            if os.path.exists(fpath):
                os.remove(fpath)

    env = os.environ.copy()
    env["DATA_DIR"]         = OUTPUT_DIR
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"]       = "1"
    env["PYTHONUNBUFFERED"] = "1"

    log_f = open(LOG_FILE, "wb")
    # New 4-stage pipeline: APP/pipeline.py. --output-dir routes all 4 outputs
    # to OUTPUT_DIR so the UI can find them via predictable filenames.
    process = subprocess.Popen(
        [sys.executable, "-u", "APP/pipeline.py",
         "--state", state_code, "--sport", sport, "--season", season,
         "--output-dir", OUTPUT_DIR],
        stdout=log_f,
        stderr=subprocess.STDOUT,
        env=env,
        start_new_session=True,
    )
    log_f.close()

    save_disk_state({
        "pid":         process.pid,
        "label":       f"{STATE_NAMES[state_code]} | {'Boys' if sport=='boys' else 'Girls'} Basketball | {season}",
        "gaps_file":   os.path.join(OUTPUT_DIR, f"{state_lower}_data_gaps_{sport}_{season_fn}.json"),
        "stab_file":   os.path.join(OUTPUT_DIR, f"{state_lower}_all_stats_tab_{sport}_{season_fn}.json"),
        "box_file":    os.path.join(OUTPUT_DIR, f"{state_lower}_box_scores_{sport}_{season_fn}.json"),
        "final_file":  os.path.join(OUTPUT_DIR, f"Final_{state_lower}_accumulated_{sport}_{ss}.json"),
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
            if st.button("🛑 Restart", type="secondary", use_container_width=True,
                         help="Stop the current scrape and pick a new state/sport/season."):
                stop_pid(disk.get("pid"))
                clear_disk_state()
                st.rerun()
    else:
        if os.path.exists(disk.get("final_file", "")):
            st.success(f"🎉 Completed: **{disk['label']}** — Download your files below, then start a new scrape above.")
        elif os.path.exists(disk.get("box_file", "")):
            st.warning(f"⚠️ Pipeline stopped after Phase 3 — partial outputs available below.")
        else:
            st.warning(f"⚠️ Stopped/failed: **{disk['label']}** — Check logs below. You can start a new scrape above.")

    # Progress
    prog    = parse_progress_from_log()
    prog_ph = st.empty()
    render_progress(prog_ph, prog)

    st.divider()

    # Output files — 4 files in one row
    st.subheader("Output Files")
    fc1, fc2, fc3, fc4 = st.columns(4)
    with fc1:
        st.markdown("**Phase 1 — Data Gaps**")
        gaps_ph = st.empty()
        if not show_download(gaps_ph, disk["gaps_file"], "Download Data Gaps"):
            gaps_ph.warning("⏳ Generating...")
    with fc2:
        st.markdown("**Phase 2 — Stats Section**")
        stab_ph = st.empty()
        if not show_download(stab_ph, disk["stab_file"], "Download Stats Section"):
            stab_ph.info("🔒 Waiting...")
    with fc3:
        st.markdown("**Phase 3 — Box Scores**")
        box_ph = st.empty()
        if not show_download(box_ph, disk["box_file"], "Download Box Scores"):
            box_ph.info("🔒 Waiting...")
    with fc4:
        st.markdown("**Phase 4 — Final Accumulation**")
        final_ph = st.empty()
        if not show_download(final_ph, disk["final_file"], "Download FINAL"):
            final_ph.info("🔒 Waiting...")

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
