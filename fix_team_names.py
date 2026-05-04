import json
import re

def run():
    all_states_file = 'boys_basketball_all_states.json'
    print(f"Loading mapping from {all_states_file}...")
    try:
        with open(all_states_file, 'r') as f:
            all_states_data = json.load(f)
    except Exception as e:
        print(f"Error loading all states file: {e}")
        return

    mapping = {}
    for state, state_data in all_states_data.get('byState', {}).items():
        for region, region_data in state_data.get('regions', {}).items():
            for team in region_data.get('teams', []):
                url = team.get("teamUrl", "")
                m = re.match(r"https?://(?:www\.)?maxpreps\.com/([^/]+)/([^/]+)/([^/]+)/", url)
                if m:
                    key = f"{m.group(1).lower()}/{m.group(2).lower()}/{m.group(3).lower()}"
                    mapping[key] = team.get("teamName")

    print(f"Loaded {len(mapping)} team mappings.")

    stats_file = 'Texas_scraped_data/tx_accumulated_stats_boys_2025_2026.json'
    print(f"Loading {stats_file}...")
    with open(stats_file, 'r') as f:
        stats_data = json.load(f)

    updated_count = 0
    missing = set()
    for item in stats_data:
        team_id = item.get("team_id")
        if team_id:
            parts = team_id.split('/')
            if len(parts) >= 3:
                key = f"{parts[0].lower()}/{parts[1].lower()}/{parts[2].lower()}"
                if key in mapping:
                    item["team_name"] = mapping[key]
                    updated_count += 1
                else:
                    missing.add(key)

    print(f"Updated {updated_count} records out of {len(stats_data)} total records.")
    print(f"Unique teams without a match: {len(missing)}")

    print(f"Saving updated data back to {stats_file}...")
    with open(stats_file, 'w') as f:
        json.dump(stats_data, f, indent=4)
    print("Done!")

if __name__ == "__main__":
    run()
