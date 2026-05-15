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
import threading
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from bs4 import BeautifulSoup
from concurrent.futures import ThreadPoolExecutor, as_completed

# ── Config ────────────────────────────────────────────────────────────────────

INPUT_FILE   = "texas_data_gaps.json"
OUTPUT_FILE  = "texas_box_scores.json"
DELAY        = 0.6          # seconds between HTTP requests (per worker thread)
TEAM_WORKERS = 15           # parallel teams (each thread scrapes its team's games sequentially)

# When refresh_build_id returns the SAME bid (i.e. MaxPreps hasn't rolled yet),
# wait this long before checking again, up to BID_STABLE_MAX_RETRIES times.
BID_STABLE_WAIT_SEC   = 15 * 60   # 15 minutes
BID_STABLE_MAX_RETRIES = 10        # ~2.5 hours total before giving up on a team

# Timestamped print: every log line gets a "[YYYY-MM-DD HH:MM:SS]" prefix so the
# Streamlit log viewer and stdout show live timing.
_original_print = print
def print(*args, **kwargs):
    _original_print(time.strftime('[%Y-%m-%d %H:%M:%S]'), *args, **kwargs)

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

# Thread-local sessions: a fresh Session per thread avoids any contention on
# the connection pool and matches the pattern used by app.py.
_tls = threading.local()

def _get_session():
    if not hasattr(_tls, "session"):
        _tls.session = _make_session()
    return _tls.session

# ── Thread-safe build ID management ──────────────────────────────────────────
# Many threads can hit a stale build ID at the same time. Without locking they
# would all independently refetch, blasting MaxPreps with concurrent root-page
# requests. The lock + version pattern (mirrored from app.py) collapses those
# concurrent refresh attempts into a single fetch.

_bid_lock = threading.Lock()
_bid_value = None
_bid_version = 0

def _fetch_build_id_raw():
    """Fetch the build ID with retry. Caller must hold _bid_lock."""
    delays = [5, 10, 20, 40, 60]
    last_err = None
    for attempt, wait in enumerate(delays, 1):
        try:
            r = _get_session().get("https://www.maxpreps.com", timeout=30, headers=HTML_HEADERS)
            r.raise_for_status()
            m = re.search(r"/_next/static/([a-zA-Z0-9_-]+)/_buildManifest\.js", r.text)
            if m:
                return m.group(1)
            print(f"  [WARN] Build ID not found in page (attempt {attempt}/{len(delays)}). Waiting {wait}s…")
        except Exception as e:
            last_err = e
            print(f"  [WARN] Build ID fetch error: {e} (attempt {attempt}/{len(delays)}). Waiting {wait}s…")
        time.sleep(wait)
    raise RuntimeError(f"MaxPreps build ID not found after all retries: {last_err}")

def get_build_id():
    """Returns (build_id, version) atomically. Lazy-fetches on first call."""
    global _bid_value, _bid_version
    with _bid_lock:
        if _bid_value is None:
            _bid_value = _fetch_build_id_raw()
        return _bid_value, _bid_version

def refresh_build_id(old_version):
    """Refresh only if the cached version matches old_version. Collapses
    concurrent 404-driven refreshes from many threads into a single fetch."""
    global _bid_value, _bid_version
    with _bid_lock:
        if _bid_version == old_version:
            _bid_value = _fetch_build_id_raw()
            _bid_version += 1
        return _bid_value, _bid_version


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


def _short_season(season):
    """Normalise a season string for use as a URL path segment.

    '2024-2025' → '24-25'; '24-25' → '24-25'; None/empty → None.
    Used to fetch past-season schedule data — MaxPreps serves it at
    {team_path}/{YY-YY}/schedule.json, NOT {team_path}/schedule.json
    (which always returns the current season regardless of any flag we pass)."""
    if not season:
        return None
    m = re.match(r'^(?:20)?(\d{2})-(?:20)?(\d{2})$', season.strip())
    if m:
        return f"{m.group(1)}-{m.group(2)}"
    return season


def fetch_schedule(build_id, team_path, season_suffix=None):
    """Returns contest list, {"_expired": True}, or None on error.

    season_suffix (e.g. '24-25') is inserted between the team path and
    'schedule.json' so the past-season schedule is fetched instead of the
    current one. None means current season (existing behavior).
    """
    if season_suffix:
        url = f"https://www.maxpreps.com/_next/data/{build_id}/{team_path}/{season_suffix}/schedule.json"
    else:
        url = f"https://www.maxpreps.com/_next/data/{build_id}/{team_path}/schedule.json"
    time.sleep(DELAY)
    try:
        r = _get_session().get(url, timeout=25)
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
    except (requests.exceptions.ConnectionError, requests.exceptions.Timeout):
        # Re-raise so the worker's outer retry can decide whether to back off.
        raise
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
        # Our team's stats are not on this page — all found data belongs to the
        # opponent. Putting it in the team section would create ghost records
        # (opponent players accumulated under the wrong team ID).
        opp_key = next(iter(team_data)) if team_data else None
        result = {
            "team_name": our_team_name,
            "opp_name":  opp_key or "",
        }
        for cat in ("shooting", "detailed_shooting", "totals", "misc"):
            result[cat] = {
                "team":     {"players": []},
                "opponent": {"players": team_data.get(opp_key, {}).get(cat, [])
                             if opp_key else []},
            }
        return result

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
        r = _get_session().get(url, headers=HTML_HEADERS, timeout=25, allow_redirects=True)
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
    # Atomic write: avoid leaving a half-written file if interrupted mid-save.
    tmp = output_file + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)
    os.replace(tmp, output_file)


def _name_from_url(team_url, fallback=""):
    """Derive full team name (e.g. 'Avinger Indians') from the URL slug — see
    matching helper in app.py. Page parser needs the full name to match the
    team header on the box score."""
    m = re.match(r"https?://(?:www\.)?maxpreps\.com/([^/]+)/([^/]+)/([^/]+)/", team_url)
    if m:
        slug = m.group(3).replace("-", " ").title()
        if slug:
            return slug.replace("Aandm", "A&M").replace("aandm", "a&m")
    return fallback


# ── Core scraping logic (callable from gap finder or standalone) ──────────────

def _scrape_team(team, season_suffix=None):
    """Worker: scrape one team's full set of games. Returns
    (team_id, team_name, team_url, games_list, error_dict_or_None).

    season_suffix (e.g. '24-25') makes the schedule fetch target a past
    season. None = current season.

    All HTTP work happens here — no shared state is touched. Caller commits
    the returned games/error under a lock.
    """
    team_url = team["teamUrl"]
    team_id = team_url_to_path(team_url)
    # Derive team_name from URL slug rather than trusting the gaps file —
    # stale stored names (e.g. 'Avinger' instead of 'Avinger Indians') break
    # the box-score page parser's team-header match.
    team_name = _name_from_url(team_url, team.get("teamName", ""))
    path = team_url_to_path(team_url)

    # ── Schedule with bounded retries ────────────────────────────────────────
    contests = None
    bid_change_retries = 0     # 404s where refresh produced a NEW bid
    stable_bid_retries = 0     # 404s where refresh returned the SAME bid (wait 15 min, then retry)
    none_retries = 0
    net_retries = 0
    bid, bid_version = get_build_id()
    while contests is None:
        try:
            contests = fetch_schedule(bid, path, season_suffix=season_suffix)

            if isinstance(contests, dict) and contests.get("_expired"):
                # 404 on schedule.json. Try a fresh build_id.
                new_bid, new_bid_version = refresh_build_id(bid_version)
                if new_bid != bid:
                    # MaxPreps rolled the build id — retry with the new one immediately.
                    bid_change_retries += 1
                    if bid_change_retries > 3:
                        return team_id, team_name, team_url, [], {
                            "teamName": team_name, "teamUrl": team_url,
                            "stage": "schedule", "reason": "build_id_kept_rolling",
                        }
                    bid, bid_version = new_bid, new_bid_version
                    contests = None
                    continue
                # Same bid back. Per user-requested strategy: MaxPreps may not have
                # rolled the build id yet — wait 15 minutes then check again. Repeat
                # up to BID_STABLE_MAX_RETRIES times before skipping the team.
                stable_bid_retries += 1
                if stable_bid_retries > BID_STABLE_MAX_RETRIES:
                    return team_id, team_name, team_url, [], {
                        "teamName": team_name, "teamUrl": team_url,
                        "stage": "schedule",
                        "reason": f"build_id_stable_after_{BID_STABLE_MAX_RETRIES}x{BID_STABLE_WAIT_SEC//60}min_waits",
                    }
                print(f"  [{team_name}] schedule 404, bid={bid} unchanged. "
                      f"Waiting {BID_STABLE_WAIT_SEC // 60} min for build id to update "
                      f"({stable_bid_retries}/{BID_STABLE_MAX_RETRIES}).")
                time.sleep(BID_STABLE_WAIT_SEC)
                contests = None
                continue

            if contests is None:
                none_retries += 1
                if none_retries > 5:
                    return team_id, team_name, team_url, [], {
                        "teamName": team_name, "teamUrl": team_url,
                        "stage": "schedule", "reason": "fetch_returned_none",
                    }
                time.sleep(5)
                continue

        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
            net_retries += 1
            if net_retries > 5:
                return team_id, team_name, team_url, [], {
                    "teamName": team_name, "teamUrl": team_url,
                    "stage": "schedule", "error": str(e),
                }
            time.sleep(min(5 * net_retries, 30))
            continue
        except Exception as e:
            return team_id, team_name, team_url, [], {
                "teamName": team_name, "teamUrl": team_url,
                "stage": "schedule", "error": str(e),
            }

    # ── Games (sequential within a single team to cap per-team request rate) ──
    entries = get_game_entries(contests)
    team_games = []
    for game_url, guid, ssid in entries:
        if not guid:
            continue
        for _attempt in range(3):
            try:
                record = scrape_game(game_url, guid, ssid, team_name, team_id)
                if isinstance(record, dict) and record.get("_404"):
                    break
                if record:
                    team_games.append(record)
                break
            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout):
                time.sleep(min(10 * (_attempt + 1), 30))
            except Exception:
                break

    return team_id, team_name, team_url, team_games, None


def run(input_file=None, output_file=None, sport="boys", season="2025-2026", workers=None):
    """
    Scrape all full/partial teams from a gaps JSON file.
    Can be called programmatically or invoked via main().

    workers: parallel team count (default: TEAM_WORKERS = 15).
    """
    if input_file is None:
        input_file = INPUT_FILE
    if output_file is None:
        output_file = OUTPUT_FILE
    if workers is None or workers <= 0:
        workers = TEAM_WORKERS

    # ── Load input ───────────────────────────────────────────────────────────
    with open(input_file, encoding="utf-8") as f:
        gaps = json.load(f)

    # By default, scrape EVERY team — full, partial, AND teams the gap finder
    # classified as "no box scores". The gap-finder classification is just a
    # heuristic based on the presence of any stat-category divs on the page;
    # it can misclassify teams whose schedules added stats after the gap run,
    # so we re-check them too. Dedup by teamUrl in case the same team appears
    # in more than one bucket of the gaps file.
    full_b    = gaps.get("teamsFullBoxScores", [])
    partial_b = gaps.get("teamsPartialBoxScores", [])
    none_b    = gaps.get("teamsNoBoxScores", [])
    teams_by_url = {}
    for t in full_b + partial_b + none_b:
        url = t.get("teamUrl")
        if url and url not in teams_by_url:
            teams_by_url[url] = t
    teams = list(teams_by_url.values())
    total_teams = len(teams)
    # Normalise the season into the YY-YY URL segment ('2024-2025' → '24-25').
    # Without this the schedule fetch URL omits the season and MaxPreps falls
    # back to the current season — silently scraping wrong-season games.
    season_suffix = _short_season(season)
    print(f"Teams to process : {total_teams}  (full={len(full_b)} + partial={len(partial_b)} + no-data={len(none_b)})")
    print(f"Season           : {season} (URL suffix: {season_suffix or '(current)'})")
    print(f"Workers          : {workers}\n")

    # Warm the build ID cache before fanning out so all threads share one fetch.
    bid, _ = get_build_id()
    print(f"Build ID : {bid}\n")
    print(f"Output   : {output_file}\n")
    print("─" * 60)

    all_games  = []
    errors     = []
    processed_teams = set()

    # ── Resume Logic ─────────────────────────────────────────────────────────
    if os.path.exists(output_file):
        try:
            with open(output_file, "r", encoding="utf-8") as f:
                existing_data = json.load(f)
                all_games = existing_data.get("games", [])
                errors = existing_data.get("meta", {}).get("errors", [])
                processed_teams = set(existing_data.get("meta", {}).get("processedTeams", []))
                print(f"Resuming: {len(processed_teams)} teams already processed, {len(all_games)} games loaded.")
        except Exception as e:
            print(f"Could not load existing output file for resumption: {e}")

    # Retry-on-resume: drop previously errored teams so they get re-attempted.
    if errors:
        retry_paths = {team_url_to_path(e["teamUrl"]) for e in errors if e.get("teamUrl")}
        processed_teams -= retry_paths
        errors = []
        if retry_paths:
            print(f"Re-queueing {len(retry_paths)} previously-errored teams for retry.")

    teams_to_do = [t for t in teams if team_url_to_path(t["teamUrl"]) not in processed_teams]
    if not teams_to_do:
        print(f"Nothing to do — all {total_teams} teams already processed.")
    else:
        print(f"Submitting {len(teams_to_do)} teams to {workers} workers...\n")
        agg_lock = threading.Lock()

        def _commit_result(team_id, team_name, team_url, games, error):
            with agg_lock:
                if error is not None:
                    errors.append(error)
                    # Don't mark errored teams as processed — future run will retry.
                else:
                    all_games.extend(games)
                    processed_teams.add(team_id)
                tdone = len(processed_teams)
                errs = len(errors)
                pct = tdone / total_teams * 100 if total_teams else 0.0
                status = "ERR " if error is not None else f"+{len(games):>3}"
                print(f"  [{tdone:>4}/{total_teams}] {pct:5.1f}% | err={errs:<3} | games={len(all_games):>5} | {status} | {team_name}")
                # Periodic save (every 10 successful team completions).
                if (tdone % 10 == 0 or tdone == total_teams) and error is None:
                    try:
                        _save(all_games, errors, total_teams, output_file, processed_teams)
                    except Exception as save_e:
                        print(f"  [WARN] Periodic save failed: {save_e}")

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(_scrape_team, t, season_suffix): t for t in teams_to_do}
            for fut in as_completed(futures):
                try:
                    team_id, team_name, team_url, games, error = fut.result()
                except Exception as e:
                    # A worker crash shouldn't kill the loop. Record it and move on.
                    t = futures[fut]
                    print(f"  [ERROR] Worker crashed for {t.get('teamName', '?')}: {e}")
                    with agg_lock:
                        errors.append({
                            "teamName": t.get("teamName", ""),
                            "teamUrl":  t.get("teamUrl", ""),
                            "stage":    "worker_crash",
                            "error":    str(e),
                        })
                    continue
                _commit_result(team_id, team_name, team_url, games, error)

    # ── Final save ────────────────────────────────────────────────────────────
    # MUST pass processed_teams — otherwise the meta is overwritten with an empty
    # list and the next run would re-scrape every team from scratch.
    _save(all_games, errors, total_teams, output_file, processed_teams)

    # Surface any team in the input that we never reached.
    all_input_paths = {team_url_to_path(t["teamUrl"]) for t in teams}
    missing = all_input_paths - processed_teams
    if missing:
        print(f"\n[WARNING] {len(missing)} input teams were not processed by the scraper:")
        for p in list(missing)[:20]:
            print(f"    {p}")
        if len(missing) > 20:
            print(f"    ... and {len(missing) - 20} more")
        print("Re-run the scraper to retry these teams.")

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
    parser.add_argument(
        "--workers", type=int, default=TEAM_WORKERS,
        help=f"Parallel team workers (default: {TEAM_WORKERS}). "
             f"Each worker scrapes one team's games sequentially. "
             f"Raise for speed, lower if you hit rate limits.",
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

    run(input_file=input_file, output_file=out, sport=args.sport,
        season=args.season, workers=args.workers)


if __name__ == "__main__":
    main()
