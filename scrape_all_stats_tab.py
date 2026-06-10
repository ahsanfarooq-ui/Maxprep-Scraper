"""
Scrape MaxPreps' Stats-tab (print-stats endpoint) for EVERY canonical team
in a per-game accumulated file, regardless of whether the per-game pipeline
captured data for that team.

Why this exists:
  accumulate_from_stats_tab.py only processes a curated list (the GP=0
  flagged teams from find_stats_only_teams.py). This script enumerates the
  full canonical team set instead. Output format is identical — a flat
  list of team_total + player records — so any downstream merger that
  consumed accumulate_from_stats_tab.py's output works unchanged.

Key behaviour:
  - team_name on the output records is preserved from the accumulated
    file (NOT re-derived from the URL slug), so a later merge can match
    on (team_id, team_name) without canonicalisation drift.
  - TotalGamesChecked on team_total rows is carried from the accumulated
    file (the per-game pipeline's authoritative value).
  - Teams whose team_id is slug-only (opponent ghosts) are skipped.

Usage:
  python scrape_all_stats_tab.py \\
      --accumulated Texas_scraped_data/tx_accumulated_stats_boys_2025_2026.json \\
      --season 2025-2026 \\
      --workers 15 \\
      --output Texas_scraped_data/tx_all_stats_tab_boys_2025_2026.json
"""

import os
import sys
import json
import time
import argparse
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

from scrape_team_stats import (
    _discover_ids,
    _fetch_print_stats_html,
    _parse_print_stats,
    _short_season,
    TEAM_WORKERS,
)
from accumulate_from_stats_tab import stats_to_accumulated_record


_original_print = print
def print(*args, **kwargs):  # noqa: A001
    _original_print(time.strftime('[%Y-%m-%d %H:%M:%S]'), *args, **kwargs)


def _id_to_url(team_id):
    """Rebuild the public team URL from a canonical team_id."""
    return f"https://www.maxpreps.com/{team_id}/"


def _process_team(team_id, team_name, total_games_checked, season_suffix):
    """Scrape one team's print-stats and produce accumulated-format records.

    Crucially, the records carry the team_id and team_name passed in (from
    the accumulated file), not anything re-derived from the URL.
    """
    team_url = _id_to_url(team_id)
    schoolid, ssid = _discover_ids(team_url, season_suffix)
    if not schoolid or not ssid:
        return [], 'ids_missing'
    html = _fetch_print_stats_html(schoolid, ssid)
    if html is None:
        return [], 'fetch_failed'
    per_player, season_total, status = _parse_print_stats(html)
    if status != 'has_data' or not (per_player or season_total):
        return [], status

    records = []
    if season_total:
        records.append(stats_to_accumulated_record(
            team_id, team_name, 'Season Totals', season_total, 'team_total',
            total_games_checked=total_games_checked,
        ))
    for pname, pstats in per_player.items():
        records.append(stats_to_accumulated_record(
            team_id, team_name, pname, pstats, 'player',
        ))
    return records, 'has_data'


def _enumerate_teams(accumulated_path):
    """Pull every (team_id, team_name, TotalGamesChecked) triple from
    team_total rows whose team_id is a full canonical path (>= 4 slashes).
    Slug-only ghosts (opponent appearances) are excluded."""
    with open(accumulated_path, encoding='utf-8') as f:
        records = json.load(f)
    if not isinstance(records, list):
        raise ValueError(f'Expected a flat list, got {type(records).__name__}')

    teams = []
    seen = set()
    skipped_ghost = 0
    for r in records:
        if r.get('record_type') != 'team_total':
            continue
        tid = r.get('team_id', '')
        if tid.count('/') < 3:
            skipped_ghost += 1
            continue
        if tid in seen:
            continue
        seen.add(tid)
        teams.append({
            'team_id':              tid,
            'team_name':            r.get('team_name', ''),
            'total_games_checked':  r.get('TotalGamesChecked'),
        })
    return teams, skipped_ghost


def run(accumulated_path, season, workers, output_file):
    if not os.path.exists(accumulated_path):
        print(f'[ERROR] Accumulated file not found: {accumulated_path}')
        return None

    teams, skipped_ghost = _enumerate_teams(accumulated_path)
    season_suffix = _short_season(season)

    out_dir = os.path.dirname(output_file)
    if out_dir and not os.path.exists(out_dir):
        os.makedirs(out_dir, exist_ok=True)

    print(f'Source accumulated : {accumulated_path}')
    print(f'  canonical teams  : {len(teams)}')
    print(f'  slug-only skipped: {skipped_ghost}')
    print(f'Season suffix      : {season_suffix or "(current)"}')
    print(f'Workers            : {workers}')
    print(f'Output             : {output_file}')
    print('-' * 70)

    all_records = []
    success = 0
    skipped = []
    done = 0
    lock = threading.Lock()

    def worker(t):
        nonlocal done, success
        try:
            records, status = _process_team(
                t['team_id'], t['team_name'],
                t['total_games_checked'], season_suffix,
            )
        except Exception as e:
            with lock:
                done += 1
                skipped.append({**t, 'reason': f'exception: {e}'})
                print(f'  [{done:>4}/{len(teams)}] CRASH | {t["team_name"]}: {e}')
            return
        with lock:
            done += 1
            if records:
                all_records.extend(records)
                success += 1
                n_players = sum(1 for r in records if r['record_type'] == 'player')
                gp = next((r['GP'] for r in records if r['record_type'] == 'team_total'), 0)
                print(f'  [{done:>4}/{len(teams)}] OK     | {t["team_name"]:38s}  '
                      f'GP={gp:>2}  players={n_players}')
            else:
                skipped.append({**t, 'reason': status})
                print(f'  [{done:>4}/{len(teams)}] skip:{status:<14} | {t["team_name"]}')

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(worker, t) for t in teams]
        for f in as_completed(futures):
            try:
                f.result()
            except Exception as e:
                with lock:
                    done += 1
                    print(f'  [{done:>4}/{len(teams)}] CRASH outer: {e}')

    tmp = output_file + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(all_records, f, indent=4, ensure_ascii=False)
    os.replace(tmp, output_file)

    sidecar = output_file.replace('.json', '_report.json')
    report = {
        'sourceAccumulated':  os.path.abspath(accumulated_path),
        'season':             season,
        'canonicalTeams':     len(teams),
        'teamsScraped':       success,
        'teamsSkipped':       len(skipped),
        'totalRecords':       len(all_records),
        'team_totalRecords':  sum(1 for r in all_records if r['record_type'] == 'team_total'),
        'playerRecords':      sum(1 for r in all_records if r['record_type'] == 'player'),
        'skipReasons':        dict(Counter(s['reason'] for s in skipped)),
        'skipped':            skipped,
        'generatedAt':        time.strftime('%Y-%m-%d %H:%M:%S'),
        'outputFile':         os.path.abspath(output_file),
    }
    tmp2 = sidecar + '.tmp'
    with open(tmp2, 'w', encoding='utf-8') as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    os.replace(tmp2, sidecar)

    print()
    print('=' * 70)
    print(f'  Canonical teams: {len(teams)}')
    print(f'  Scraped OK     : {success}')
    print(f'  Skipped        : {len(skipped)}')
    if skipped:
        for reason, n in Counter(s['reason'] for s in skipped).most_common():
            print(f'    {n:>4}: {reason}')
    print(f'  Records total  : {len(all_records)}  '
          f'({sum(1 for r in all_records if r["record_type"]=="team_total")} team_totals + '
          f'{sum(1 for r in all_records if r["record_type"]=="player")} players)')
    print(f'  Output         : {output_file}')
    print(f'  Sidecar report : {sidecar}')
    print('=' * 70)
    return output_file


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--accumulated', required=True,
                    help='Per-game accumulated_stats JSON file to enumerate teams from.')
    ap.add_argument('--season',  default='2025-2026',
                    help='Season for the print-stats URL (default 2025-2026).')
    ap.add_argument('--workers', type=int, default=TEAM_WORKERS,
                    help=f'Parallel worker count (default {TEAM_WORKERS}).')
    ap.add_argument('--output',  default=None,
                    help='Output file. Default: alongside input, '
                         'accumulated_stats → all_stats_tab.')
    args = ap.parse_args()

    output = args.output
    if output is None:
        if 'accumulated_stats' in args.accumulated:
            output = args.accumulated.replace('accumulated_stats', 'all_stats_tab')
        else:
            base, ext = os.path.splitext(args.accumulated)
            output = f'{base}_all_stats_tab{ext}'

    run(args.accumulated, args.season, args.workers, output)


if __name__ == '__main__':
    main()
