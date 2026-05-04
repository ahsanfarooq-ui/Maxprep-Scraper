"""
HS Basketball — Box Score Scraper
==================================
Input:  [state]_data_gaps.json    (produced by texas_data_gap_finder.py)
Output: [state]_box_scores.json

Scrapes every scheduled game box score for all teams classified as
'full' or 'partial' in the input file.

Uses the same boxscore.aspx?contestid={guid}&ssid={ssid} method as
texas_data_gap_finder.py — a plain HTTP request, no JS rendering needed.

Games are deduplicated by contest GUID so that if two Texas teams play
each other, the game is only fetched once.

Output is compatible with Accumulation_data.py.
"""

import os
import re
import sys
import json
import time
import base64
import struct
import argparse
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from bs4 import BeautifulSoup

# ── Config ────────────────────────────────────────────────────────────────────

INPUT_FILE  = "texas_data_gaps.json"
OUTPUT_FILE = "texas_box_scores.json"
DELAY       = 0.6          # seconds between HTTP requests

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

# ── Session with automatic retry on connection drops ─────────────────────────

def _make_session():
    s = requests.Session()
    retry = Retry(
        total=4,
        backoff_factor=2.0,          # waits 2 s, 4 s, 8 s, 16 s
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    s.mount("https://", adapter)
    s.mount("http://",  adapter)
    s.headers.update(HEADERS)
    return s

SESSION = _make_session()

# ── Schedule helpers (same logic as texas_data_gap_finder.py) ─────────────────

def get_build_id():
    r = SESSION.get("https://www.maxpreps.com", timeout=20)
    r.raise_for_status()
    m = re.search(r"/_next/static/([a-zA-Z0-9_-]+)/_buildManifest\.js", r.text)
    if not m:
        raise RuntimeError("MaxPreps build ID not found")
    return m.group(1)


def team_url_to_path(team_url):
    return re.sub(r"https://www\.maxpreps\.com/", "", team_url).rstrip("/")


def decode_contest_guid(c_param):
    """Base64url-encoded contest ID → GUID string."""
    try:
        s = c_param.replace("-", "+").replace("_", "/")
        pad = (4 - len(s) % 4) % 4
        b = base64.b64decode(s + "=" * pad)
        if len(b) != 16:
            return None
        p1 = struct.unpack_from("<I", b, 0)[0]
        p2 = struct.unpack_from("<H", b, 4)[0]
        p3 = struct.unpack_from("<H", b, 6)[0]
        p4 = b[8:16].hex()
        return f"{p1:08x}-{p2:04x}-{p3:04x}-{p4[:4]}-{p4[4:]}"
    except Exception:
        return None


def fetch_schedule(build_id, team_path):
    """Returns contest list, {"_expired": True}, or None on error."""
    url = f"https://www.maxpreps.com/_next/data/{build_id}/{team_path}/schedule.json"
    time.sleep(DELAY)
    try:
        r = SESSION.get(url, timeout=25)
        if r.status_code == 404:
            return {"_expired": True}
        if r.status_code != 200:
            return None
        data = r.json()
        return (
            data.get("pageProps", {}).get("initialPageProps", {}).get("contests")
            or data.get("pageProps", {}).get("contests")
            or []
        )
    except Exception as e:
        print(f"    [WARN] schedule fetch failed for {team_path}: {e}")
        return None


def get_game_entries(contests):
    """
    Extract (game_url, contest_guid, ssid) for every scheduled game.
    The ssid at index 14 is the team's own season ID — consistent across
    all games in the team's schedule.
    """
    NULL = "00000000-0000-0000-0000-000000000000"
    team_ssid = next(
        (c[14] for c in contests
         if isinstance(c, list) and len(c) > 14 and c[14] and c[14] != NULL),
        None,
    )
    entries = []
    for c in contests:
        if not (isinstance(c, list) and len(c) > 18):
            continue
        url = c[18]
        if not (isinstance(url, str) and url.startswith("https://")):
            continue
        m = re.search(r"[?&]c=([A-Za-z0-9_-]+)", url)
        guid = decode_contest_guid(m.group(1)) if m else None
        ssid = (c[14] if len(c) > 14 and c[14] and c[14] != NULL else team_ssid)
        entries.append((url, guid, ssid))
    return entries


# ── HTML parsing ──────────────────────────────────────────────────────────────

# Column-header → field-name mappings per stat category
# (percentage columns are deliberately omitted — they're recalculated downstream)

_SHOOTING_MAP = {
    "min":  "minutes_played",
    "pts":  "points",
    "fgm":  "fg_made",
    "fga":  "fg_attempts",
}
_DETAILED_MAP = {
    "3pm":  "3pt_made",
    "3pa":  "3pt_attempts",
    "ftm":  "ft_made",
    "fta":  "ft_attempts",
    "2fgm": "2pt_made",
    "2fga": "2pt_attempts",
}
_TOTALS_MAP = {
    "oreb": "offensive_rebounds",
    "dreb": "defensive_rebounds",
    "reb":  "rebounds",
    "ast":  "assists",
    "stl":  "steals",
    "blk":  "blocks",
    "to":   "turnovers",
    "pf":   "personal_fouls",
}
_MISC_MAP = {
    "chr":  "charges_taken",
    "defl": "deflections",
    "tf":   "technical_fouls",
}
_CAT_MAPS = {
    "shooting":          _SHOOTING_MAP,
    "detailed_shooting": _DETAILED_MAP,
    "totals":            _TOTALS_MAP,
    "misc":              _MISC_MAP,
}


def _safe_num(text):
    """Cell text → int/float, or None if blank/dash."""
    if not text or text in ("-", "—", "–"):
        return None
    try:
        f = float(text)
        return int(f) if f == int(f) else f
    except (ValueError, TypeError):
        return None


def _parse_athlete_cell(text):
    """
    'C. Urune-Williams(Jr)' or 'C. Urune-Williams (Jr)'
    → ('C. Urune-Williams', 'Jr')
    """
    m = re.match(r"^(.+?)\s*\((\w+)\)\s*$", text.strip())
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return text.strip(), ""


def _identify_category(headers):
    """Determine stat category type from table column headers."""
    hs = {h.lower().replace(" ", "").replace("%", "") for h in headers}
    if "chr" in hs or "defl" in hs:
        return "misc"
    if "oreb" in hs or "dreb" in hs:
        return "totals"
    if "3pm" in hs or "ftm" in hs or "2fgm" in hs:
        return "detailed_shooting"
    if "fgm" in hs or "fga" in hs:
        return "shooting"
    return None


def _parse_players(table, category):
    """
    Parse player rows from a stat table's <tbody> (Team Totals live in
    <tfoot> and are automatically excluded).

    Returns a list of player dicts with the fields expected by
    Accumulation_data.py for this category.
    """
    field_map = _CAT_MAPS.get(category, {})
    headers = [th.get_text(strip=True) for th in table.select("thead th, thead td")]

    players = []
    for tr in table.select("tbody tr"):
        cells = [td.get_text(strip=True) for td in tr.find_all(["td", "th"])]
        if len(cells) < 2:
            continue

        # Column 1 is always the athlete name cell
        name_text = cells[1]
        if not name_text or "team totals" in name_text.lower():
            continue

        player_name, player_class = _parse_athlete_cell(name_text)
        if not player_name:
            continue

        player = {"player_name": player_name, "class": player_class}

        for col_idx, header in enumerate(headers):
            if col_idx <= 1:
                continue             # skip # and Athlete Name columns
            if col_idx >= len(cells):
                break
            key = header.lower().replace(" ", "").replace("%", "")
            field = field_map.get(key)
            if field:
                player[field] = _safe_num(cells[col_idx])

        players.append(player)

    return players


def parse_game_page(soup, our_team_name):
    """
    Parse all div.stat-category elements on a rendered game page.

    Each div has:
      span.school  → team name
      h4           → category label (Shooting / Totals / Misc Totals; absent
                      for Detailed Shooting — identified from column headers)
      table tbody  → player rows  (Team Totals are in tfoot, safely excluded)

    Returns a dict:
    {
      "team_name":     <our team name as found on the page>,
      "opp_name":      <opponent team name, or "" if not found>,
      "shooting":          {"team": {"players":[...]}, "opponent": {"players":[...]}},
      "detailed_shooting": {...},
      "totals":            {...},
      "misc":              {...},
    }
    or None if no stat sections are found.
    """
    stat_divs = soup.select("div.stat-category")
    if not stat_divs:
        return None

    # Group divs by team name
    team_data = {}   # team_name → {category → [players]}

    for div in stat_divs:
        # Skip divs that only contain a "not entered" message
        if div.select_one("div.no-data") and not div.find("table"):
            continue
        table = div.find("table")
        if not table:
            continue

        # Team name from span.school
        school_el = div.select_one("span.school")
        team_name = school_el.get_text(strip=True) if school_el else ""
        if not team_name:
            continue

        # Category from column headers
        headers = [th.get_text(strip=True)
                   for th in table.select("thead th, thead td")]
        category = _identify_category(headers)
        if not category:
            continue

        players = _parse_players(table, category)
        if team_name not in team_data:
            team_data[team_name] = {}
        team_data[team_name][category] = players

    if not team_data:
        return None

    # Identify our team vs opponent by fuzzy name match
    our_norm = our_team_name.lower().strip()

    def _matches(name):
        n = name.lower().strip()
        return n == our_norm or our_norm in n or n in our_norm

    team_key = next((n for n in team_data if _matches(n)), None)
    if not team_key:
        team_key = next(iter(team_data))   # fallback: first team found

    opp_key = next((n for n in team_data if n != team_key), None)

    result = {
        "team_name": team_key,
        "opp_name":  opp_key or "",
    }
    for cat in ("shooting", "detailed_shooting", "totals", "misc"):
        result[cat] = {
            "team":     {"players": team_data.get(team_key, {}).get(cat, [])},
            "opponent": {"players": team_data.get(opp_key,  {}).get(cat, [])
                         if opp_key else []},
        }
    return result


def _scoreline_teams(soup):
    """
    Extract (home_team_name, away_team_name) from the score table at the
    top of the page — used to populate opponent name when stat-category
    divs only show one team.
    """
    for table in soup.find_all("table"):
        rows = table.select("tbody tr")
        if len(rows) >= 2:
            cells0 = [td.get_text(strip=True) for td in rows[0].find_all(["td","th"])]
            cells1 = [td.get_text(strip=True) for td in rows[1].find_all(["td","th"])]
            if cells0 and cells1 and cells0[0] and cells1[0]:
                name0, name1 = cells0[0], cells1[0]
                # Quick sanity: team names are text, scores are digits
                if not name0.isdigit() and not name1.isdigit():
                    return name0, name1
    return "", ""


# ── Game scraping ─────────────────────────────────────────────────────────────

def scrape_game(game_url, guid, ssid, our_team_name, team_id):
    """
    Fetch and parse one game's box score.

    Returns a record dict compatible with Accumulation_data.py, or None
    if the page could not be fetched or has no stat-category sections.
    """
    time.sleep(DELAY)

    url = (
        f"https://www.maxpreps.com/local/stats/boxscore.aspx"
        f"?contestid={guid}&ssid={ssid}"
        if guid and ssid else game_url
    )

    try:
        r = SESSION.get(url, headers=HTML_HEADERS, timeout=25, allow_redirects=True)
        if r.status_code == 404:
            return {"_404": True}
        if r.status_code != 200:
            return None
        soup = BeautifulSoup(r.text, "html.parser")
    except Exception as e:
        # Re-raise connection/timeout errors so the caller can handle retries
        if isinstance(e, (requests.exceptions.ConnectionError, requests.exceptions.Timeout)):
            raise e
        print(f"    [WARN] fetch failed for {game_url[-55:]}: {e}")
        return None

    page = parse_game_page(soup, our_team_name)
    if not page:
        return None      # game has no stat-category content

    # Opponent name: prefer what the stat parser found; fall back to scoreline
    opp_name = page["opp_name"]
    if not opp_name:
        t1, t2 = _scoreline_teams(soup)
        our_norm = our_team_name.lower().strip()
        if t1.lower().strip() == our_norm or our_norm in t1.lower():
            opp_name = t2
        else:
            opp_name = t1

    # Game date from the final (redirected) URL
    date_m = re.search(r"/(\d{1,2}-\d{1,2}-\d{4})/", r.url)
    game_date = date_m.group(1) if date_m else ""

    # Stable opponent ID: normalised slug of opponent name
    opp_id = re.sub(r"[^a-z0-9]+", "-", opp_name.lower()).strip("-")

    return {
        "contest_id":        guid,
        "game_url":          r.url,
        "game_date":         game_date,
        "is_deleted":        False,
        "team":     {"team_id": team_id,  "team_name": our_team_name},
        "opponent": {"team_id": opp_id,   "team_name": opp_name},
        "shooting":          page["shooting"],
        "detailed_shooting": page["detailed_shooting"],
        "totals":            page["totals"],
        "misc":              page["misc"],
    }


# ── Output helpers ────────────────────────────────────────────────────────────

def _save(games, errors, total_teams, output_file, processed_teams=None):
    out = {
        "meta": {
            "totalGames":  len(games),
            "totalErrors": len(errors),
            "totalTeams":  total_teams,
            "processedTeamsCount": len(processed_teams) if processed_teams else 0,
            "processedTeams": list(processed_teams) if processed_teams else [],
            "errors":      errors,
            "last_updated": time.strftime("%Y-%m-%d %H:%M:%S")
        },
        "games": games,
    }
    with open(output_file, "w") as f:
        json.dump(out, f, indent=2)


# ── Core scraping logic (callable from gap finder or standalone) ──────────────

def run(input_file=None, output_file=None, sport="boys", season="2025-2026"):
    """
    Scrape all full/partial teams from a gaps JSON file.
    Can be called programmatically or invoked via main().
    """
    if input_file is None:
        input_file = INPUT_FILE
    if output_file is None:
        output_file = OUTPUT_FILE

    # ── Load input ───────────────────────────────────────────────────────────
    with open(input_file) as f:
        gaps = json.load(f)

    teams = (
        gaps.get("teamsFullBoxScores", []) +
        gaps.get("teamsPartialBoxScores", [])
    )
    total_teams = len(teams)
    print(f"Teams to process : {total_teams}  (full + partial)\n")

    build_id = get_build_id()
    print(f"Build ID : {build_id}\n")
    print(f"Output   : {output_file}\n")
    print("─" * 60)

    all_games  = []
    errors     = []
    processed_teams = set()

    # ── Resume Logic ─────────────────────────────────────────────────────────
    if os.path.exists(output_file):
        try:
            with open(output_file, "r") as f:
                existing_data = json.load(f)
                all_games = existing_data.get("games", [])
                errors = existing_data.get("meta", {}).get("errors", [])
                processed_teams = set(existing_data.get("meta", {}).get("processedTeams", []))
                print(f"Resuming: {len(processed_teams)} teams already processed, {len(all_games)} games loaded.")
        except Exception as e:
            print(f"Could not load existing output file for resumption: {e}")

    for t_idx, team in enumerate(teams, 1):
        team_name = team["teamName"]
        team_url  = team["teamUrl"]
        team_id   = team_url_to_path(team_url)

        if team_id in processed_teams:
            # Skip already processed teams
            continue

        print(f"\nProcessing team {t_idx}/{total_teams}: {team_name}")

        # ── Schedule (with retry logic) ──────────────────────────────────────
        contests = None
        while contests is None:
            try:
                path = team_url_to_path(team_url)
                contests = fetch_schedule(build_id, path)

                if isinstance(contests, dict) and contests.get("_expired"):
                    print(f"  [INFO] Build ID expired or 404. Refreshing build ID...")
                    build_id = get_build_id()
                    print(f"  [INFO] New Build ID: {build_id}")
                    contests = None # force retry with new build_id
                    continue
                
                if contests is None:
                    # Some other non-404 error occurred in fetch_schedule
                    print(f"  [WARN] Schedule fetch returned None for {team_name}. Retrying in 5s...")
                    time.sleep(5)
                    continue

            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
                print(f"  [ERROR] Network issue: {e}. Retrying in 30s...")
                time.sleep(30)
                continue
            except Exception as e:
                print(f"  [ERROR] Unexpected error fetching schedule: {e}. Retrying in 10s...")
                time.sleep(10)
                continue

        entries = get_game_entries(contests)
        team_games_count = 0

        for game_url, guid, ssid in entries:
            if not guid:
                continue
            
            for _attempt in range(3):
                try:
                    record = scrape_game(game_url, guid, ssid, team_name, team_id)
                    if isinstance(record, dict) and record.get("_404"):
                        # boxscore.aspx 404 means the game has no page — skip it
                        break
                    if record:
                        all_games.append(record)
                        team_games_count += 1
                    break
                except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
                    print(f"    [ERROR] Network issue scraping game: {e}. Retrying in 30s...")
                    time.sleep(30)
                except Exception as e:
                    print(f"    [ERROR] Error scraping game: {e}.")
                    break

        processed_teams.add(team_id)
        print(f"    [DONE] Added {team_games_count} games for {team_name}")

        # ── Progress + periodic save ──────────────────────────────────────────
        _progress(t_idx, total_teams, all_games, errors)
        
        # Save after every team for "runtime" saving
        _save(all_games, errors, total_teams, output_file, processed_teams)

    # ── Final save ────────────────────────────────────────────────────────────
    _save(all_games, errors, total_teams, output_file)

    unique_guids = len({g["contest_id"] for g in all_games})
    print("\n" + "=" * 60)
    print(f"  Total game records  : {len(all_games)}")
    print(f"  Unique contest IDs  : {unique_guids}")
    print(f"  Teams with errors   : {len(errors)}")
    print(f"  Saved → {output_file}")
    print("=" * 60)

    # Quick sample
    if all_games:
        print("\nSample games (first 3):")
        for g in all_games[:3]:
            t_players  = len(g["shooting"]["team"]["players"])
            op_players = len(g["shooting"]["opponent"]["players"])
            print(f"  {g['team']['team_name']} vs {g['opponent']['team_name']}"
                  f"  ({g['game_date']}) — "
                  f"team {t_players} players, opp {op_players} players")

    # ── Accumulation ─────────────────────────────────────────────────────────
    accumulated_file = output_file.replace("box_scores", "accumulated_stats")
    print(f"\n{'─' * 60}")
    print(f"Running data accumulation → {accumulated_file}")
    print("─" * 60)
    try:
        from Accumulation_data import process_stats
        process_stats(input_file=output_file, output_file=accumulated_file)
    except Exception as e:
        print(f"  [ERROR] Accumulation failed: {e}")


def _progress(t_idx, total, games, errors):
    pct = t_idx / total * 100
    bar_len = 20
    filled_len = int(bar_len * t_idx // total)
    bar = "█" * filled_len + "-" * (bar_len - filled_len)
    
    print(f"  Progress: |{bar}| {pct:5.1f}%  |  "
          f"Total Games: {len(games):>5}  |  Errors: {len(errors)}")


# ── CLI entry point ───────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="HS Basketball Box Score Scraper"
    )
    parser.add_argument(
        "--state", default="TX", help="State code (default: TX)"
    )
    parser.add_argument(
        "--sport", default="boys", choices=["boys", "girls"],
        help="boys (default) or girls",
    )
    parser.add_argument(
        "--season", default="2025-2026",
        help="Season (e.g., 2025-2026)",
    )
    parser.add_argument(
        "--output", default=None, help="Explicit output file (optional)"
    )
    args = parser.parse_args()

    state_lower = args.state.lower()
    season_fn = args.season.replace("-", "_")
    
    # Input: tx_data_gaps_boys_2025_2026.json
    input_file = f"{state_lower}_data_gaps_{args.sport}_{season_fn}.json"
    if not os.path.exists(input_file):
        # Fallback to the dash version or old name
        alt_name = f"{state_lower}_data_gaps_{args.sport}_{args.season}.json"
        if os.path.exists(alt_name):
            input_file = alt_name
        else:
            fallback = f"{state_lower}_data_gaps.json"
            if os.path.exists(fallback):
                input_file = fallback
            else:
                print(f"Error: Input file {input_file} not found.")
                sys.exit(1)

    out = args.output
    if out is None:
        # Output: tx_box_scores_boys_2025_2026.json
        out = input_file.replace("data_gaps", "box_scores")

    run(input_file=input_file, output_file=out, sport=args.sport, season=args.season)


if __name__ == "__main__":
    main()
