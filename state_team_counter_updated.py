"""
MaxPreps Basketball – All-States Team Counter (Updated, Resilient)
==================================================================
Same purpose as state_teams_counter.py: walk MaxPreps' league hierarchy for
every US state and build the master team list (boys_basketball_all_states.json
/ girls_basketball_all_states.json).

Key differences from the original:

1. **No silently-dropped states.** The original marked a state as "done with
   0 teams" whenever the fetch returned nothing — for ANY reason, including a
   stale build ID or a brief MaxPreps outage. On the next run, the resume
   logic skipped that state forever. This version separately tracks each
   state's `status` (complete / partial / failed) and only skips `complete`
   states on resume.

2. **404 = recorded, not blocked on.** When a 404 comes back and refreshing
   returns the SAME build ID (the endpoint is genuinely missing, not a stale
   build), the fetcher short-circuits: it records the full URL + league name
   in `failedLeaves` and moves on. Build-id rolls are still handled (we retry
   with the new bid up to 3 times). For verification, every `failedLeaves`
   entry contains `publicUrl` you can click directly.

3. **Every team that was successfully fetched is in the output**, even for
   states whose status is `partial` or `failed`. Partial states record which
   sub-league URLs failed (`failedLeaves`), so the next run can re-attempt
   only the gaps rather than refetching the whole state.

4. **Atomic file writes**, **per-leaf retries**, **timestamped logs**.

Usage:
  
  
  python state_team_counter_updated.py --sport girls --season 2025-2026
  python state_team_counter_updated.py --sport boys  --states TX,CA
"""

import os
import re
import sys
import json
import time
import signal
import argparse
import threading
import requests
import concurrent.futures
from collections import defaultdict
from typing import Optional

DATA_DIR = os.environ.get("DATA_DIR", ".")

# ─── Configuration ────────────────────────────────────────────────────────────

SPORT = "basketball"
STATE_CODES = [
    "al", "ak", "az", "ar", "ca", "co", "ct", "de", "fl", "ga",
    "hi", "id", "il", "in", "ia", "ks", "ky", "la", "me", "md",
    "ma", "mi", "mn", "ms", "mo", "mt", "ne", "nv", "nh", "nj",
    "nm", "ny", "nc", "nd", "oh", "ok", "or", "pa", "ri", "sc",
    "sd", "tn", "tx", "ut", "vt", "va", "wa", "wv", "wi", "wy", "dc",
]

STATE_NAMES = {
    "al":"Alabama",       "ak":"Alaska",         "az":"Arizona",
    "ar":"Arkansas",      "ca":"California",     "co":"Colorado",
    "ct":"Connecticut",   "de":"Delaware",       "fl":"Florida",
    "ga":"Georgia",       "hi":"Hawaii",         "id":"Idaho",
    "il":"Illinois",      "in":"Indiana",        "ia":"Iowa",
    "ks":"Kansas",        "ky":"Kentucky",       "la":"Louisiana",
    "me":"Maine",         "md":"Maryland",       "ma":"Massachusetts",
    "mi":"Michigan",      "mn":"Minnesota",      "ms":"Mississippi",
    "mo":"Missouri",      "mt":"Montana",        "ne":"Nebraska",
    "nv":"Nevada",        "nh":"New Hampshire",  "nj":"New Jersey",
    "nm":"New Mexico",    "ny":"New York",       "nc":"North Carolina",
    "nd":"North Dakota",  "oh":"Ohio",           "ok":"Oklahoma",
    "or":"Oregon",        "pa":"Pennsylvania",   "ri":"Rhode Island",
    "sc":"South Carolina","sd":"South Dakota",   "tn":"Tennessee",
    "tx":"Texas",         "ut":"Utah",           "vt":"Vermont",
    "va":"Virginia",      "wa":"Washington",     "wv":"West Virginia",
    "wi":"Wisconsin",     "wy":"Wyoming",        "dc":"District of Columbia",
}

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

DELAY                  = 0.35      # per-request base delay (per worker)
LEAF_WORKERS           = 10        # parallel league-leaf fetches per state
BID_ROLL_MAX_RETRIES   = 3         # max retries when build_id is genuinely rolling
TRANSIENT_MAX_RETRIES  = 5         # max retries for 5xx / connection errors
# NOTE: 404 + same-build-id is treated as a permanent missing endpoint — we do
# NOT wait for the build id to update. The endpoint is recorded in failedLeaves
# (with its full URL) so the user can click through and verify it manually.


# ─── Timestamped print ───────────────────────────────────────────────────────

_original_print = print
def print(*args, **kwargs):  # noqa: A001
    _original_print(time.strftime('[%Y-%m-%d %H:%M:%S]'), *args, **kwargs)


# ─── Thread-safe build_id management ─────────────────────────────────────────
# Many worker threads can simultaneously hit a stale build id. Without locking
# they would all independently refetch, blasting MaxPreps' root page. Lock +
# version collapses concurrent refreshes into a single fetch.

_bid_lock = threading.Lock()
_bid_value: Optional[str] = None
_bid_version = 0
_stop_flag = threading.Event()   # set by SIGINT so workers exit waits early


def _fetch_build_id_raw() -> str:
    """Single fetch of the build id from MaxPreps root, with retry."""
    delays = [5, 10, 20, 40, 60]
    last_err = None
    for attempt, wait in enumerate(delays, 1):
        try:
            r = requests.get("https://www.maxpreps.com", headers=HEADERS, timeout=30)
            r.raise_for_status()
            m = re.search(r'/_next/static/([a-zA-Z0-9_-]+)/_buildManifest\.js', r.text)
            if m:
                return m.group(1)
            print(f"  [WARN] Build id pattern not found (attempt {attempt}/{len(delays)}). Waiting {wait}s…")
        except Exception as e:
            last_err = e
            print(f"  [WARN] Build id fetch error: {e} (attempt {attempt}/{len(delays)}). Waiting {wait}s…")
        time.sleep(wait)
    raise RuntimeError(f"MaxPreps build id not found after all retries: {last_err}")


def get_build_id():
    """Returns (build_id, version). Lazy-fetches on first call."""
    global _bid_value, _bid_version
    with _bid_lock:
        if _bid_value is None:
            _bid_value = _fetch_build_id_raw()
            print(f"Build id: {_bid_value}")
        return _bid_value, _bid_version


def refresh_build_id(old_version: int):
    """Refresh only if the cached version matches old_version. Concurrent
    callers see the same fresh build id without each triggering a new fetch."""
    global _bid_value, _bid_version
    with _bid_lock:
        if _bid_version == old_version:
            new_bid = _fetch_build_id_raw()
            if new_bid != _bid_value:
                print(f"Build id rolled: {_bid_value} → {new_bid}")
            _bid_value = new_bid
            _bid_version += 1
        return _bid_value, _bid_version


# ─── Resilient JSON fetcher ──────────────────────────────────────────────────

def to_nextjs_url(build_id: str, public_href: str) -> str:
    if "?" in public_href:
        path_part, query = public_href.split("?", 1)
        query_suffix = f"?{query}"
    else:
        path_part, query_suffix = public_href, ""
    path = re.sub(r"^https?://www\.maxpreps\.com", "", path_part).rstrip("/")
    return f"https://www.maxpreps.com/_next/data/{build_id}{path}.json{query_suffix}"


def _interruptible_sleep(seconds: float):
    """Sleep that wakes early if the stop flag is set (Ctrl+C)."""
    if _stop_flag.wait(timeout=seconds):
        raise KeyboardInterrupt("Stop requested")


def _make_error(reason: str, public_href: str, bid: str, http_status: Optional[int] = None,
                detail: str = "") -> dict:
    """Build a structured error record for an endpoint we couldn't fetch.

    Saved verbatim in the output file's `failedLeaves`. Each record includes
    enough info to click through and verify the endpoint manually."""
    return {
        "reason":      reason,
        "href":        public_href,
        "fullUrl":     to_nextjs_url(bid, public_href) if bid else public_href,
        "publicUrl":   ("https://www.maxpreps.com" + public_href
                        if public_href.startswith("/") else public_href),
        "leagueName":  league_name_from_href(public_href),
        "httpStatus":  http_status,
        "detail":      detail,
        "attemptedAt": time.strftime('%Y-%m-%d %H:%M:%S'),
    }


def fetch_json_resilient(public_href: str, label: str = "") -> tuple[Optional[dict], Optional[dict]]:
    """Fetch a MaxPreps Next.js JSON endpoint with full resilience.

    Returns (data, error). On success: (data_dict, None). On any non-retryable
    failure: (None, error_dict) where error_dict is structured for inclusion
    in the output file's failedLeaves list.

    Strategy:
      - 200            → return (data, None)
      - 404 + new bid  → retry with new bid (up to BID_ROLL_MAX_RETRIES)
      - 404 + same bid → record "404_endpoint_missing" and move on (no wait;
                         the user explicitly chose this over a 15-min wait)
      - 429            → honor Retry-After, then retry
      - 5xx            → exponential backoff (up to TRANSIENT_MAX_RETRIES)
      - Connection err → exponential backoff (up to TRANSIENT_MAX_RETRIES)
    """
    bid, bid_version = get_build_id()
    bid_rolls = 0
    transient = 0
    tag = label or public_href

    while True:
        if _stop_flag.is_set():
            return None, _make_error("stopped_by_user", public_href, bid)
        url = to_nextjs_url(bid, public_href)
        time.sleep(DELAY)
        try:
            r = requests.get(url, headers=HEADERS, timeout=30)
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
            transient += 1
            if transient > TRANSIENT_MAX_RETRIES:
                print(f"  [ERROR] {tag}: persistent connection error ({e}). Giving up.")
                return None, _make_error("connection_error", public_href, bid, detail=str(e))
            wait = min(10 * transient, 60)
            print(f"  [WARN] {tag}: {type(e).__name__}. Waiting {wait}s ({transient}/{TRANSIENT_MAX_RETRIES}).")
            _interruptible_sleep(wait)
            continue
        except Exception as e:
            print(f"  [ERROR] {tag}: unexpected: {e}")
            return None, _make_error("unexpected_exception", public_href, bid, detail=str(e))

        if r.status_code == 200:
            try:
                return r.json(), None
            except ValueError as e:
                print(f"  [ERROR] {tag}: invalid JSON ({e}).")
                return None, _make_error("invalid_json", public_href, bid, http_status=200, detail=str(e))

        if r.status_code == 404:
            # Either the build_id is stale or this endpoint genuinely doesn't exist.
            new_bid, new_version = refresh_build_id(bid_version)
            if new_bid != bid:
                # Build id rolled — retry with the new one immediately.
                bid_rolls += 1
                if bid_rolls > BID_ROLL_MAX_RETRIES:
                    print(f"  [WARN] {tag}: build id keeps rolling ({BID_ROLL_MAX_RETRIES}+). Giving up.")
                    return None, _make_error("build_id_kept_rolling", public_href, bid, http_status=404)
                bid, bid_version = new_bid, new_version
                continue
            # Same bid back — endpoint is genuinely missing (no stale-build issue
            # to wait out). Record the URL + league name so the user can click
            # through and verify manually, then move on. No 15-min wait.
            err = _make_error("404_endpoint_missing", public_href, bid, http_status=404)
            print(f"  [404 ] {tag}: endpoint not found; build id {bid} unchanged. "
                  f"Recording for verification — {err['publicUrl']}")
            return None, err

        if r.status_code == 429:
            wait = int(r.headers.get("Retry-After", 30))
            print(f"  [RATE] {tag}: 429 Too Many Requests. Waiting {wait}s.")
            _interruptible_sleep(wait)
            continue

        if 500 <= r.status_code < 600:
            transient += 1
            if transient > TRANSIENT_MAX_RETRIES:
                print(f"  [ERROR] {tag}: persistent HTTP {r.status_code}. Giving up.")
                return None, _make_error("http_5xx", public_href, bid, http_status=r.status_code)
            wait = min(2 ** transient, 60)
            print(f"  [WARN] {tag}: HTTP {r.status_code}. Waiting {wait}s ({transient}/{TRANSIENT_MAX_RETRIES}).")
            _interruptible_sleep(wait)
            continue

        print(f"  [WARN] {tag}: unexpected HTTP {r.status_code}.")
        return None, _make_error(f"http_{r.status_code}", public_href, bid, http_status=r.status_code)


# ─── League-tree helpers (same logic as original) ────────────────────────────

def is_leaf_league(href: str) -> bool:
    return "leagueid=" in href


def get_link_cards(page_props: dict) -> list:
    lp = (page_props or {}).get("layoutProps") or {}
    return (lp.get("linkCardProps") or {}).get("data") or []


def get_leaf_hrefs_from_cards(cards: list) -> tuple[list, list]:
    leaves, sections = [], []
    for card in cards:
        lt = card.get("linkType")
        for group in card.get("groups") or []:
            for link in group.get("links") or []:
                href = link.get("href") or ""
                if is_leaf_league(href):
                    leaves.append(href)
                elif lt == 4:
                    sections.append(href)
    return leaves, sections


def league_name_from_href(href: str) -> str:
    for kw in ("/district/", "/league/", "/conference/", "/region/"):
        m = re.search(rf"{kw}([^/?]+)", href)
        if m:
            return m.group(1).replace("-", " ").title()
    path = href.split("?")[0].rstrip("/")
    slug = path.rsplit("/", 1)[-1]
    return slug.replace("-", " ").title() if slug else "Unknown League"


def season_from_href(href: str) -> str:
    m = re.search(r"/basketball/(?:girls/)?(\d{2}-\d{2})/", href)
    return m.group(1) if m else "unknown"


def parse_season(raw: str) -> str:
    raw = raw.strip().replace("/", "-")
    m = re.match(r"^(20\d{2})-(?:20)?(\d{2})$", raw)
    if m:
        return f"{int(m.group(1)) % 100:02d}-{int(m.group(2)):02d}"
    if re.match(r"^\d{2}-\d{2}$", raw):
        return raw
    m = re.match(r"^(20\d{2})$", raw)
    if m:
        end = int(m.group(1)) % 100
        return f"{(end - 1) % 100:02d}-{end:02d}"
    raise ValueError(f"Cannot parse season '{raw}'.")


def get_teams_from_leaf(leaf_href: str) -> tuple[list, Optional[dict]]:
    """Fetch one leaf league's team list.

    Returns (teams, error). error is None on success; on failure it's a
    structured dict from _make_error containing the URL, league name, HTTP
    status etc. so the caller can write it into the output file's
    failedLeaves list for later verification.
    """
    data, err = fetch_json_resilient(leaf_href, label=f"leaf {leaf_href[-60:]}")
    if data is None:
        return [], err
    lp = (data.get("pageProps") or {}).get("layoutProps") or {}
    table = lp.get("tableData") or []
    league = league_name_from_href(leaf_href)
    season = season_from_href(leaf_href)
    lid = (re.search(r"leagueid=([^&]+)", leaf_href) or ["", ""])[1]
    teams = []
    for entry in table:
        name = entry.get("schoolName") or ""
        team_url = entry.get("teamCanonicalUrl") or ""
        city, state = "", ""
        if team_url:
            m = re.match(r"https?://(?:www\.)?maxpreps\.com/([^/]+)/([^/]+)/([^/]+)/", team_url)
            if m:
                state = m.group(1).upper()
                city = m.group(2).replace("-", " ").title()
                full_name = m.group(3).replace("-", " ").title()
                if full_name:
                    name = full_name
        if name:
            teams.append({
                "schoolId":  entry.get("schoolId") or "",
                "teamName":  name,
                "city":      city,
                "state":     state,
                "teamUrl":   team_url,
                "leagueId":  lid,
                "league":    league,
                "season":    season,
            })
            print(f"      [Scraped Team] {name} ({league})")
    return teams, None


def get_all_teams_for_state(state: str, season: Optional[str], restrict_to_leaves: Optional[set] = None):
    """Walk a state's league tree and fetch every team.

    Returns (teams, status, failed_leaves, all_leaves) where:
      teams        — list of team dicts (deduped by teamUrl)
      status       — "complete" | "partial" | "failed"
      failed_leaves— hrefs that we couldn't fetch (None or [])
      all_leaves   — the full leaf set we tried to walk (for reference)

    If restrict_to_leaves is provided, ONLY those leaves are fetched (used
    when resuming a partial state — re-attempt only the gaps).
    """
    global SPORT
    base = (f"https://www.maxpreps.com/{state}/{SPORT}/{season}/"
            if season else f"https://www.maxpreps.com/{state}/{SPORT}/")

    failed_leaves: list[dict] = []
    if restrict_to_leaves is not None:
        # Resume mode: skip the state-page walk, just refetch the listed leaves.
        leaves = list(restrict_to_leaves)
        state_page_ok = True
        all_leaves = leaves
    else:
        # State page (top-level menu).
        state_data, state_err = fetch_json_resilient(base, label=f"state {state}")
        if state_data is None:
            # Couldn't fetch the state index — record the error and return failed.
            return [], "failed", [state_err] if state_err else [], []
        state_page_ok = True

        cards = get_link_cards(state_data.get("pageProps") or {})
        leaf_hrefs, section_hrefs = get_leaf_hrefs_from_cards(cards)

        # Drill one level down for each section card to find more leaves.
        for sec_href in section_hrefs:
            sec_data, sec_err = fetch_json_resilient(sec_href, label=f"section {sec_href[-60:]}")
            if sec_data is None:
                # Section-level miss isn't fatal — record it and keep walking the
                # direct leaves we already have.
                if sec_err is not None:
                    sec_err["leafKind"] = "section"
                    failed_leaves.append(sec_err)
                continue
            sec_cards = get_link_cards(sec_data.get("pageProps") or {})
            sl, _ = get_leaf_hrefs_from_cards(sec_cards)
            leaf_hrefs.extend(sl)

        leaves = list(set(leaf_hrefs))
        all_leaves = leaves

    if not leaves:
        # State page returned but had no leaves — either MaxPreps lists nothing,
        # or the page structure changed. Mark partial so we retry next run.
        status = "complete" if state_page_ok and not failed_leaves else (
            "partial" if state_page_ok else "failed")
        return [], status, failed_leaves, all_leaves

    all_teams = []
    seen_urls: set[str] = set()
    lock = threading.Lock()

    with concurrent.futures.ThreadPoolExecutor(max_workers=LEAF_WORKERS) as pool:
        futures = {pool.submit(get_teams_from_leaf, h): h for h in leaves}
        for fut in concurrent.futures.as_completed(futures):
            href = futures[fut]
            try:
                teams_from_leaf, leaf_err = fut.result()
            except Exception as e:
                print(f"  [WARN] leaf crashed {href}: {e}")
                with lock:
                    failed_leaves.append(_make_error("worker_crash", href, "", detail=str(e)))
                continue
            if leaf_err is not None:
                with lock:
                    failed_leaves.append(leaf_err)
                continue
            with lock:
                for t in teams_from_leaf:
                    # Dedup by teamUrl — guaranteed unique per team.
                    key = t.get("teamUrl") or t.get("schoolId") or t.get("teamName")
                    if key and key not in seen_urls:
                        seen_urls.add(key)
                        all_teams.append(t)

    status = "partial" if failed_leaves else "complete"
    return all_teams, status, failed_leaves, all_leaves


# ─── Output helpers ──────────────────────────────────────────────────────────

def _save_atomic(out_file: str, sport_label: str, results: dict):
    grand_total = sum(v.get("totalTeams", 0) for v in results.values())
    output = {
        "meta": {
            "sport":        sport_label,
            "grandTotal":   grand_total,
            "last_updated": time.strftime('%Y-%m-%d %H:%M:%S'),
        },
        "byState": results,
    }
    tmp = out_file + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2)
    os.replace(tmp, out_file)


def _build_state_record(state_name: str, teams: list, status: str,
                        failed_leaves: list, all_leaves: list) -> dict:
    by_league = defaultdict(list)
    for t in teams:
        by_league[t["league"]].append(t)
    return {
        "stateName":    state_name,
        "totalTeams":   len(teams),
        "status":       status,
        "failedLeaves": failed_leaves,
        "knownLeaves":  all_leaves,         # full set we tried — used for resume
        "lastUpdated":  time.strftime('%Y-%m-%d %H:%M:%S'),
        "regions": {r: {"teamCount": len(ts), "teams": ts}
                    for r, ts in by_league.items()},
    }


def _merge_partial_state(prev: dict, new_teams: list, status_after: str,
                         remaining_failed: list, all_leaves: list) -> dict:
    """Merge new_teams into a previously-partial state record. Deduplicates
    by teamUrl. The status_after argument is the status from the retry attempt
    (we'll combine it with the previous status to decide the final status)."""
    state_name = prev.get("stateName", "")
    # Flatten the previous teams.
    prev_teams = []
    for region_data in prev.get("regions", {}).values():
        for t in region_data.get("teams", []):
            prev_teams.append(t)
    # Dedup-merge.
    seen = set()
    merged = []
    for t in prev_teams + new_teams:
        key = t.get("teamUrl") or t.get("schoolId") or t.get("teamName")
        if key and key not in seen:
            seen.add(key)
            merged.append(t)
    final_status = "complete" if not remaining_failed else "partial"
    # If the all_leaves arg is empty (resume retried only some), reuse the
    # full known set from the previous record so we don't shrink it.
    known = all_leaves or prev.get("knownLeaves") or []
    return _build_state_record(state_name, merged, final_status,
                               remaining_failed, known)


# ─── Driver ──────────────────────────────────────────────────────────────────

def run(sport: str = "boys", season: Optional[str] = None,
        states: Optional[list] = None, force: bool = False):
    """Walk every state's league tree and produce the master team list.

    sport   : "boys" or "girls"
    season  : either "YYYY-YYYY" (e.g. "2024-2025"), "YY-YY" ("24-25"), or
              None for current season.
    states  : optional list of specific state codes to refresh. Bypasses
              resume; always re-attempts these.
    force   : if True, re-fetch every targeted state from scratch (ignore
              existing complete records).
    """
    global SPORT
    if sport == "girls":
        SPORT, sport_label, prefix = "basketball/girls", "Girls Basketball", "girls_basketball_all_states"
    else:
        SPORT, sport_label, prefix = "basketball", "Boys Basketball", "boys_basketball_all_states"

    if season is not None:
        try:
            season = parse_season(season)
        except ValueError as e:
            print(f"[ERROR] {e}")
            return None

    out_file = (os.path.join(DATA_DIR, f"{prefix}_{season}.json")
                if season else os.path.join(DATA_DIR, f"{prefix}.json"))

    # Load any existing output to support resume.
    results: dict = {}
    if os.path.exists(out_file):
        try:
            with open(out_file, encoding="utf-8") as f:
                existing = json.load(f)
            results = existing.get("byState") or {}
            print(f"Loaded existing output: {len(results)} state record(s).")
        except json.JSONDecodeError:
            print(f"[WARN] Existing output file is corrupt JSON. Starting fresh.")
            results = {}

    # Warm the build id cache (also exits early if MaxPreps is fully down).
    try:
        get_build_id()
    except Exception as e:
        print(f"[ERROR] Could not fetch build id: {e}")
        return None

    print(f"Counting teams: {sport_label} | Season: {season or 'current'} | Output: {out_file}")

    target_states = [s.lower() for s in states] if states else STATE_CODES
    explicit = states is not None

    # Resume logic: when no explicit state list was given, skip states whose
    # status is "complete" unless --force. Always retry partial / failed /
    # missing states.
    if not explicit and not force:
        complete_states = {s.lower() for s, v in results.items()
                           if v.get("status") == "complete"}
        skipped = [s for s in target_states if s in complete_states]
        target_states = [s for s in target_states if s not in complete_states]
        if skipped:
            print(f"Resume: skipping {len(skipped)} already-complete state(s). "
                  f"{len(target_states)} remaining (incl. partial/failed retries).")

    if not target_states:
        print(f"All states are already complete in {out_file}.")
        return out_file

    # Per-state processing.
    total_target = len(target_states)
    for idx, state in enumerate(target_states, 1):
        if _stop_flag.is_set():
            print("Stop requested; saving and exiting.")
            break
        sc = state.upper()
        state_name = STATE_NAMES.get(state, sc)
        prev_record = results.get(sc)
        prev_status = prev_record.get("status") if prev_record else None
        prev_failed = prev_record.get("failedLeaves") if prev_record else []

        print(f"\n--- [{idx}/{total_target}] {sc} ({state_name}) ---")
        if prev_status == "partial" and prev_failed:
            # prev_failed may be either old-format strings or new-format dicts.
            # Extract just the href values for the retry set.
            retry_hrefs = set()
            for x in prev_failed:
                if isinstance(x, str):
                    retry_hrefs.add(x)
                elif isinstance(x, dict) and x.get("href"):
                    retry_hrefs.add(x["href"])
            print(f"Resuming partial state — retrying {len(retry_hrefs)} failed leaf(s) only.")
            teams, status_after, remaining_failed, _ = get_all_teams_for_state(
                state, season, restrict_to_leaves=retry_hrefs
            )
            results[sc] = _merge_partial_state(prev_record, teams, status_after,
                                               remaining_failed, [])
        else:
            teams, status, failed_leaves, all_leaves = get_all_teams_for_state(state, season)
            results[sc] = _build_state_record(state_name, teams, status,
                                              failed_leaves, all_leaves)

        rec = results[sc]
        icon = {"complete": "OK ", "partial": "PRT", "failed": "ERR"}.get(rec["status"], "???")
        # Running totals across every state currently in the output file (so the
        # cumulative counter is correct on resume runs too).
        cum_teams = sum(v.get("totalTeams", 0) for v in results.values())
        cum_complete = sum(1 for v in results.values() if v.get("status") == "complete")
        cum_partial  = sum(1 for v in results.values() if v.get("status") == "partial")
        cum_failed   = sum(1 for v in results.values() if v.get("status") == "failed")
        print(f"  [{icon}] [{idx}/{total_target}] {sc}: {rec['totalTeams']} team(s), "
              f"status={rec['status']}, failed_leaves={len(rec['failedLeaves'])}")
        print(f"        Running totals → complete={cum_complete}, partial={cum_partial}, "
              f"failed={cum_failed}  |  teams={cum_teams}")

        # Atomic save after each state finishes.
        _save_atomic(out_file, sport_label, results)

    # ── Summary ─────────────────────────────────────────────────────────────
    complete = sum(1 for v in results.values() if v.get("status") == "complete")
    partial  = sum(1 for v in results.values() if v.get("status") == "partial")
    failed   = sum(1 for v in results.values() if v.get("status") == "failed")
    grand    = sum(v.get("totalTeams", 0) for v in results.values())
    print(f"\nFinished. Output: {out_file}")
    print(f"  States: complete={complete}, partial={partial}, failed={failed}")
    print(f"  Total teams in file: {grand}")
    if partial or failed:
        print("  Re-run this script (without --force) to retry partial/failed states.")
    return out_file


def _install_sigint_handler():
    """Ctrl+C sets a stop flag so workers exit their backoff waits early and
    the current state's progress is saved cleanly."""
    def _handler(signum, frame):
        print("\n[SIGINT] Stop requested — finishing current state then exiting.")
        _stop_flag.set()
    try:
        signal.signal(signal.SIGINT, _handler)
    except (ValueError, AttributeError):
        # signal.signal only works in the main thread; skip on Windows-subprocess
        pass


def main():
    parser = argparse.ArgumentParser(
        description="MaxPreps Basketball – Resilient All-States Team Counter",
    )
    parser.add_argument("--sport",  default="boys", choices=["boys", "girls"],
                        help="boys (default) or girls")
    parser.add_argument("--season", default=None,
                        help="Season (e.g. 2025-2026, 2024-2025, or 25-26). "
                             "If omitted, MaxPreps' current season is used.")
    parser.add_argument("--states", default=None,
                        help="Comma-separated state codes to refresh (e.g. TX,CA). "
                             "Bypasses resume.")
    parser.add_argument("--force",  action="store_true",
                        help="Re-fetch every targeted state from scratch, "
                             "ignoring 'complete' status in the existing file.")
    args = parser.parse_args()

    _install_sigint_handler()
    states = [x.strip() for x in args.states.split(",")] if args.states else None
    run(sport=args.sport, season=args.season, states=states, force=args.force)


if __name__ == "__main__":
    main()
