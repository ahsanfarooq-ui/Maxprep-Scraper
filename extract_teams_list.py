"""
Extract Teams List
==================
Reads a basketball_all_states.json file and outputs a flat list of all
teams with just three fields: team_name, city, state.

Usage:
  python extract_teams_list.py                                      # default input/output
  python extract_teams_list.py --input girls_basketball_all_states.json
  python extract_teams_list.py --input boys_basketball_all_states.json --output boys_teams_list.json
"""

import json
import argparse

def extract_teams(input_file, output_file):
    with open(input_file) as f:
        data = json.load(f)

    teams_list = []

    for state_code, state_data in data.get("byState", {}).items():
        for region, region_data in state_data.get("regions", {}).items():
            for team in region_data.get("teams", []):
                teams_list.append({
                    "team_name": team.get("teamName", ""),
                    "city":      team.get("city", ""),
                    "state":     team.get("state", ""),
                })

    # Sort by state then team name
    teams_list.sort(key=lambda x: (x["state"], x["team_name"]))

    with open(output_file, "w") as f:
        json.dump(teams_list, f, indent=4)

    print(f"Total teams : {len(teams_list)}")
    print(f"Saved       → {output_file}")
    print(f"\nSample (first 5):")
    for t in teams_list[:5]:
        print(f"  {t}")


def main():
    parser = argparse.ArgumentParser(description="Extract flat teams list from all-states JSON")
    parser.add_argument(
        "--input", default="girls_basketball_all_states.json", metavar="FILE",
        help="Input all-states JSON file (default: girls_basketball_all_states.json)",
    )
    parser.add_argument(
        "--output", default=None, metavar="FILE",
        help="Output file (default: derived from input, e.g. girls_teams_list.json)",
    )
    args = parser.parse_args()

    out = args.output
    if out is None:
        out = args.input.replace("_all_states.json", "_teams_list.json")
        if out == args.input:
            out = args.input.replace(".json", "_teams_list.json")

    extract_teams(input_file=args.input, output_file=out)


if __name__ == "__main__":
    main()
