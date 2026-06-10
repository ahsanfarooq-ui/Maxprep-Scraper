"""APP — new 4-step MaxPreps pipeline.

Replaces the old run_full_pipeline.py + find_stats_only_teams.py +
accumulate_from_stats_tab.py + merge_stats_tab_into_accumulated.py flow.

Step order:
  1. Gap finder          → {state_folder}/{state}_data_gaps_{sport}_{season}.json
  2. Stats-tab (all)     → {state_folder}/{state}_all_stats_tab_{sport}_{season}.json
  3. Box scores          → {state_folder}/{state}_box_scores_{sport}_{season}.json
  4. Final accumulation  → Final_scraped_data/Final_{state}_accumulated_{sport}_{ss}.json
       (per-game accumulator + stats-tab merge with GP=0 guard + TGC fix)
"""
