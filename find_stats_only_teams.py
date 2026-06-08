"""
Find teams whose per-game accumulator captured ZERO data for THEM
(GP == 0 in their team_total row), but whose coach-edited "Stats" tab
DOES have player data.

This is the Sunnyvale Raiders case: gap finder might say
`gamesWithStats: 8` but those 8 games had only the OPPONENT's stats —
nothing for our team. So the accumulator emits `GP: 0` for them. Yet
the team's Stats tab on MaxPreps has season totals + per-player rows.

Input: the accumulated_stats JSON. We use it because team_total.GP is
the only reliable signal of "did the per-game pipeline get our team's
data?" — the gap finder's `gamesWithStats` counts pages with ANY stats,
including pages where only the opponent uploaded.

For every team in the accumulated file with full-canonical team_id and
team_total.GP == 0, this script:
  1. Fetches the team's stats page HTML to get schoolid + ssid.
  2. Hits the print-stats endpoint.
  3. If stats-tab season GP > 0 → flag the team.

Usage:
  python find_stats_only_teams.py --accumulated Texas_scraped_data/tx_accumulated_stats_girls_2025_2026.json
  python find_stats_only_teams.py --accumulated ... --season 2025-2026 --workers 15
"""

import os
import sys
import json
import time
import argparse
import threading
import requests
from bs4 import BeautifulSoup
from concurrent.futures import ThreadPoolExecutor, as_completed

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

from scrape_team_stats import (
    _discover_ids,
    _fetch_print_stats_html,
    _parse_print_stats,
    _short_season,
    _team_url_to_id,
    _name_from_url,
    HEADERS,
    DELAY,
    TEAM_WORKERS,
)

_original_print = print
def print(*args, **kwargs):  # noqa: A001
    _original_print(time.strftime('[%Y-%m-%d %H:%M:%S]'), *args, **kwargs)


def _id_to_url(team_id):
    """Rebuild the public team URL from a canonical team_id."""
    return f"https://www.maxpreps.com/{team_id}/"


def _check_team(team_id, team_name, total_games_checked, season_suffix):
    """Fetch print-stats for one team, return whether stats-tab has data
    and a small summary."""
    team_url = _id_to_url(team_id)
    schoolid, ssid = _discover_ids(team_url, season_suffix)
    if not schoolid or not ssid:
        return None, None, [], 'ids_missing'
    html = _fetch_print_stats_html(schoolid, ssid)
    if html is None:
        return None, None, [], 'fetch_failed'
    per_player, season_total, status = _parse_print_stats(html)
    if status != 'has_data':
        return None, None, [], status
    stats_gp = int(season_total.get('GP') or 0)
    players_with_gp = [(n, int(s.get('GP') or 0))
                       for n, s in per_player.items()
                       if (s.get('GP') or 0) > 0]
    sample = [f"{n} (GP={gp})" for n, gp in sorted(players_with_gp,
                                                    key=lambda x: -x[1])[:5]]
    return stats_gp, len(players_with_gp), sample, None


def run(accumulated_file, season, workers, output_file):
    if not os.path.exists(accumulated_file):
        print(f"[ERROR] Accumulated file not found: {accumulated_file}")
        return None

    with open(accumulated_file, encoding='utf-8') as f:
        records = json.load(f)
    if not isinstance(records, list):
        print(f"[ERROR] Expected a flat list, got {type(records).__name__}")
        return None

    # Candidate teams: team_total rows with GP == 0 and a full canonical team_id.
    # GP==0 means the per-game pipeline got no data for THIS team specifically.
    candidates = []
    skipped_non_canonical = 0
    skipped_has_data = 0
    for r in records:
        if r.get('record_type') != 'team_total':
            continue
        tid = r.get('team_id', '')
        if tid.count('/') < 3:
            # Slug-only ghost (opponent appearance) — not a real candidate.
            skipped_non_canonical += 1
            continue
        if (r.get('GP') or 0) > 0:
            skipped_has_data += 1
            continue
        candidates.append({
            'team_id':            tid,
            'team_name':          r.get('team_name', ''),
            'total_games_checked': r.get('TotalGamesChecked'),
        })

    print(f"Source             : {accumulated_file}")
    print(f"  team_total rows  : {sum(1 for r in records if r.get('record_type')=='team_total')}")
    print(f"  with GP > 0      : {skipped_has_data} (skipped — already have per-game data)")
    print(f"  slug-only ghosts : {skipped_non_canonical} (skipped — opponent-only records)")
    print(f"  GP == 0 + canonical (CHECKED): {len(candidates)}")
    print(f"Workers            : {workers}")
    print(f"Season URL suffix  : {_short_season(season) or '(current)'}")
    print("-" * 70)

    if not candidates:
        # Still write an empty result file so downstream sees the run happened.
        out = {
            'meta': {
                'sourceAccumulated':  os.path.abspath(accumulated_file),
                'season':             season,
                'candidatesChecked':  0,
                'teamsFlagged':       0,
                'teamsSkipped':       0,
                'totalGPRecoverable': 0,
                'generatedAt':        time.strftime('%Y-%m-%d %H:%M:%S'),
            },
            'flaggedTeams': [],
            'skipped':      [],
        }
        with open(output_file, 'w', encoding='utf-8') as f:
            json.dump(out, f, indent=2, ensure_ascii=False)
        print("Nothing to check — empty output file written.")
        return output_file

    season_suffix = _short_season(season)
    flagged = []
    skipped = []
    done = 0
    agg_lock = threading.Lock()

    def process(team):
        nonlocal done
        try:
            stats_gp, n_players, sample, err = _check_team(
                team['team_id'], team['team_name'],
                team['total_games_checked'], season_suffix,
            )
        except Exception as e:
            with agg_lock:
                done += 1
                skipped.append({**team, 'reason': f'exception: {e}'})
                print(f"  [{done:>4}/{len(candidates)}] CRASH | {team['team_name']}: {e}")
            return
        with agg_lock:
            done += 1
            name = team['team_name']
            if err:
                skipped.append({**team, 'reason': err})
                print(f"  [{done:>4}/{len(candidates)}] skip:{err:<15} | {name}")
                return
            if (stats_gp or 0) == 0:
                # Stats tab also empty — not a recoverable case.
                print(f"  [{done:>4}/{len(candidates)}] no-stats-tab-data    | {name}")
                return
            entry = {
                'teamName':              name,
                'team_id':               team['team_id'],
                'teamUrl':               _id_to_url(team['team_id']),
                'acc_GP':                0,
                'acc_TotalGamesChecked': team['total_games_checked'],
                'statsPage_GP':          stats_gp,
                'statsPage_players':     n_players,
                'discrepancy':           stats_gp,
                'samplePlayers':         sample,
            }
            flagged.append(entry)
            tag = f"FLAG +{stats_gp:>3}gp"
            print(f"  [{done:>4}/{len(candidates)}] {tag:25s} | {name}  "
                  f"(stats-tab GP={stats_gp}, players={n_players})")

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(process, t) for t in candidates]
        for fut in as_completed(futures):
            try:
                fut.result()
            except Exception as e:
                with agg_lock:
                    done += 1
                    print(f"  [{done:>4}/{len(candidates)}] CRASH outer: {e}")

    flagged.sort(key=lambda x: -x['discrepancy'])

    output = {
        'meta': {
            'sourceAccumulated':    os.path.abspath(accumulated_file),
            'season':               season,
            'filter':               'team_total.GP == 0 AND stats-tab GP > 0',
            'candidatesChecked':    len(candidates),
            'teamsFlagged':         len(flagged),
            'teamsSkipped':         len(skipped),
            'totalGPRecoverable':   sum(t['discrepancy'] for t in flagged),
            'generatedAt':          time.strftime('%Y-%m-%d %H:%M:%S'),
            'description':          ('Teams whose per-game accumulator captured no '
                                     'data for themselves (their team_total.GP == 0) '
                                     'but whose Stats tab on MaxPreps has player data. '
                                     'Pipeline-style scrape these via '
                                     'accumulate_from_stats_tab.py to recover their '
                                     'season-aggregate stats.'),
        },
        'flaggedTeams': flagged,
        'skipped':      skipped,
    }

    tmp = output_file + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(output, f, indent=2, ensure_ascii=False)
    os.replace(tmp, output_file)

    print()
    print("=" * 70)
    print(f"  Candidates checked  : {len(candidates)}")
    print(f"  Teams FLAGGED       : {len(flagged)}")
    print(f"  Teams skipped       : {len(skipped)} (ids/fetch/parse error)")
    print(f"  Total GP recoverable: {sum(t['discrepancy'] for t in flagged)}")
    print(f"  Output file         : {output_file}")
    print("=" * 70)
    if flagged:
        print()
        print("Top 10 flagged teams by recoverable GP:")
        for t in flagged[:10]:
            print(f"  +{t['discrepancy']:>3}gp  {t['teamName']:40s}  "
                  f"(stats-tab GP={t['statsPage_GP']}, players={t['statsPage_players']})")
    return output_file


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--accumulated', required=True,
                    help='Path to the accumulated_stats JSON file.')
    ap.add_argument('--season',  default='2025-2026',
                    help='Season for the print-stats URL (default 2025-2026).')
    ap.add_argument('--workers', type=int, default=TEAM_WORKERS,
                    help=f'Parallel worker count (default {TEAM_WORKERS}).')
    ap.add_argument('--output',  default=None,
                    help='Output file (default: derived from --accumulated).')
    args = ap.parse_args()

    output = args.output
    if output is None:
        # Default convention: place next to input, swap 'accumulated_stats' → 'stats_only_teams'
        if 'accumulated_stats' in args.accumulated:
            output = args.accumulated.replace('accumulated_stats', 'stats_only_teams')
        else:
            output = args.accumulated.replace('.json', '_stats_only.json')

    run(args.accumulated, args.season, args.workers, output)


if __name__ == '__main__':
    main()
