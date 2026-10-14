"""
LeanIX Lifecycle Cleanup Script
--------------------------------
Finds all End-of-Life applications, prioritizes them by business criticality,
checks for missing owner subscriptions, and prints a decommissioning action report.

Usage:
    python leanix_lifecycle_cleanup.py [--survey]

    --survey    Also create a LeanIX survey targeting high-priority EOL apps

Requirements:
    pip install requests tabulate

Configuration:
    Set LEANIX_API_TOKEN, LEANIX_WORKSPACE_HOST, and LEANIX_WORKSPACE as environment
    variables, or edit leanix_client.py defaults.
"""

import sys
import json
import argparse
from datetime import datetime, timedelta

try:
    from tabulate import tabulate
except ImportError:
    sys.exit("Missing dependency: pip install tabulate")

import requests
from leanix_client import HOST, WORKSPACE, API_TOKEN, get_access_token, graphql


# ── CONFIG ────────────────────────────────────────────────────────────────────

CRITICALITY_ORDER = {
    "missionCritical":      1,
    "businessCritical":     2,
    "businessOperational":  3,
    "administrativeService": 4,
    None:                   5,
}

CRITICALITY_LABEL = {
    "missionCritical":      "Mission Critical",
    "businessCritical":     "Business Critical",
    "businessOperational":  "Business Operational",
    "administrativeService": "Administrative",
    None:                   "—",
}


# ── FETCH EOL APPS ────────────────────────────────────────────────────────────

EOL_QUERY = """
query GetEOLApplications($cursor: String) {
  allFactSheets(
    filter: {
      facetFilters: [
        { facetKey: "FactSheetTypes", operator: OR, keys: ["Application"] }
        { facetKey: "lifecycle",      operator: OR, keys: ["endOfLife"] }
      ]
    }
    after: $cursor
    first: 100
  ) {
    pageInfo { hasNextPage endCursor }
    edges {
      node {
        id
        displayName
        ... on Application {
          businessCriticality
          lifecycle { asString phases { phase startDate } }
          subscriptions {
            edges {
              node {
                type
                roles { name }
                user { id displayName email }
              }
            }
          }
          tags { name tagGroup { name } }
        }
      }
    }
  }
}
"""

def fetch_eol_apps(token):
    apps, cursor = [], None
    while True:
        data = graphql(token, EOL_QUERY, {"cursor": cursor})
        page = data["allFactSheets"]
        for edge in page["edges"]:
            apps.append(edge["node"])
        if not page["pageInfo"]["hasNextPage"]:
            break
        cursor = page["pageInfo"]["endCursor"]
    return apps


# ── ANALYSIS ──────────────────────────────────────────────────────────────────

def get_eol_date(app):
    phases = (app.get("lifecycle") or {}).get("phases") or []
    for p in phases:
        if p.get("phase") == "endOfLife" and p.get("startDate"):
            return p["startDate"]
    return None


def get_owners(app):
    owners = []
    for edge in (app.get("subscriptions") or {}).get("edges", []):
        node = edge["node"]
        roles = [r["name"] for r in (node.get("roles") or [])]
        if node.get("type") in ("RESPONSIBLE", "ACCOUNTABLE") or any(
            r.lower() in ("responsible", "accountable", "owner") for r in roles
        ):
            user = node.get("user") or {}
            owners.append(user.get("email") or user.get("displayName") or "Unknown")
    return owners


def build_report(apps):
    rows = []
    for app in apps:
        criticality = app.get("businessCriticality")
        eol_date    = get_eol_date(app)
        owners      = get_owners(app)
        url         = f"https://{HOST}/{WORKSPACE}/factsheet/Application/{app['id']}"

        rows.append({
            "id":          app["id"],
            "name":        app["displayName"],
            "criticality": criticality,
            "eol_date":    eol_date or "—",
            "owners":      ", ".join(owners) if owners else "⚠ NO OWNER",
            "url":         url,
            "priority":    CRITICALITY_ORDER.get(criticality, 5),
        })

    rows.sort(key=lambda r: (r["priority"], r["name"]))
    return rows


# ── SURVEY ────────────────────────────────────────────────────────────────────

def create_decommission_survey(token, high_priority_ids):
    """
    Creates a LeanIX survey for high-priority EOL apps asking owners
    to confirm decommissioning plans.
    """
    deadline = (datetime.utcnow() + timedelta(weeks=3)).strftime("%Y-%m-%d")

    payload = {
        "pollTitle": f"Decommissioning Confirmation – EOL Apps ({datetime.utcnow().strftime('%Y-%m-%d')})",
        "pollFactSheetType": "Application",
        "surveyDurationInWeeks": 3,
        "introductionSubject": "Action Required: Confirm Decommissioning Plan",
        "introductionText": (
            "These applications are marked End-of-Life in our EA repository. "
            "Please confirm the decommissioning status and provide a target shutdown date. "
            f"Deadline: {deadline}."
        ),
        "factsheetIds": high_priority_ids,
        "questions": [
            {
                "type": "radio",
                "label": "What is the current decommissioning status?",
                "options": [
                    {"label": "Decommissioning in progress – on track"},
                    {"label": "Decommissioning planned but not yet started"},
                    {"label": "Blocked – requires further review"},
                    {"label": "Still in use – lifecycle data is incorrect"},
                ],
            },
            {
                "type": "date",
                "label": "Expected shutdown / decommission date",
            },
            {
                "type": "textarea",
                "label": "Any blockers, dependencies, or notes?",
            },
        ],
    }

    resp = requests.post(
        f"https://{HOST}/{WORKSPACE}/api/v1/polls",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json=payload,
        timeout=30,
    )
    if not resp.ok:
        print(f"  [!] Survey creation failed: {resp.status_code} – {resp.text}")
        return None
    return resp.json().get("id")


# ── MAIN ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="LeanIX EOL Lifecycle Cleanup Report")
    parser.add_argument("--survey", action="store_true",
                        help="Create a decommissioning survey for high-priority EOL apps")
    parser.add_argument("--json", dest="as_json", action="store_true",
                        help="Output results as JSON instead of a table")
    args = parser.parse_args()

    if not API_TOKEN or API_TOKEN == "YOUR_API_TOKEN_HERE":
        sys.exit("Error: Set LEANIX_API_TOKEN env variable.")

    print("Authenticating…")
    token = get_access_token()

    print("Fetching End-of-Life applications…")
    apps = fetch_eol_apps(token)
    print(f"Found {len(apps)} EOL applications.\n")

    report = build_report(apps)

    if args.as_json:
        print(json.dumps(report, indent=2))
        return

    # ── Print table ──
    headers = ["Priority", "Application", "Business Criticality", "EOL Date", "Owner(s)"]
    table_rows = [
        [
            r["priority"],
            r["name"],
            CRITICALITY_LABEL.get(r["criticality"], r["criticality"] or "—"),
            r["eol_date"],
            r["owners"],
        ]
        for r in report
    ]
    print(tabulate(table_rows, headers=headers, tablefmt="rounded_outline"))

    # ── Summary ──
    no_owner   = [r for r in report if r["owners"] == "⚠ NO OWNER"]
    high_prio  = [r for r in report if r["priority"] <= 2]
    no_date    = [r for r in report if r["eol_date"] == "—"]

    print(f"\n── Summary ─────────────────────────────")
    print(f"  Total EOL apps:              {len(report)}")
    print(f"  High priority (crit/mission): {len(high_prio)}")
    print(f"  Missing owner:               {len(no_owner)}")
    print(f"  No EOL date set:             {len(no_date)}")
    print(f"────────────────────────────────────────\n")

    if no_owner:
        print("⚠  Apps with no owner assigned:")
        for r in no_owner:
            print(f"   • {r['name']} [{CRITICALITY_LABEL.get(r['criticality'], '—')}]")
            print(f"     {r['url']}")
        print()

    # ── Optional survey ──
    if args.survey:
        high_prio_ids = [r["id"] for r in high_prio]
        if not high_prio_ids:
            print("No high-priority EOL apps found — skipping survey creation.")
        else:
            print(f"Creating decommissioning survey for {len(high_prio_ids)} high-priority apps…")
            survey_id = create_decommission_survey(token, high_prio_ids)
            if survey_id:
                print(f"  Survey created: {survey_id}")
                print(f"  View at: https://{HOST}/{WORKSPACE}/surveys/{survey_id}")
            else:
                print("  Survey creation failed — check output above.")


if __name__ == "__main__":
    main()
