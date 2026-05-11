import re
import os
import sys
import json
import time
import subprocess
import streamlit as st

OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scraper_output")
os.makedirs(OUTPUT_DIR, exist_ok=True)

# Disk-persisted state files (survive browser disconnect / screen sleep)
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
    if pid is None:
        return False
    try:
        os.kill(int(pid), 0)
        return True
    except PermissionError:
        return True   # process exists, no permission to signal — still running
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
        cols = st.columns(4)
        for i, (col, label) in enumerate(zip(cols, PHASE_LABELS), 1):
            if i < phase:
                col.success(f"✅ {label}")
            elif i == phase:
                col.warning(f"⏳ {label}")
            else:
                col.info(f"🔒 {label}")

        if total > 0:
            st.progress(pct, text=f"{PHASE_LABELS[phase-1]}: **{done} / {total} teams** ({pct*100:.1f}%)")
        else:
            st.progress(0.0, text=f"{PHASE_LABELS[phase-1] if phase <= 4 else 'Done'}: starting…")

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
    The scraper runs **4 phases** in sequence. A download button appears as soon as each file is ready.

    | Phase | What happens | Output File | Approx. Time |
    |-------|-------------|-------------|--------------|
    | **Phase 1** — Schedule Fetch | Fetches every team's game schedule using 20 parallel workers | *(no file)* | ~5 min |
    | **Phase 2** — Gap Analysis | Checks each game for box score availability. Classifies teams as Full / Partial / No stats | `{state}_data_gaps_{sport}_{season}.json` | ~15 min |
    | **Phase 3** — Box Score Scraping | Scrapes all available player box scores for every team | `{state}_box_scores_{sport}_{season}.json` | ~20 min |
    | **Phase 4** — Accumulation | Calculates season totals, per-game averages (PPG, RPG, APG etc.) and percentages for every player | `{state}_accumulated_stats_{sport}_{season}.json` | ~2 min |

    > **Total runtime:** 30–45 minutes depending on state size (TX ~1800 teams, smaller states ~300 teams).
    """)

st.divider()

# ── Read persisted state from disk (survives screen sleep / browser reconnect) ─
disk = load_disk_state()
running = disk is not None and is_pid_running(disk.get("pid"))

# ── Dropdowns (disabled while scraping) ──────────────────────────────────────
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

# ── Clear previous data option ────────────────────────────────────────────────
clear_previous = st.checkbox("🗑️ Clear previous data for this state/sport/season before starting",
                              value=False, disabled=running)

# ── Start button ──────────────────────────────────────────────────────────────
if st.button("▶ Start Scraping", type="primary", use_container_width=True, disabled=running):
    season_fn   = season.replace("-", "_")
    state_lower = state_code.lower()

    clear_disk_state()

    if clear_previous:
        for fname in [
            f"{state_lower}_data_gaps_{sport}_{season_fn}.json",
            f"{state_lower}_box_scores_{sport}_{season_fn}.json",
            f"{state_lower}_accumulated_stats_{sport}_{season_fn}.json",
        ]:
            fpath = os.path.join(OUTPUT_DIR, fname)
            if os.path.exists(fpath):
                os.remove(fpath)

    env = os.environ.copy()
    env["DATA_DIR"]         = OUTPUT_DIR
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"]       = "1"

    # Open log file in binary mode — subprocess writes UTF-8 bytes directly
    log_f = open(LOG_FILE, "wb")
    process = subprocess.Popen(
        [sys.executable, "-u", "app.py",
         "--state", state_code, "--sport", sport, "--season", season],
        stdout=log_f,
        stderr=subprocess.STDOUT,
        env=env,
        start_new_session=True,   # detach: survives browser disconnect
    )
    log_f.close()   # parent closes its copy; subprocess keeps writing

    save_disk_state({
        "pid":       process.pid,
        "label":     f"{STATE_NAMES[state_code]} | {'Boys' if sport=='boys' else 'Girls'} Basketball | {season}",
        "gaps_file": os.path.join(OUTPUT_DIR, f"{state_lower}_data_gaps_{sport}_{season_fn}.json"),
        "box_file":  os.path.join(OUTPUT_DIR, f"{state_lower}_box_scores_{sport}_{season_fn}.json"),
        "acc_file":  os.path.join(OUTPUT_DIR, f"{state_lower}_accumulated_stats_{sport}_{season_fn}.json"),
    })
    st.rerun()

# ── Live dashboard ────────────────────────────────────────────────────────────
if disk is not None:
    prog = parse_progress_from_log()
    logs = tail_log()

    st.info(f"Scraping: **{disk['label']}**")

    st.subheader("Progress")
    prog_ph = st.empty()
    render_progress(prog_ph, prog)

    st.divider()

    st.subheader("Output Files")
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

    st.divider()

    st.subheader("Live Logs")
    st.text_area("", value=logs, height=300, label_visibility="collapsed")

    if running:
        time.sleep(1.5)
        st.rerun()
    else:
        if os.path.exists(disk["acc_file"]):
            st.success("🎉 Scraping completed! Download your files above.")
        else:
            st.error("Scraping stopped or failed. Check the logs above.")

        if st.button("🔄 Start New Scrape", use_container_width=True):
            clear_disk_state()
            st.rerun()
