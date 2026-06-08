"""
Full MaxPreps pipeline orchestrator.

Runs every stage end-to-end for a single (state, sport, season) trio:

  1. Gap finder              (app.py)
       → {state_folder}/{state_lower}_data_gaps_{sport}_{season}.json
  2. Box-score scraper       (auto-run by stage 1)
       → {state_folder}/{state_lower}_box_scores_{sport}_{season}.json
  3. Per-game accumulator    (auto-run by stage 2)
       → {state_folder}/{state_lower}_accumulated_stats_{sport}_{season}.json
  4. Stats-tab finder        (find_stats_only_teams.py)
       → stats_only_check/{state_lower}_stats_only_teams_{sport}_{season}.json
  5. Stats-tab accumulator   (accumulate_from_stats_tab.py)
       → stats_only_check/{state_lower}_stats_tab_accumulated_{sport}_{season}.json
  6. Updated accumulator     (merge_stats_tab_into_accumulated.py)
       → stats_only_check/{state_lower}_accumulated_stats_{sport}_{season}_updated.json

Stages 1–3 are already chained by the existing pipeline (app.py auto-runs
scrape_box_scores.py which auto-runs Accumulation_data.py). Stages 4–6 are
the stats-tab fallback pipeline we built on top of that.

Originals in the {state}_scraped_data folder are never overwritten by
later stages — the final updated file lands in stats_only_check/.

Usage:
  python run_full_pipeline.py --state TX --sport girls --season 2025-2026
  python run_full_pipeline.py --state AR --sport boys  --season 2025-2026 --workers 15
  python run_full_pipeline.py --state TX --sport girls --season 2025-2026 --start-at 4  # stages 4–6 only
"""

import os
import sys
import time
import json
import shutil
import argparse
import subprocess

# Convention used elsewhere in the repo.
STATE_FOLDERS = {
    'AR': 'Arkansas_scraped_data',
    'LA': 'Louisiana_scraped_data',
    'NM': 'NewMaxico_scraped_data',
    'OK': 'Oklahoma_scraped_data',
    'TX': 'Texas_scraped_data',
}
STATS_OUT_DIR = 'stats_only_check'


def _ts(msg):
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def _run(cmd, env):
    """Run a subprocess inheriting stdout/stderr. Returns True on success."""
    _ts(f'$ {" ".join(cmd)}')
    rc = subprocess.run(cmd, env=env).returncode
    if rc != 0:
        _ts(f'  ↳ exit code {rc}')
    return rc == 0


def run_pipeline(state, sport, season, start_at, end_at, workers, output_dir=None):
    """If output_dir is provided, ALL six stage outputs go to that single
    folder (handy for Streamlit / cloud deployments). Otherwise stages 1–3
    land in {STATE_FOLDERS[state]}/ and stages 4–6 in stats_only_check/."""
    state_code = state.upper()
    state_lower = state.lower()
    season_fn = season.replace('-', '_')
    if output_dir:
        state_folder = output_dir
        stats_folder = output_dir
    else:
        state_folder = STATE_FOLDERS.get(state_code, f'{state_code}_scraped_data')
        stats_folder = STATS_OUT_DIR
    os.makedirs(state_folder, exist_ok=True)
    os.makedirs(stats_folder, exist_ok=True)

    # Canonical paths the pipeline writes/reads
    gaps_path = os.path.join(state_folder, f'{state_lower}_data_gaps_{sport}_{season_fn}.json')
    box_path  = os.path.join(state_folder, f'{state_lower}_box_scores_{sport}_{season_fn}.json')
    acc_path  = os.path.join(state_folder, f'{state_lower}_accumulated_stats_{sport}_{season_fn}.json')

    flag_path = os.path.join(stats_folder, f'{state_lower}_stats_only_teams_{sport}_{season_fn}.json')
    stab_path = os.path.join(stats_folder, f'{state_lower}_stats_tab_accumulated_{sport}_{season_fn}.json')
    upd_path  = os.path.join(stats_folder, f'{state_lower}_accumulated_stats_{sport}_{season_fn}_updated.json')

    # Subprocess environment — DATA_DIR redirects stage-1 outputs into the
    # per-state folder. PYTHONIOENCODING forces UTF-8 stdout on Windows.
    env = os.environ.copy()
    env['DATA_DIR'] = state_folder
    env['PYTHONIOENCODING'] = 'utf-8'
    env['PYTHONUTF8'] = '1'

    py = [sys.executable, '-X', 'utf8']

    print('=' * 78)
    _ts(f'PIPELINE  state={state_code}  sport={sport}  season={season}')
    _ts(f'  stage 1–3 folder     : {state_folder}/')
    _ts(f'  stage 4–6 folder     : {stats_folder}/')
    _ts(f'  stages {start_at}–{end_at}')
    print('=' * 78)

    # ─── Stage 1–3: gap finder → scraper → accumulator (chained) ────────────
    if start_at <= 1 <= end_at:
        print()
        _ts('━ STAGE 1–3: Gap finder + box-score scraper + accumulator (chained) ━')
        ok = _run(py + ['app.py',
                         '--state', state_code,
                         '--sport', sport,
                         '--season', season],
                  env)
        if not ok:
            _ts('STAGES 1–3 FAILED — stopping.')
            return False

    # ─── Stage 4: find stats-only teams ─────────────────────────────────────
    if start_at <= 4 <= end_at:
        print()
        _ts('━ STAGE 4: find_stats_only_teams ━')
        if not os.path.exists(acc_path):
            _ts(f'  SKIP — accumulated file missing: {acc_path}')
        else:
            ok = _run(py + ['find_stats_only_teams.py',
                             '--accumulated', acc_path,
                             '--season', season,
                             '--workers', str(workers),
                             '--output', flag_path],
                      env)
            if not ok:
                _ts('STAGE 4 FAILED — stopping.')
                return False

    # Read flagged count to decide whether stage 5 has work to do.
    flagged_count = 0
    if os.path.exists(flag_path):
        try:
            with open(flag_path, encoding='utf-8') as f:
                flagged_count = json.load(f).get('meta', {}).get('teamsFlagged', 0)
        except Exception:
            pass

    # ─── Stage 5: scrape the stats-tab for the flagged teams ────────────────
    if start_at <= 5 <= end_at:
        print()
        _ts(f'━ STAGE 5: accumulate_from_stats_tab (flagged teams: {flagged_count}) ━')
        if flagged_count == 0:
            _ts('  SKIP — 0 flagged teams; nothing to scrape from stats tab')
        else:
            ok = _run(py + ['accumulate_from_stats_tab.py',
                             '--input', flag_path,
                             '--season', season,
                             '--workers', str(workers),
                             '--output', stab_path],
                      env)
            if not ok:
                _ts('STAGE 5 FAILED — stopping.')
                return False

    # ─── Stage 6: merge stats-tab into a fresh "_updated" file ──────────────
    if start_at <= 6 <= end_at:
        print()
        _ts('━ STAGE 6: merge_stats_tab_into_accumulated ━')
        if not os.path.exists(acc_path):
            _ts(f'  SKIP — accumulated file missing: {acc_path}')
        elif not os.path.exists(stab_path):
            # Identity copy when there were zero flagged teams: the user
            # still wants every dataset to have an _updated file.
            shutil.copyfile(acc_path, upd_path)
            _ts(f'  (no stats-tab file) Copied accumulated → {upd_path}')
        else:
            ok = _run(py + ['merge_stats_tab_into_accumulated.py',
                             '--accumulated', acc_path,
                             '--stats-tab',   stab_path,
                             '--output',      upd_path],
                      env)
            if not ok:
                _ts('STAGE 6 FAILED.')
                return False

    print()
    print('=' * 78)
    _ts('PIPELINE COMPLETE — outputs:')
    for label, p in [
        ('gaps',          gaps_path),
        ('box scores',    box_path),
        ('accumulated',   acc_path),
        ('flagged teams', flag_path),
        ('stats-tab',     stab_path),
        ('UPDATED',       upd_path),
    ]:
        exists = '✓' if os.path.exists(p) else '·'
        print(f'  {exists} {label:<15} {p}')
    print('=' * 78)
    return True


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--state',   required=True,
                    help='State code (TX, AR, LA, NM, OK, …).')
    ap.add_argument('--sport',   required=True, choices=['boys', 'girls'])
    ap.add_argument('--season',  required=True,
                    help='Season (e.g. 2025-2026 or 2024-2025).')
    ap.add_argument('--start-at', type=int, default=1, choices=range(1, 7),
                    help='Skip to a specific stage (1–6). Default 1.')
    ap.add_argument('--end-at',   type=int, default=6, choices=range(1, 7),
                    help='Stop after a specific stage (1–6). Default 6.')
    ap.add_argument('--workers',  type=int, default=15,
                    help='Parallel worker count for stages 4–5.')
    ap.add_argument('--output-dir', default=None,
                    help='Override the output folder. If set, ALL 6 stage '
                         'outputs go here (overrides STATE_FOLDERS routing). '
                         'Useful for Streamlit / cloud deployments.')
    args = ap.parse_args()

    if args.start_at > args.end_at:
        print('--start-at must be ≤ --end-at')
        sys.exit(2)

    ok = run_pipeline(args.state, args.sport, args.season,
                      args.start_at, args.end_at, args.workers,
                      output_dir=args.output_dir)
    sys.exit(0 if ok else 1)


if __name__ == '__main__':
    main()
