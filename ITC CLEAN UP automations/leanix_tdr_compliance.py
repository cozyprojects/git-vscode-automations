"""
LeanIX Technology Decision Record – Standards Compliance Checker
-----------------------------------------------------------------
Reads the single MULTI_FACTSHEET field on each TDR, splits linked fact sheets
into Tech Categories (TechnicalStack) and IT Components (ITComponent), then
for every TC + ITC combination:

  - If the TC has the "ARB - Approved" tag  → set resourceClassification = "approved"
  - If the TC does NOT have the tag          → set resourceClassification = "investigating"

Writes a summary into the "Exception Justification" field on the TDR for any
ITC whose TC is not ARB-Approved.

Usage:
    python leanix_tdr_compliance.py            # check all TDRs
    python leanix_tdr_compliance.py --dry-run  # report only, no writes
    python leanix_tdr_compliance.py --id <TDR_ID>  # single TDR

Requirements:
    pip install requests tabulate

Configuration:
    Set LEANIX_API_TOKEN, LEANIX_WORKSPACE_HOST, and LEANIX_WORKSPACE as
    environment variables, or edit leanix_client.py defaults.
"""

import sys
import argparse

try:
    from tabulate import tabulate
except ImportError:
    sys.exit("Missing dependency: pip install tabulate")

import requests
from leanix_client import HOST, WORKSPACE, API_TOKEN, get_access_token, graphql


# ── CONFIG ────────────────────────────────────────────────────────────────────

TDR_TEMPLATE_ID               = "786cf026-7aa1-4a07-8daf-e9197324a8f6"
FIELD_SELECTED_FACT_SHEETS    = "c1d8e1c7-646b-4838-9b1c-6e9038c7517d"  # "Factsheet type" (mixed TC + ITC)
FIELD_EXCEPTION_JUSTIFICATION = "2e2271bd-f427-4592-9258-ca5157214b2b"  # "Exception Justification"

ARB_APPROVED_TAG = "ARB - Approved"

RC_APPROVED      = "approved"
RC_INVESTIGATING = "investigating"


# ── FETCH FACT SHEET (TC or ITC) ──────────────────────────────────────────────

FS_QUERY = """
query GetFactSheet($id: ID!) {
  factSheet(id: $id) {
    id
    type
    displayName
    tags { name tagGroup { name } }
  }
}
"""

def fetch_fact_sheet(token, fs_id):
    data = graphql(token, FS_QUERY, {"id": fs_id})
    return data.get("factSheet")


# ── FETCH ITC → TC RELATION EDGE (to read/write resourceClassification) ───────

ITC_TC_RELATION_QUERY = """
query GetITCRelations($id: ID!) {
  factSheet(id: $id) {
    id
    ... on ITComponent {
      relITComponentToTechnicalStack {
        edges {
          node {
            resourceClassification
            factSheet {
              id
              displayName
              tags { name tagGroup { name } }
            }
          }
        }
      }
    }
  }
}
"""

def fetch_itc_tc_relations(token, itc_id):
    """Returns list of { tc_id, tc_name, tc_tags, resource_classification, relation_node }"""
    data = graphql(token, ITC_TC_RELATION_QUERY, {"id": itc_id})
    fs = data.get("factSheet") or {}
    edges = fs.get("relITComponentToTechnicalStack", {}).get("edges", [])
    results = []
    for edge in edges:
        node = edge["node"]
        tc   = node.get("factSheet", {})
        results.append({
            "tc_id":                    tc.get("id"),
            "tc_name":                  tc.get("displayName", "Unknown TC"),
            "tc_tags":                  tc.get("tags", []),
            "resource_classification":  node.get("resourceClassification"),
        })
    return results


# ── UPDATE RESOURCE CLASSIFICATION ON TC ↔ ITC RELATION ──────────────────────

UPDATE_RC_MUTATION = """
mutation UpdateResourceClassification($itcId: ID!, $tcId: ID!, $resourceClassification: String!) {
  updateITComponentToTechnicalStackRelation(
    itComponentId: $itcId
    technicalStackId: $tcId
    patches: [{ op: replace, path: "/resourceClassification", value: $resourceClassification }]
  ) {
    resourceClassification
  }
}
"""

def set_resource_classification(token, itc_id, tc_id, value):
    try:
        graphql(token, UPDATE_RC_MUTATION, {
            "itcId":                  itc_id,
            "tcId":                   tc_id,
            "resourceClassification": value,
        })
        return True
    except Exception as e:
        print(f"    [!] Failed to set resourceClassification on ITC {itc_id} → TC {tc_id}: {e}")
        return False


# ── FETCH ALL TDRs ────────────────────────────────────────────────────────────

def fetch_tdrs(token, single_id=None):
    resp = requests.get(
        f"https://{HOST}/{WORKSPACE}/api/v1/architectureDecisions",
        headers={"Authorization": f"Bearer {token}"},
        params={"templateId": TDR_TEMPLATE_ID, "pageSize": 200},
        timeout=30,
    )
    resp.raise_for_status()
    tdrs = resp.json().get("data", [])
    if single_id:
        tdrs = [t for t in tdrs if t["id"] == single_id]
    return tdrs


# ── UPDATE EXCEPTION JUSTIFICATION ON TDR ─────────────────────────────────────

def update_exception_justification(token, decision_id, text):
    url = f"https://{HOST}/{WORKSPACE}/api/v1/architectureDecisions/{decision_id}"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    current = requests.get(url, headers=headers, timeout=15)
    current.raise_for_status()
    current = current.json()

    fields = []
    for f in current.get("fields", []):
        if f.get("templateFieldId") == FIELD_EXCEPTION_JUSTIFICATION:
            fields.append({
                "templateFieldId": FIELD_EXCEPTION_JUSTIFICATION,
                "content": {"type": "doc", "content": [
                    {"type": "paragraph", "content": [{"type": "text", "text": text}]}
                ]},
            })
        else:
            fields.append(f)

    requests.put(url, headers=headers, json={**current, "fields": fields}, timeout=30).raise_for_status()


# ── COMPLIANCE CHECK ──────────────────────────────────────────────────────────

def is_arb_approved(tags):
    return any(t.get("name") == ARB_APPROVED_TAG for t in tags)


def check_tdr(token, tdr, dry_run):
    title      = tdr.get("title") or "Untitled"
    display_id = tdr.get("displayId", "")
    tdr_id     = tdr["id"]

    # Pull all linked fact sheet IDs from the single mixed field
    linked_ids = []
    for field in tdr.get("fields", []):
        if field.get("templateFieldId") == FIELD_SELECTED_FACT_SHEETS:
            linked_ids = field.get("factSheetIds") or []
            break

    if not linked_ids:
        return {
            "id": tdr_id, "display_id": display_id, "title": title,
            "status": "SKIPPED", "reason": "No fact sheets linked", "findings": [],
        }

    # Split linked fact sheets into TCs and ITCs by type
    tcs, itcs = [], []
    for fs_id in linked_ids:
        fs = fetch_fact_sheet(token, fs_id)
        if not fs:
            continue
        if fs["type"] == "TechnicalStack":
            tcs.append(fs)
        elif fs["type"] == "ITComponent":
            itcs.append(fs)

    if not tcs:
        return {
            "id": tdr_id, "display_id": display_id, "title": title,
            "status": "SKIPPED", "reason": "No Tech Category linked", "findings": [],
        }
    if not itcs:
        return {
            "id": tdr_id, "display_id": display_id, "title": title,
            "status": "SKIPPED", "reason": "No IT Components linked", "findings": [],
        }

    findings = []

    for tc in tcs:
        tc_approved = is_arb_approved(tc.get("tags", []))
        rc_value    = RC_APPROVED if tc_approved else RC_INVESTIGATING

        for itc in itcs:
            relations = fetch_itc_tc_relations(token, itc["id"])
            tc_relation = next((r for r in relations if r["tc_id"] == tc["id"]), None)

            finding = {
                "tc_name":    tc["displayName"],
                "itc_name":   itc["displayName"],
                "tc_approved": tc_approved,
                "rc_value":   rc_value,
                "rc_set":     False,
            }

            if tc_relation is None:
                finding["note"] = "No existing relation between this ITC and TC — skipping RC update"
            else:
                if not dry_run:
                    finding["rc_set"] = set_resource_classification(token, itc["id"], tc["id"], rc_value)
                else:
                    finding["rc_set"] = None  # would have been set

            findings.append(finding)

    exceptions = [f for f in findings if not f["tc_approved"]]

    if exceptions:
        lines = ["⚠ Standards Exception Required\n"]
        lines.append("The following IT Components are linked to Tech Categories not yet ARB-Approved:\n")
        for f in exceptions:
            lines.append(f"• {f['itc_name']} → TC: {f['tc_name']} (Resource Classification set to: {RC_INVESTIGATING})")
        lines.append("\nPlease provide justification for using technologies outside the approved standards.")

        if not dry_run:
            update_exception_justification(token, tdr_id, "\n".join(lines))

        return {
            "id": tdr_id, "display_id": display_id, "title": title,
            "status": "EXCEPTION REQUIRED",
            "reason": f"{len(exceptions)} ITC(s) linked to non-ARB-Approved TC(s)",
            "findings": findings,
        }
    else:
        if not dry_run:
            update_exception_justification(
                token, tdr_id,
                "✓ All selected IT Components are linked to ARB-Approved Tech Categories. Resource Classification set to approved."
            )
        return {
            "id": tdr_id, "display_id": display_id, "title": title,
            "status": "COMPLIANT",
            "reason": "All TCs are ARB-Approved",
            "findings": findings,
        }


# ── MAIN ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="LeanIX TDR Standards Compliance Checker")
    parser.add_argument("--dry-run", action="store_true",
                        help="Report findings without writing back to LeanIX")
    parser.add_argument("--id", dest="tdr_id", default=None,
                        help="Check a single TDR by ID instead of all TDRs")
    args = parser.parse_args()

    if not API_TOKEN or API_TOKEN == "YOUR_API_TOKEN_HERE":
        sys.exit("Error: Set LEANIX_API_TOKEN env variable.")

    if args.dry_run:
        print("DRY RUN — no changes will be written to LeanIX\n")

    print("Authenticating…")
    token = get_access_token()

    print("Fetching Technology Decision Records…")
    tdrs = fetch_tdrs(token, single_id=args.tdr_id)

    if not tdrs:
        print("No TDRs found.")
        return

    print(f"Checking {len(tdrs)} TDR(s)…\n")

    results = []
    for tdr in tdrs:
        result = check_tdr(token, tdr, dry_run=args.dry_run)
        results.append(result)
        icon = "✓" if result["status"] == "COMPLIANT" else ("–" if result["status"] == "SKIPPED" else "⚠")
        print(f"  {icon} [{result['display_id']}] {result['title']} → {result['status']}")

    # Summary table
    print()
    table_rows = [
        [r["display_id"], r["title"], r["status"], r["reason"]]
        for r in results
    ]
    print(tabulate(table_rows, headers=["ID", "Title", "Status", "Detail"],
                   tablefmt="rounded_outline"))

    exceptions = [r for r in results if r["status"] == "EXCEPTION REQUIRED"]
    compliant  = [r for r in results if r["status"] == "COMPLIANT"]
    skipped    = [r for r in results if r["status"] == "SKIPPED"]

    print(f"\n── Summary ──────────────────────────────")
    print(f"  Total TDRs checked:   {len(results)}")
    print(f"  Compliant:            {len(compliant)}")
    print(f"  Exception Required:   {len(exceptions)}")
    print(f"  Skipped:              {len(skipped)}")
    print(f"─────────────────────────────────────────")

    if exceptions:
        print("\n⚠  Exception details:")
        for r in exceptions:
            print(f"\n  [{r['display_id']}] {r['title']}")
            for f in r["findings"]:
                if not f["tc_approved"]:
                    rc_note = f"RC → {f['rc_value']}" if f["rc_set"] else "(dry run)" if f["rc_set"] is None else "RC update failed"
                    print(f"    • {f['itc_name']} → {f['tc_name']} [{rc_note}]")

    if not args.dry_run and (exceptions or compliant):
        print(f"\n✓ Exception Justification field updated on {len(exceptions) + len(compliant)} TDR(s).")


if __name__ == "__main__":
    main()
