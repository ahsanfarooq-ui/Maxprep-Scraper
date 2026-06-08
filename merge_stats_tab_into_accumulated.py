"""
Produce an UPDATED accumulated_stats file by merging stats-tab-scraped
records into a previous accumulated file — without modifying the original.

For each team_id present in the stats-tab file:
  - drop ALL of that team's existing records from the previous file
    (the team_total and any players — typically just a GP=0 placeholder)
  - append the stats-tab file's team_total + player records instead

For every other team_id: keep the previous file's records untouched.

The previous accumulated file is NEVER modified. Output is written to a
new file (caller specifies the path).

Usage:
  python merge_stats_tab_into_accumulated.py \
      --accumulated Texas_scraped_data/tx_accumulated_stats_boys_2024_2025.json \
      --stats-tab   stats_only_check/tx_stats_tab_accumulated_boys_2024_2025.json \
      --output      stats_only_check/tx_accumulated_stats_boys_2024_2025_updated.json
"""

import os
import sys
import json
import time
import argparse


_original_print = print
def print(*args, **kwargs):  # noqa: A001
    _original_print(time.strftime('[%Y-%m-%d %H:%M:%S]'), *args, **kwargs)


def merge(accumulated_path, stats_tab_path, output_path):
    if not os.path.exists(accumulated_path):
        print(f"[ERROR] Previous accumulated file not found: {accumulated_path}")
        return None
    if not os.path.exists(stats_tab_path):
        print(f"[ERROR] Stats-tab accumulated file not found: {stats_tab_path}")
        return None

    with open(accumulated_path, encoding='utf-8') as f:
        prev = json.load(f)
    with open(stats_tab_path, encoding='utf-8') as f:
        stats_tab = json.load(f)

    if not isinstance(prev, list) or not isinstance(stats_tab, list):
        print(f"[ERROR] Both inputs must be flat JSON lists.")
        return None

    # Identify the team_ids the stats-tab file is replacing.
    new_team_ids = {r['team_id'] for r in stats_tab if r.get('record_type') == 'team_total'}

    merged: list = []
    replaced_records = 0
    kept_records = 0
    for r in prev:
        if r.get('team_id') in new_team_ids:
            replaced_records += 1   # will be replaced by stats_tab records
            continue
        merged.append(r)
        kept_records += 1
    merged.extend(stats_tab)

    out_dir = os.path.dirname(output_path)
    if out_dir and not os.path.exists(out_dir):
        os.makedirs(out_dir, exist_ok=True)

    tmp = output_path + ".tmp"
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(merged, f, indent=4, ensure_ascii=False)
    os.replace(tmp, output_path)

    print(f"  prev records          : {len(prev)}")
    print(f"  stats-tab records     : {len(stats_tab)}")
    print(f"  teams replaced        : {len(new_team_ids)}")
    print(f"  prev records replaced : {replaced_records}")
    print(f"  prev records kept     : {kept_records}")
    print(f"  merged records        : {len(merged)}")
    print(f"  output                : {output_path}")
    return {
        'prev_records':     len(prev),
        'stats_tab_records': len(stats_tab),
        'teams_replaced':   len(new_team_ids),
        'replaced_records': replaced_records,
        'kept_records':     kept_records,
        'merged_records':   len(merged),
        'output':           output_path,
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--accumulated', required=True)
    ap.add_argument('--stats-tab',   required=True)
    ap.add_argument('--output',      required=True)
    args = ap.parse_args()
    merge(args.accumulated, args.stats_tab, args.output)


if __name__ == '__main__':
    main()
