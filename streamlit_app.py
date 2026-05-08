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

SEASONS = ["2025-2026", "2024-2025", "2023-2024", "2022-2023"]

st.set_page_config(page_title="MaxPreps Basketball Scraper", page_icon="🏀", layout="wide")

st.title("🏀 MaxPreps Basketball Scraper")
st.markdown("Select your options and click **Start Scraping** to begin.")
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
    season = st.selectbox("Season", options=SEASONS)

st.divider()

if st.button("Start Scraping", type="primary", use_container_width=True):

    state_name = STATE_NAMES[state_code]
    sport_label = "Boys" if sport == "boys" else "Girls"
    st.info(f"Scraping **{state_name}** | **{sport_label} Basketball** | **{season}**  \nThis may take 30–45 minutes depending on the state size.")

    log_placeholder = st.empty()
    logs = []

    env = os.environ.copy()
    env["DATA_DIR"] = OUTPUT_DIR

    process = subprocess.Popen(
        [sys.executable, "app.py",
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
            logs.append(line)
            log_placeholder.text_area("Live Logs", value="\n".join(logs[-60:]), height=400)

    process.wait()

    if process.returncode == 0:
        st.success("Scraping completed successfully!")
        st.divider()
        st.subheader("Download Output Files")

        season_fn = season.replace("-", "_")
        state_lower = state_code.lower()

        files = {
            f"{state_lower}_accumulated_stats_{sport}_{season_fn}.json": "Accumulated Stats (Final Output)",
            f"{state_lower}_box_scores_{sport}_{season_fn}.json": "Box Scores",
            f"{state_lower}_data_gaps_{sport}_{season_fn}.json": "Data Gaps Analysis",
        }

        cols = st.columns(3)
        for col, (filename, label) in zip(cols, files.items()):
            filepath = os.path.join(OUTPUT_DIR, filename)
            if os.path.exists(filepath):
                with open(filepath, "r") as f:
                    col.download_button(
                        label=f"Download {label}",
                        data=f.read(),
                        file_name=filename,
                        mime="application/json",
                        use_container_width=True,
                    )
    else:
        st.error("Scraping failed. Check the logs above for details.")
