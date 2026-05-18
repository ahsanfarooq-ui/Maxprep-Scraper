"""
Recovery: re-scrape teams that the main scraper marked as processed but for
which it actually got zero games (the bid-refresh-bug pattern). Appends new
game records to the existing box_scores file, marks the teams as processed,
and writes atomically.

Usage:
  python retry_skipped_teams.py
  python retry_skipped_teams.py --state TX --sport boys --season 2025-2026
  python retry_skipped_teams.py --workers 10
  python retry_skipped_teams.py --dry-run    # show what would be rescraped, no HTTP

Defaults to TX boys 2025-2026 because that's the one that hit the bug.
"""

import os
import sys
import json
import time
import argparse
import threading
import requests
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

# When refresh_build_id returns the SAME bid, wait this long for MaxPreps to
# roll the build id before retrying. Repeat up to BID_STABLE_MAX_RETRIES times.
BID_STABLE_WAIT_SEC    = 15 * 60   # 15 minutes
BID_STABLE_MAX_RETRIES = 10        # ~2.5 hours total before skipping a team

# Timestamped print so the live log shows when each event happened.
_original_print = print
def print(*args, **kwargs):
    _original_print(time.strftime('[%Y-%m-%d %H:%M:%S]'), *args, **kwargs)

# Reuse all parsing + helper logic from the main scraper. We re-implement the
# per-team driver here so we can use a fixed bid-refresh strategy without
# touching the main scraper module.
from scrape_box_scores import (
    DELAY,
    team_url_to_path,
    fetch_schedule,
    scrape_game,
    get_game_entries,
    get_build_id,
    refresh_build_id,
    _name_from_url,
    _short_season,
    _get_opp_index,
    _save,
)


def _scrape_one_team(team, season_suffix=None, opp_index=None):
    """Returns (team_id, team_name, team_url, games, error_or_None).

    season_suffix (e.g. '24-25') makes the schedule fetch target a past
    season. None = current season.

    opp_index is the slug→canonical-team-id map (from the master team list)
    used to enrich opponent records with full canonical names — same shape
    as the main scraper.

    Fixed bid handling: when refresh returns the SAME bid we know the 404 is
    a transient MaxPreps hiccup (not a stale build), so we back off and retry
    instead of burning through the bid-refresh counter.
    """
    team_url = team["teamUrl"]
    team_id = team_url_to_path(team_url)
    team_name = _name_from_url(team_url, team.get("teamName", ""))
    path = team_url_to_path(team_url)

    contests = None
    bid_change_retries = 0
    stable_bid_retries = 0
    none_retries = 0
    net_retries = 0
    bid, bid_version = get_build_id()

    while contests is None:
        try:
            contests = fetch_schedule(bid, path, season_suffix=season_suffix)

            if isinstance(contests, dict) and contests.get("_expired"):
                new_bid, new_bid_version = refresh_build_id(bid_version)
                if new_bid != bid:
                    # Real bid roll — retry with new bid, don't penalise.
                    bid_change_retries += 1
                    if bid_change_retries > 3:
                        return team_id, team_name, team_url, [], {
                            "teamName": team_name, "teamUrl": team_url,
                            "stage": "schedule", "reason": "build_id_kept_rolling",
                        }
                    bid, bid_version = new_bid, new_bid_version
                    contests = None
                    continue
                # Same bid back — MaxPreps may not have rolled yet. Wait 15 min
                # then check again, up to 10 cycles before skipping.
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
            time.sleep(min(10 * net_retries, 60))
            continue
        except Exception as e:
            return team_id, team_name, team_url, [], {
                "teamName": team_name, "teamUrl": team_url,
                "stage": "schedule", "error": str(e),
            }

    entries = get_game_entries(contests)
    team_games = []
    for game_url, guid, ssid in entries:
        if not guid:
            continue
        for attempt in range(3):
            try:
                record = scrape_game(game_url, guid, ssid, team_name, team_id, opp_index)
                if isinstance(record, dict) and record.get("_404"):
                    break
                if record:
                    team_games.append(record)
                break
            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout):
                time.sleep(min(10 * (attempt + 1), 30))
            except Exception:
                break

    return team_id, team_name, team_url, team_games, None


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--state",  default="TX")
    ap.add_argument("--sport",  default="boys", choices=["boys", "girls"])
    ap.add_argument("--season", default="2025-2026")
    ap.add_argument("--workers", type=int, default=8,
                    help="Parallel workers (default 8 — conservative to avoid re-triggering the original issue)")
    ap.add_argument("--dry-run", action="store_true", help="Show what would be rescraped, then exit")
    args = ap.parse_args()

    state_lower = args.state.lower()
    season_fn = args.season.replace("-", "_")
    gaps_fp = os.path.join(SCRIPT_DIR, f"{state_lower}_data_gaps_{args.sport}_{season_fn}.json")
    bs_fp   = os.path.join(SCRIPT_DIR, f"{state_lower}_box_scores_{args.sport}_{season_fn}.json")

    if not os.path.exists(gaps_fp):
        print(f"[ERROR] Gaps file not found: {gaps_fp}")
        sys.exit(1)
    if not os.path.exists(bs_fp):
        print(f"[ERROR] Box scores file not found: {bs_fp}")
        sys.exit(1)

    with open(gaps_fp, encoding="utf-8") as f: gaps = json.load(f)
    with open(bs_fp,   encoding="utf-8") as f: bs   = json.load(f)

    # Consider every team the gap finder saw — full, partial, AND no-data —
    # so the recovery picks up anything the main scraper missed regardless of
    # its original gap-finder classification.
    seen = set()
    input_teams = []
    for t in (gaps.get("teamsFullBoxScores", []) +
              gaps.get("teamsPartialBoxScores", []) +
              gaps.get("teamsNoBoxScores", [])):
        url = t.get("teamUrl")
        if url and url not in seen:
            seen.add(url)
            input_teams.append(t)
    games           = bs.get("games", [])
    meta            = bs.get("meta", {})
    processed_teams = set(meta.get("processedTeams", []))
    errors          = list(meta.get("errors", []))
    total_teams     = meta.get("totalTeams", len(input_teams))

    # Identify affected: in input list, but no games in output.
    games_per_team = Counter(g.get("team", {}).get("team_id", "") for g in games)
    affected = [t for t in input_teams if games_per_team.get(team_url_to_path(t["teamUrl"]), 0) == 0]
    affected_paths = {team_url_to_path(t["teamUrl"]) for t in affected}

    print(f"Input file       : {bs_fp}")
    print(f"Input teams      : {len(input_teams)}  ({len(games)} games already on disk)")
    print(f"Affected teams   : {len(affected)}     (expected ~{sum(t.get('gamesChecked', 0) for t in affected)} games)")

    if not affected:
        print("\nNothing to recover. Exiting.")
        return

    if args.dry_run:
        print("\n--dry-run set. Affected teams:")
        for t in affected:
            print(f"  gamesChecked={t.get('gamesChecked',0):>3}  |  {t['teamName']}")
        return

    # Un-process them so a future scrape_box_scores run would also pick them up.
    processed_teams -= affected_paths
    # Drop any previous error entries for these teams.
    errors = [e for e in errors if team_url_to_path(e.get("teamUrl", "")) not in affected_paths]

    # Warm build ID cache before fanning out.
    bid, _ = get_build_id()
    # Normalise season for the schedule fetch URL (otherwise MaxPreps falls
    # back to the current season and we'd recover the wrong-season games).
    season_suffix = _short_season(args.season)
    # Master-list slug → canonical-team-id map so opponent records carry
    # full names + canonical team_ids (same shape as the main scraper).
    opp_index = _get_opp_index(args.sport, args.season)
    print(f"Build ID         : {bid}")
    print(f"Season           : {args.season} (URL suffix: {season_suffix or '(current)'})")
    print(f"Opp index size   : {len(opp_index):,} slugs")
    print(f"Workers          : {args.workers}\n")
    print("-" * 60)

    agg_lock = threading.Lock()
    done_count = 0
    new_games_total = 0
    err_count_start = len(errors)

    def commit(team_id, team_name, team_url, team_games, error):
        nonlocal done_count, new_games_total
        with agg_lock:
            done_count += 1
            if error is not None:
                errors.append(error)
                tag = "ERR"
            else:
                games.extend(team_games)
                processed_teams.add(team_id)
                new_games_total += len(team_games)
                tag = f"+{len(team_games):>3}"
            print(f"  [{done_count:>3}/{len(affected)}] {tag} | total_new={new_games_total:>4} | {team_name}")
            # Periodic save every 5 teams.
            if done_count % 5 == 0 or done_count == len(affected):
                try:
                    _save(games, errors, total_teams, bs_fp, processed_teams)
                except Exception as save_e:
                    print(f"  [WARN] Periodic save failed: {save_e}")

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(_scrape_one_team, t, season_suffix, opp_index): t for t in affected}
        for fut in as_completed(futures):
            try:
                team_id, team_name, team_url, team_games, error = fut.result()
            except Exception as e:
                t = futures[fut]
                with agg_lock:
                    errors.append({
                        "teamName": t.get("teamName", ""), "teamUrl": t.get("teamUrl", ""),
                        "stage": "worker_crash", "error": str(e),
                    })
                    done_count += 1
                    print(f"  [{done_count:>3}/{len(affected)}] CRASH | {t.get('teamName', '?')}: {e}")
                continue
            commit(team_id, team_name, team_url, team_games, error)

    _save(games, errors, total_teams, bs_fp, processed_teams)

    new_errors = len(errors) - err_count_start
    print("\n" + "=" * 60)
    print(f"  Recovered teams      : {len(affected) - new_errors}")
    print(f"  New games appended   : {new_games_total}")
    print(f"  Still failed         : {new_errors}")
    print(f"  Output file          : {bs_fp}")
    print("=" * 60)
    if new_errors:
        print("\nStill-failed teams (re-run this script to retry, or check manually):")
        for e in errors[err_count_start:]:
            print(f"  {e.get('teamName', '?'):35s}  reason={e.get('reason') or e.get('error')!r}")


if __name__ == "__main__":
    main()
