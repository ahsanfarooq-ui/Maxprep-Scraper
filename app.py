
"""
HS Basketball — Box Score Gap Finder (High Speed Parallel Version)
==================================================================
Merges the multi-threaded performance of v3 with robust runtime saving 
and resume logic.

Expected runtime for 1800+ teams: ~30–45 minutes (vs 7 hours).


HOW TO USE:::::===========================================================
python app.py --state TX --sport girls --season 2025-2026
python app.py --state NM --sport boys --season 2025-2026
==========================================================================
"""

import os
import re
import sys
import json
import time
import base64
import struct
import threading
import argparse
import requests
from bs4 import BeautifulSoup
from urllib.parse import quote_plus
from concurrent.futures import ThreadPoolExecutor, as_completed

DATA_DIR = os.environ.get("DATA_DIR", ".")

# ─── State lookup ─────────────────────────────────────────────────────────────

STATE_NAMES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas",
    "CA": "California", "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware",
    "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho",
    "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas",
    "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland",
    "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi",
    "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada",
    "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York",
    "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma",
    "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina",
    "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas", "UT": "Utah",
    "VT": "Vermont", "VA": "Virginia", "WA": "Washington", "WV": "West Virginia",
    "WI": "Wisconsin", "WY": "Wyoming", "DC": "District of Columbia",
}

# ─── Config ───────────────────────────────────────────────────────────────────

INPUT_FILE    = "boys_basketball_all_states.json"
DELAY         = 0.3    # base delay (per thread)
SCHED_WORKERS = 20     # Parallel schedule fetches
GAME_WORKERS  = 50     # Parallel game checks

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept":          "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer":         "https://www.maxpreps.com/",
}

HTML_HEADERS = {**HEADERS, "Accept": "text/html,application/xhtml+xml,*/*"}

# ─── Thread-local HTTP sessions ───────────────────────────────────────────────

_tls = threading.local()

def _session(json_mode=True):
    key = "jsess" if json_mode else "hsess"
    if not hasattr(_tls, key):
        s = requests.Session()
        s.headers.update(HEADERS if json_mode else HTML_HEADERS)
        setattr(_tls, key, s)
    return getattr(_tls, key)

# ─── Build-ID management (Thread Safe) ────────────────────────────────────────

_bid_lock    = threading.Lock()
_bid_value   = None
_bid_version = 0

def _fetch_raw_bid():
    r = requests.get("https://www.maxpreps.com", headers=HEADERS, timeout=20)
    r.raise_for_status()
    m = re.search(r"/_next/static/([a-zA-Z0-9_-]+)/_buildManifest\.js", r.text)
    if not m:
        raise RuntimeError("Build ID not found")
    return m.group(1)

def get_build_id():
    global _bid_value, _bid_version
    with _bid_lock:
        if _bid_value is None:
            _bid_value = _fetch_raw_bid()
        return _bid_value, _bid_version

def refresh_build_id(old_version):
    global _bid_value, _bid_version
    with _bid_lock:
        if _bid_version == old_version:
            _bid_value    = _fetch_raw_bid()
            _bid_version += 1
        return _bid_value, _bid_version

# ─── Helpers ──────────────────────────────────────────────────────────────────

def team_url_to_path(team_url):
    return re.sub(r"https://www\.maxpreps\.com/", "", team_url).rstrip("/")

def clean_team_name(name):
    """Fix known URL-encoding corruptions in team names from the master list."""
    return name.replace("Aandm", "A&M").replace("aandm", "a&m")

def decode_contest_guid(c_param):
    try:
        s = c_param.replace("-", "+").replace("_", "/")
        pad = (4 - len(s) % 4) % 4
        b = base64.b64decode(s + "=" * pad)
        if len(b) != 16: return None
        p1 = struct.unpack_from("<I", b, 0)[0]
        p2 = struct.unpack_from("<H", b, 4)[0]
        p3 = struct.unpack_from("<H", b, 6)[0]
        p4 = b[8:16].hex()
        return f"{p1:08x}-{p2:04x}-{p3:04x}-{p4[:4]}-{p4[4:]}"
    except Exception: return None

def _raw_fetch_schedule(bid, team_path):
    url = f"https://www.maxpreps.com/_next/data/{bid}/{team_path}/schedule.json"
    time.sleep(DELAY)
    try:
        r = _session().get(url, timeout=20)
        if r.status_code == 404: return {"_expired": True}
        if r.status_code != 200: return None
        data = r.json()
        return (data.get("pageProps", {}).get("initialPageProps", {}).get("contests")
                or data.get("pageProps", {}).get("contests") or [])
    except Exception: return None

def get_game_entries(contests):
    NULL_GUID = "00000000-0000-0000-0000-000000000000"
    team_ssid = None
    for c in contests:
        if isinstance(c, list) and len(c) > 14:
            if c[14] and c[14] != NULL_GUID:
                team_ssid = c[14]
                break
    entries = []
    for c in contests:
        if not (isinstance(c, list) and len(c) > 18): continue
        game_url = c[18]
        if not (isinstance(game_url, str) and game_url.startswith("https://")): continue
        m = re.search(r"[?&]c=([A-Za-z0-9_-]+)", game_url)
        guid = decode_contest_guid(m.group(1)) if m else None
        ssid = c[14] if len(c) > 14 and c[14] and c[14] != NULL_GUID else team_ssid
        entries.append((game_url, guid, ssid))
    return entries

def _check_soup(soup, team_name):
    stat_sections = soup.select("div.stat-category")
    no_data_msgs  = [el.get_text(strip=True).lower() for el in soup.select("div.no-data")]
    norm = team_name.lower().strip()
    team_not_entered = any(norm in msg and "not entered" in msg for msg in no_data_msgs)
    return bool(stat_sections) and not team_not_entered

# ─── Workers ──────────────────────────────────────────────────────────────────

def fetch_sched_worker(team):
    path = team_url_to_path(team["teamUrl"])
    bid, version = get_build_id()
    for _ in range(2):
        contests = _raw_fetch_schedule(bid, path)
        if contests is None: return team, None
        if isinstance(contests, dict) and contests.get("_expired"):
            bid, version = refresh_build_id(version)
            continue
        return team, get_game_entries(contests)
    return team, None

def check_game_worker(game_url, guid, ssid, team_name):
    time.sleep(DELAY)
    url = (f"https://www.maxpreps.com/local/stats/boxscore.aspx?contestid={guid}&ssid={ssid}"
           if guid and ssid else game_url)
    try:
        r = _session(json_mode=False).get(url, timeout=20, allow_redirects=True)
        if r.status_code != 200: return None
        return _check_soup(BeautifulSoup(r.text, "html.parser"), team_name)
    except Exception: return None

# ─── Save / Output ────────────────────────────────────────────────────────────

def scorestream_url(team_name, state_name):
    return f"https://scorestream.com/search?q={quote_plus(team_name + ' ' + state_name + ' high school basketball')}"

def google_search_url(team_name, city, state_name):
    q = f'"{team_name}" {city} {state_name} high school basketball schedule stats'
    return f"https://www.google.com/search?q={quote_plus(q)}"

def _save_gaps(output_file, state_name, state_code, total_count, full_data, partial_data, no_data, errors, processed_teams, sport="boys", season="2025-2026"):
    total_games_checked = sum(t["gamesChecked"] for t in full_data + partial_data + no_data)
    output = {
        "meta": {
            "state": state_name, "stateCode": state_code,
            "sport": f"{sport.title()} Basketball", "season": season,
            "totalTeams": total_count, "processedTeamsCount": len(processed_teams),
            "processedTeams": list(processed_teams), "totalGamesChecked": total_games_checked,
            "teamsFullBoxScores": len(full_data), "teamsPartialBoxScores": len(partial_data),
            "teamsNoBoxScores": len(no_data), "errors_count": len(errors),
            "last_updated": time.strftime("%Y-%m-%d %H:%M:%S"),
        },
        "teamsFullBoxScores": sorted(full_data, key=lambda x: x["teamName"]),
        "teamsPartialBoxScores": sorted(partial_data, key=lambda x: x["teamName"]),
        "teamsNoBoxScores": sorted(no_data, key=lambda x: x["teamName"]),
        "errors": errors,
    }
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Parallel HS Basketball Box Score Gap Finder")
    parser.add_argument("--state", default=os.environ.get("STATE", "TX"), help="State code (default: TX)")
    parser.add_argument("--sport", default=os.environ.get("SPORT", "boys"), choices=["boys", "girls"], help="boys (default) or girls")
    parser.add_argument("--season", default=os.environ.get("SEASON", "2025-2026"), help="Season (e.g., 2025-2026 or 25-26)")
    args = parser.parse_args()

    state_code  = args.state.upper()
    state_lower = state_code.lower()
    state_name  = STATE_NAMES.get(state_code, state_code)
    
    # Normalise season for input file lookup (e.g. 25-26)
    short_season = args.season

    sport_label = args.sport.lower()
    # Input from state_teams_counter: boys_basketball_all_states_25-26.json
    APP_DIR = os.path.dirname(os.path.abspath(__file__))

    # Look for input file in app directory (bundled with repo)
    input_file = os.path.join(APP_DIR, f"{sport_label}_basketball_all_states_{short_season}.json")

    if not os.path.exists(input_file):
        # Try without season suffix (e.g. boys_basketball_all_states.json)
        fallback = os.path.join(APP_DIR, f"{sport_label}_basketball_all_states.json")
        if os.path.exists(fallback):
            input_file = fallback
        else:
            print(f"Team list missing. Running state_teams_counter...")
            state_teams_counter.run(sport=args.sport, season=short_season)
            generated = os.path.join(DATA_DIR, f"{sport_label}_basketball_all_states_{short_season}.json")
            if os.path.exists(generated):
                input_file = generated
            else:
                print(f"Error: Input file not found.")
                sys.exit(1)

    # Output: tx_data_gaps_boys_2025_2026.json
    season_fn = args.season.replace("-", "_")
    output_file = os.path.join(DATA_DIR, f"{state_lower}_data_gaps_{sport_label}_{season_fn}.json")
    
    with open(input_file, encoding="utf-8") as f: data = json.load(f)
    if state_code not in data.get("byState", {}):
        print(f"Error: State {state_code} not found."); sys.exit(1)

    state_regions = data["byState"][state_code]["regions"]
    all_teams = [{"teamName": clean_team_name(t["teamName"]), "teamUrl": t["teamUrl"], "region": r}
                 for r, d in state_regions.items() for t in d["teams"]]
    total = len(all_teams)

    # Resume Logic
    full_data, partial_data, no_data, errors, processed_teams = [], [], [], [], set()
    if os.path.exists(output_file):
        try:
            with open(output_file, "r", encoding="utf-8") as f:
                existing = json.load(f)
                full_data = existing.get("teamsFullBoxScores", [])
                partial_data = existing.get("teamsPartialBoxScores", [])
                no_data = existing.get("teamsNoBoxScores", [])
                errors = existing.get("errors", [])
                processed_teams = set(existing.get("meta", {}).get("processedTeams", []))
                print(f"Resuming: {len(processed_teams)} teams already processed.")
        except Exception: pass

    # Filter teams for Phase 1
    teams_to_process = [t for t in all_teams if team_url_to_path(t["teamUrl"]) not in processed_teams]
    if teams_to_process:
        # Phase 1: Schedules
        print(f"Phase 1: Fetching {len(teams_to_process)} schedules ({SCHED_WORKERS} workers)...")
        sched_results = {}
        with ThreadPoolExecutor(max_workers=SCHED_WORKERS) as pool:
            futures = {pool.submit(fetch_sched_worker, t): t for t in teams_to_process}
            for i, fut in enumerate(as_completed(futures), 1):
                team, entries = fut.result()
                sched_results[team["teamName"]] = (team, entries)
                if i % 100 == 0 or i == len(teams_to_process):
                    print(f"  Schedules: {i}/{len(teams_to_process)} done")

        # Phase 2: Game checks
        print(f"Phase 2: Checking games in parallel ({GAME_WORKERS} workers)...")
        agg_lock = threading.Lock()
        game_jobs = []
        for tname, (team, entries) in sched_results.items():
            if entries is None:
                errors.append({"teamName": team["teamName"], "teamUrl": team["teamUrl"], "region": team["region"]})
                processed_teams.add(team_url_to_path(team["teamUrl"]))
            elif not entries:
                city_m = re.search(rf"/{state_lower}/([^/]+)/", team["teamUrl"])
                city = city_m.group(1).replace("-", " ").title() if city_m else state_name
                no_data.append({"teamName": team["teamName"], "teamUrl": team["teamUrl"], "region": team["region"], 
                                "gamesChecked": 0, "gamesWithStats": 0, "gamesMissing": 0,
                                "alternativeSources": {"scoreStream": scorestream_url(team["teamName"], state_name), 
                                                     "googleSearch": google_search_url(team["teamName"], city, state_name)}})
                processed_teams.add(team_url_to_path(team["teamUrl"]))
            else:
                game_jobs.append({'team': team, 'entries': entries})

        def process_team_games(job):
            team, entries = job['team'], job['entries']
            games_checked, games_with_stats = 0, 0
            for url, guid, ssid in entries:
                res = check_game_worker(url, guid, ssid, team["teamName"])
                if res is not None:
                    games_checked += 1
                    if res: games_with_stats += 1
            
            entry = {"teamName": team["teamName"], "teamUrl": team["teamUrl"], "region": team["region"],
                     "gamesChecked": games_checked, "gamesWithStats": games_with_stats, "gamesMissing": games_checked - games_with_stats}
            
            with agg_lock:
                if games_checked == 0: no_data.append(entry)
                elif games_with_stats == games_checked: full_data.append(entry)
                elif games_with_stats > 0: partial_data.append(entry)
                else: no_data.append(entry)
                processed_teams.add(team_url_to_path(team["teamUrl"]))
                
                # Print frequent progress
                tdone = len(processed_teams)
                pct = tdone / total * 100
                print(f"  [{tdone:>4}/{total}] {pct:5.1f}% | Full: {len(full_data):>4} | Part: {len(partial_data):>4} | {team['teamName']}")
                
                if tdone % 10 == 0 or tdone == total:
                    _save_gaps(output_file, state_name, state_code, total, full_data, partial_data, no_data, errors, processed_teams, args.sport, args.season)

        with ThreadPoolExecutor(max_workers=GAME_WORKERS) as pool:
            list(pool.map(process_team_games, game_jobs))

        _save_gaps(output_file, state_name, state_code, total, full_data, partial_data, no_data, errors, processed_teams, args.sport, args.season)
        print(f"\nGap analysis complete for {total} teams.")
    else:
        print(f"Gap analysis already complete for {total} teams. Proceeding to next steps...")

    if total == 0:
        print(f"\n[WARNING] No teams found for {state_name} ({args.sport}) in season {args.season}.")
        print("This usually means MaxPreps hasn't posted the leagues for this season yet.")
        sys.exit(0)

    print(f"\nSaved {total} teams to {output_file}. Starting scraper...")
    
    # Auto-run Scraper
    try:
        from scrape_box_scores import run as scrape_run
        box_scores_out = output_file.replace("data_gaps", "box_scores")
        scrape_run(input_file=output_file, output_file=box_scores_out, sport=args.sport, season=args.season)
    except Exception as e: print(f"Scraper failed: {e}")

if __name__ == "__main__":
    main()
