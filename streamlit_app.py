import re
import streamlit as st
import subprocess
import sys
import os

OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scraper_output")
os.makedirs(OUTPUT_DIR, exist_ok=True)

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

SEASONS       = [f"{y}-{y+1}" for y in range(2029, 2019, -1)]
DEFAULT_SEASON = "2025-2026"
PHASE_LABELS  = [
    "Phase 1 — Fetching Schedules",
    "Phase 2 — Gap Analysis",
    "Phase 3 — Scraping Box Scores",
    "Phase 4 — Accumulating Stats",
]

# ── Parse a log line into progress state ──────────────────────────────────────
def parse_log(line, state):
    # Phase 1 start
    if "Phase 1:" in line:
        state["phase"] = 1
        m = re.search(r"Fetching\s+(\d+)\s+schedules", line)
        if m:
            state["total"] = int(m.group(1))

    # Phase 1 progress
    m = re.search(r"Schedules:\s*(\d+)/(\d+)", line)
    if m:
        state["phase"]    = 1
        state["done"]     = int(m.group(1))
        state["total"]    = int(m.group(2))

    # Phase 2 start
    if "Phase 2:" in line:
        state["phase"] = 2
        state["done"]  = 0

    # Phase 2 progress  [  42/ 600]  7.0% | Full:   18 | Part:    3 | TeamName
    m = re.search(r"\[\s*(\d+)/\s*(\d+)\].*Full:\s*(\d+).*Part:\s*(\d+)\s*\|\s*(.+)", line)
    if m:
        state["phase"]   = 2
        state["done"]    = int(m.group(1))
        state["total"]   = int(m.group(2))
        state["full"]    = int(m.group(3))
        state["partial"] = int(m.group(4))
        state["team"]    = m.group(5).strip()

    # Phase 3 start
    if "Teams to process" in line or "Starting scraper" in line:
        state["phase"] = 3
        state["done"]  = 0
        m = re.search(r"(\d+)\s+\(full \+ partial\)", line)
        if m:
            state["total"] = int(m.group(1))

    # Phase 3 progress  "Processing team 12/340: TeamName"
    m = re.search(r"Processing team\s+(\d+)/(\d+):\s*(.+)", line)
    if m:
        state["phase"] = 3
        state["done"]  = int(m.group(1))
        state["total"] = int(m.group(2))
        state["team"]  = m.group(3).strip()

    # Phase 3 done line  "[DONE] Added 5 games for TeamName"
    m = re.search(r"\[DONE\] Added\s+(\d+)\s+games for (.+)", line)
    if m:
        state["games"] = state.get("games", 0) + int(m.group(1))

    # Phase 4 start
    if "Running data accumulation" in line or "Accumulation complete" in line:
        state["phase"] = 4
        state["done"]  = 0

    # Phase 4 progress  "Accumulating: 500/12000 games processed..."
    m = re.search(r"Accumulating:\s*(\d+)/(\d+)", line)
    if m:
        state["phase"] = 4
        state["done"]  = int(m.group(1))
        state["total"] = int(m.group(2))

    return state


# ── Render the progress section ───────────────────────────────────────────────
def render_progress(ph, state):
    phase   = state["phase"]
    done    = state["done"]
    total   = state["total"]
    pct     = done / total if total > 0 else 0.0

    with ph.container():
        # Phase step indicators
        cols = st.columns(4)
        for i, (col, label) in enumerate(zip(cols, PHASE_LABELS), 1):
            if i < phase:
                col.success(f"✅ {label}")
            elif i == phase:
                col.warning(f"⏳ {label}")
            else:
                col.info(f"🔒 {label}")

        # Progress bar
        if total > 0:
            st.progress(pct, text=f"{label_for(phase)}: **{done} / {total} teams**  ({pct*100:.1f}%)")
        else:
            st.progress(0.0, text=f"{label_for(phase)}: starting…")

        # Current team being processed
        current_team = state.get("team", "")
        if current_team:
            st.caption(f"⚙️ Currently processing: **{current_team}**")

        # Metrics row
        if phase >= 2 and done > 0:
            m1, m2, m3, m4, m5 = st.columns(5)
            m1.metric("Teams Done",         done)
            m2.metric("Full Box Scores",    state["full"])
            m3.metric("Partial Box Scores", state["partial"])
            m4.metric("No Box Scores",      max(0, done - state["full"] - state["partial"]))
            m5.metric("Games Scraped",      state.get("games", 0))


def label_for(phase):
    return PHASE_LABELS[phase - 1] if 1 <= phase <= 4 else "Starting…"


# ═══════════════════════════════════════════════════════════════════════════════
#  UI
# ═══════════════════════════════════════════════════════════════════════════════

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

col1, col2, col3 = st.columns(3)
with col1:
    state_code = st.selectbox(
        "State",
        options=list(STATE_NAMES.keys()),
        format_func=lambda x: f"{x} — {STATE_NAMES[x]}"
    )
with col2:
    sport = st.selectbox(
        "Sport",
        options=["boys", "girls"],
        format_func=lambda x: "Boys Basketball" if x == "boys" else "Girls Basketball"
    )
with col3:
    season = st.selectbox("Season", options=SEASONS, index=SEASONS.index(DEFAULT_SEASON))

st.divider()

if st.button("▶ Start Scraping", type="primary", use_container_width=True):

    season_fn   = season.replace("-", "_")
    state_lower = state_code.lower()
    state_name  = STATE_NAMES[state_code]
    sport_label = "Boys" if sport == "boys" else "Girls"

    st.info(f"Scraping **{state_name}** | **{sport_label} Basketball** | **{season}**")

    # ── File paths ────────────────────────────────────────────────────────────
    gaps_file = os.path.join(OUTPUT_DIR, f"{state_lower}_data_gaps_{sport}_{season_fn}.json")
    box_file  = os.path.join(OUTPUT_DIR, f"{state_lower}_box_scores_{sport}_{season_fn}.json")
    acc_file  = os.path.join(OUTPUT_DIR, f"{state_lower}_accumulated_stats_{sport}_{season_fn}.json")

    # ── Progress section ──────────────────────────────────────────────────────
    st.subheader("Progress")
    progress_ph = st.empty()
    prog_state  = {"phase": 1, "done": 0, "total": 0, "full": 0, "partial": 0, "team": "", "games": 0}
    render_progress(progress_ph, prog_state)

    st.divider()

    # ── Output files ──────────────────────────────────────────────────────────
    st.subheader("Output Files")
    fc1, fc2, fc3 = st.columns(3)
    with fc1:
        st.markdown("**Phase 2 — Data Gaps**")
        gaps_ph = st.empty()
        gaps_ph.warning("⏳ Generating...")
    with fc2:
        st.markdown("**Phase 3 — Box Scores**")
        box_ph = st.empty()
        box_ph.info("🔒 Waiting for Phase 2...")
    with fc3:
        st.markdown("**Phase 4 — Accumulated Stats**")
        acc_ph = st.empty()
        acc_ph.info("🔒 Waiting for Phase 3...")

    st.divider()

    # ── Live logs ─────────────────────────────────────────────────────────────
    st.subheader("Live Logs")
    log_ph = st.empty()
    logs   = []

    gaps_ready = box_ready = acc_ready = False

    env = os.environ.copy()
    env["DATA_DIR"] = OUTPUT_DIR

    process = subprocess.Popen(
        [sys.executable, "-u", "app.py",
         "--state", state_code, "--sport", sport, "--season", season],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=env,
        bufsize=1,
    )

    for line in process.stdout:
        line = line.rstrip()
        if line:
            # Update progress
            prog_state = parse_log(line, prog_state)
            render_progress(progress_ph, prog_state)

            # Update logs
            logs.append(line)
            log_ph.text_area("", value="\n".join(logs[-60:]), height=300,
                             label_visibility="collapsed")

        # Download buttons as files appear
        if not gaps_ready and os.path.exists(gaps_file):
            gaps_ready = True
            with open(gaps_file, "r") as f:
                gaps_ph.download_button("✅ Download Data Gaps", data=f.read(),
                    file_name=os.path.basename(gaps_file), mime="application/json",
                    use_container_width=True)
            box_ph.warning("⏳ Scraping box scores...")

        if not box_ready and os.path.exists(box_file):
            box_ready = True
            with open(box_file, "r") as f:
                box_ph.download_button("✅ Download Box Scores", data=f.read(),
                    file_name=os.path.basename(box_file), mime="application/json",
                    use_container_width=True)
            acc_ph.warning("⏳ Accumulating stats...")

        if not acc_ready and os.path.exists(acc_file):
            acc_ready = True
            with open(acc_file, "r") as f:
                acc_ph.download_button("✅ Download Accumulated Stats", data=f.read(),
                    file_name=os.path.basename(acc_file), mime="application/json",
                    use_container_width=True)

    process.wait()

    # Mark all phases complete
    prog_state["phase"] = 5
    render_progress(progress_ph, prog_state)

    if process.returncode == 0:
        st.success("🎉 Scraping completed successfully! Download your files above.")
    else:
        st.error("Scraping failed. Check the logs above for details.")
