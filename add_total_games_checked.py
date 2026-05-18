"""
Add TotalGamesChecked to an existing accumulated-stats file.
============================================================
One-shot post-processor that injects the `TotalGamesChecked` field into
every `team_total` / "Season Totals" record in an accumulated_stats JSON,
using the matching data-gaps file as the source for `gamesChecked`.

Records are matched by BOTH `team_id` (against the gaps file's `teamUrl`
with the maxpreps.com prefix stripped) AND `team_name` (against `teamName`).
Player records are not touched. team_total records that don't have a match
in the gaps file are left unchanged (no null pollution).

The new field is inserted in the dict immediately after `GP` so it reads
naturally alongside it.

Usage:
  python add_total_games_checked.py --state TX --sport girls --season 2025-2026
  python add_total_games_checked.py --accumulated path/to/acc.json --gaps path/to/gaps.json
  python add_total_games_checked.py --state TX --sport girls --season 2025-2026 --dry-run
"""

import os
import sys
import json
import time
import argparse

# Timestamped print.
_original_print = print
def print(*args, **kwargs):  # noqa: A001
    _original_print(time.strftime('[%Y-%m-%d %H:%M:%S]'), *args, **kwargs)


def _default_paths(state, sport, season):
    """Convention-based file paths matching the rest of the pipeline."""
    state_lower = state.lower()
    season_fn = season.replace("-", "_")
    base = os.path.dirname(os.path.abspath(__file__))
    acc = os.path.join(base, f"{state_lower}_accumulated_stats_{sport}_{season_fn}.json")
    gaps = os.path.join(base, f"{state_lower}_data_gaps_{sport}_{season_fn}.json")
    return acc, gaps


def _load_games_checked_lookup(gaps_path):
    """Build {(team_id, team_name): gamesChecked} from a data-gaps file."""
    with open(gaps_path, encoding='utf-8') as f:
        gaps = json.load(f)
    lookup = {}
    for bucket in ('teamsFullBoxScores', 'teamsPartialBoxScores', 'teamsNoBoxScores'):
        for t in gaps.get(bucket, []):
            url = t.get('teamUrl', '')
            if not url:
                continue
            tid = url.replace('https://www.maxpreps.com/', '').rstrip('/')
            tname = (t.get('teamName') or '').replace('Aandm', 'A&M').replace('aandm', 'a&m')
            gc = t.get('gamesChecked')
            if tid and tname and gc is not None:
                lookup[(tid, tname)] = gc
    return lookup


def add_total_games_checked(accumulated_path, gaps_path, dry_run=False):
    if not os.path.exists(accumulated_path):
        print(f"[ERROR] Accumulated file not found: {accumulated_path}")
        return None
    if not os.path.exists(gaps_path):
        print(f"[ERROR] Gaps file not found: {gaps_path}")
        return None

    print(f"Accumulated input: {accumulated_path}")
    print(f"Gaps source:      {gaps_path}")

    lookup = _load_games_checked_lookup(gaps_path)
    print(f"Loaded {len(lookup)} (team_id, team_name) → gamesChecked entries from gaps.")

    with open(accumulated_path, encoding='utf-8') as f:
        records = json.load(f)
    if not isinstance(records, list):
        print(f"[ERROR] Accumulated file's top-level value is not a list "
              f"(got {type(records).__name__}). Aborting.")
        return None

    total_team_totals = 0
    matched = 0
    skipped_no_match = 0
    already_had_field = 0
    new_records = []

    for r in records:
        if r.get('record_type') != 'team_total' or r.get('Name') != 'Season Totals':
            # Player records and other types untouched.
            new_records.append(r)
            continue

        total_team_totals += 1
        key = (r.get('team_id'), r.get('team_name'))
        gc = lookup.get(key)

        if gc is None:
            skipped_no_match += 1
            new_records.append(r)
            continue

        if 'TotalGamesChecked' in r:
            already_had_field += 1
            # Refresh the value in case gaps changed since last run.
            r = {**r, 'TotalGamesChecked': gc}
            new_records.append(r)
            continue

        # Inject the field right after GP so it reads alongside it.
        new_rec = {}
        for k, v in r.items():
            new_rec[k] = v
            if k == 'GP':
                new_rec['TotalGamesChecked'] = gc
        # Safety: if for some reason 'GP' wasn't in the record, append at end.
        if 'TotalGamesChecked' not in new_rec:
            new_rec['TotalGamesChecked'] = gc
        new_records.append(new_rec)
        matched += 1

    print()
    print("=" * 60)
    print(f"  team_total records in file       : {total_team_totals}")
    print(f"  Matched (TotalGamesChecked added): {matched}")
    print(f"  Refreshed (field already existed): {already_had_field}")
    print(f"  Skipped (no gaps-file match)     : {skipped_no_match}")
    print("=" * 60)

    if dry_run:
        print("Dry run — not writing the file.")
        return None

    # Atomic write so an interrupted save can't corrupt the existing file.
    tmp = accumulated_path + ".tmp"
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(new_records, f, indent=4, ensure_ascii=False)
    os.replace(tmp, accumulated_path)
    print(f"Saved → {accumulated_path}")
    return accumulated_path


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--state",  default="TX", help="State code (used to derive default paths)")
    ap.add_argument("--sport",  default="girls", choices=["boys", "girls"])
    ap.add_argument("--season", default="2025-2026",
                    help="Season (e.g. 2025-2026 or 2024-2025)")
    ap.add_argument("--accumulated", default=None,
                    help="Override path to the accumulated-stats JSON")
    ap.add_argument("--gaps",        default=None,
                    help="Override path to the data-gaps JSON")
    ap.add_argument("--dry-run", action="store_true",
                    help="Print what would change, don't write the file")
    args = ap.parse_args()

    acc, gaps = _default_paths(args.state, args.sport, args.season)
    if args.accumulated:
        acc = args.accumulated
    if args.gaps:
        gaps = args.gaps
    add_total_games_checked(acc, gaps, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
