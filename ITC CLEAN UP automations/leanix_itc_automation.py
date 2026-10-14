#  python3 leanix_itc_automation.py --dry-run (dry run no actions)
#!/usr/bin/env python3
"""
LeanIX ITC Relation Automation
- Applications missing an IT Component relation → tag "Missing ITC" + technicalSuitability="inappropriate"
- Applications with an ITC relation that carry the "Missing ITC" tag → remove tag + reset to "adequate"

Usage:
  python3 leanix_itc_automation.py           # live run
  python3 leanix_itc_automation.py --dry-run # preview only, no mutations
"""

import argparse
import json
import sys
from leanix_client import HOST, get_access_token, graphql as _graphql

GRAPHQL_URL = f"https://{HOST}/services/pathfinder/v1/graphql"

def graphql(token, query, variables=None):
    return _graphql(token, query, variables, url=GRAPHQL_URL)

MISSING_ITC_TAG_NAME = "Missing ITC"
MISSING_ITC_TAG_COLOR = "#FF6B6B"
FLAG_TECH_SUITABILITY = "inappropriate"
UNFLAG_TECH_SUITABILITY = "adequate"

# ---------------------------------------------------------------------------
# Data fetching
# ---------------------------------------------------------------------------

def get_all_applications(token: str) -> list[dict]:
    """Return all Application fact sheets with id, rev, name, technicalSuitability, and tags."""
    query = """
    query($after: String) {
      allFactSheets(factSheetType: Application, first: 100, after: $after) {
        pageInfo { hasNextPage endCursor }
        edges {
          node {
            id rev name
            ... on Application {
              technicalSuitability
              tags { id name }
              relApplicationToITComponent { edges { node { id } } }
              relApplicationToNewObsRiskITComponent { edges { node { id } } }
            }
          }
        }
      }
    }
    """
    apps = []
    cursor = None
    while True:
        data = graphql(token, query, {"after": cursor})
        page = data["allFactSheets"]
        for edge in page["edges"]:
            node = edge["node"]
            apps.append({
                "id": node["id"],
                "rev": node["rev"],
                "name": node["name"],
                "technicalSuitability": node.get("technicalSuitability"),
                "tags": node.get("tags", []),
                "has_itc": (
                    len(node.get("relApplicationToITComponent", {}).get("edges", [])) > 0 or
                    len(node.get("relApplicationToNewObsRiskITComponent", {}).get("edges", [])) > 0
                ),
            })
        if not page["pageInfo"]["hasNextPage"]:
            break
        cursor = page["pageInfo"]["endCursor"]
    return apps


def find_tag_by_name(token: str, name: str) -> str | None:
    """Search for a tag by name across the workspace. Returns tag ID or None."""
    data = graphql(token, "{ allTags { edges { node { id name } } } }")
    for edge in data.get("allTags", {}).get("edges", []):
        tag = edge.get("node", {})
        if tag.get("name") == name:
            return tag["id"]
    return None


def create_tag(token: str, name: str, color: str) -> str:
    """Create a new workspace tag and return its ID."""
    data = graphql(token, f'mutation {{ createTag(name: "{name}", color: "{color}") {{ id name }} }}')
    return data["createTag"]["id"]


def ensure_tag(token: str, name: str, color: str) -> str:
    """Return the ID of the named tag, creating it if it doesn't exist."""
    tag_id = find_tag_by_name(token, name)
    if tag_id:
        return tag_id
    print(f"  Creating tag '{name}'...")
    return create_tag(token, name, color)

# ---------------------------------------------------------------------------
# Mutations
# ---------------------------------------------------------------------------

UPDATE_MUTATION = """
mutation($id: ID!, $rev: Long!, $patches: [Patch]!) {
  updateFactSheet(id: $id, rev: $rev, patches: $patches) {
    factSheet { id rev ... on Application { technicalSuitability tags { id name } } }
  }
}
"""


def flag_application(token: str, app: dict, tag_id: str, dry_run: bool):
    """Add 'Missing ITC' tag and set technicalSuitability to 'inappropriate'."""
    already_tagged = any(t["id"] == tag_id for t in app["tags"])
    already_flagged = app["technicalSuitability"] == FLAG_TECH_SUITABILITY

    if already_tagged and already_flagged:
        print(f"  [SKIP]   {app['name']} — already flagged")
        return "skip"

    if dry_run:
        print(f"  [DRY-RUN FLAG] {app['name']} — would tag + set technicalSuitability={FLAG_TECH_SUITABILITY}")
        return "flag"

    patches = []
    if not already_flagged:
        patches.append({"op": "replace", "path": "/technicalSuitability", "value": FLAG_TECH_SUITABILITY})
    if not already_tagged:
        all_tag_ids = [t["id"] for t in app["tags"]] + [tag_id]
        tag_payload = json.dumps([{"tagId": tid} for tid in all_tag_ids])
        patches.append({"op": "replace", "path": "/tags", "value": tag_payload})

    graphql(token, UPDATE_MUTATION, {"id": app["id"], "rev": app["rev"], "patches": patches})
    print(f"  [FLAG]   {app['name']} — tagged 'Missing ITC' + technicalSuitability={FLAG_TECH_SUITABILITY}")
    return "flag"


def unflag_application(token: str, app: dict, tag_id: str, dry_run: bool):
    """Remove 'Missing ITC' tag and reset technicalSuitability to 'adequate'."""
    has_tag = any(t["id"] == tag_id for t in app["tags"])
    if not has_tag:
        return "skip"

    if dry_run:
        print(f"  [DRY-RUN UNFLAG] {app['name']} — would remove tag + reset technicalSuitability={UNFLAG_TECH_SUITABILITY}")
        return "unflag"

    remaining_tags = [t["id"] for t in app["tags"] if t["id"] != tag_id]
    tag_payload = json.dumps([{"tagId": tid} for tid in remaining_tags])
    patches = [
        {"op": "replace", "path": "/technicalSuitability", "value": UNFLAG_TECH_SUITABILITY},
        {"op": "replace", "path": "/tags", "value": tag_payload},
    ]
    graphql(token, UPDATE_MUTATION, {"id": app["id"], "rev": app["rev"], "patches": patches})
    print(f"  [UNFLAG] {app['name']} — tag removed + technicalSuitability={UNFLAG_TECH_SUITABILITY}")
    return "unflag"

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="LeanIX ITC relation automation")
    parser.add_argument("--dry-run", action="store_true", help="Preview changes without writing")
    args = parser.parse_args()

    print("Authenticating...")
    token = get_access_token()

    print("Fetching all Applications...")
    apps = get_all_applications(token)
    print(f"  Found {len(apps)} applications")

    print(f"Ensuring tag '{MISSING_ITC_TAG_NAME}' exists...")
    tag_id = ensure_tag(token, MISSING_ITC_TAG_NAME, MISSING_ITC_TAG_COLOR)
    print(f"  Tag ID: {tag_id}")

    missing_itc = [a for a in apps if not a["has_itc"]]
    has_itc = [a for a in apps if a["has_itc"]]

    print(f"\nApps missing ITC: {len(missing_itc)}")
    print(f"Apps with ITC:    {len(has_itc)}")
    print()

    counts = {"flag": 0, "unflag": 0, "skip": 0, "error": 0}

    print("Processing apps missing ITC (flagging)...")
    for app in missing_itc:
        try:
            result = flag_application(token, app, tag_id, args.dry_run)
            counts[result] = counts.get(result, 0) + 1
        except Exception as e:
            print(f"  [ERROR]  {app['name']}: {e}", file=sys.stderr)
            counts["error"] += 1

    print("\nProcessing apps with ITC (unflagging if needed)...")
    for app in has_itc:
        try:
            result = unflag_application(token, app, tag_id, args.dry_run)
            counts[result] = counts.get(result, 0) + 1
        except Exception as e:
            print(f"  [ERROR]  {app['name']}: {e}", file=sys.stderr)
            counts["error"] += 1

    mode = "DRY RUN" if args.dry_run else "LIVE"
    print(f"\n{'='*50}")
    print(f"Summary ({mode})")
    print(f"  Flagged:   {counts.get('flag', 0)}")
    print(f"  Unflagged: {counts.get('unflag', 0)}")
    print(f"  Skipped:   {counts.get('skip', 0)}")
    if counts.get("error"):
        print(f"  Errors:    {counts['error']}")
    print(f"{'='*50}")


if __name__ == "__main__":
    main()
