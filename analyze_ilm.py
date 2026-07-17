#!/usr/bin/env python3
import json, sys

FILES = {
    "DEV": "dev_ilm_policy",
    "QA": "qa_ilm_policy",
    "PROD": "prod_ilm_policy",
    "CCS": "ccs_ilm_policy",
}

def analyze(path):
    with open(path) as fh:
        data = json.load(fh)
    results = []
    for name, body in data.items():
        phases = body.get("policy", {}).get("phases", {})
        # Find which phases use searchable_snapshot
        ss_phases = [ph for ph, cfg in phases.items()
                     if "searchable_snapshot" in cfg.get("actions", {})]
        delete_phase = phases.get("delete", {})
        delete_actions = delete_phase.get("actions", {})
        has_delete_phase = "delete" in phases
        has_delete_action = "delete" in delete_actions
        # delete_searchable_snapshot default is True
        dss = None
        if has_delete_action:
            dss = delete_actions["delete"].get("delete_searchable_snapshot", True)
        results.append({
            "name": name,
            "ss_phases": ss_phases,
            "uses_ss": bool(ss_phases),
            "has_delete_phase": has_delete_phase,
            "has_delete_action": has_delete_action,
            "delete_searchable_snapshot": dss,
        })
    return results

def classify(r):
    """Return (status, reason) for policies that use searchable snapshots."""
    if not r["uses_ss"]:
        return None
    if not r["has_delete_action"]:
        return ("AT_RISK", "uses searchable_snapshot but has NO delete phase/action -> snapshot never removed by ILM")
    if r["delete_searchable_snapshot"] is False:
        return ("DISABLED", "delete phase explicitly sets delete_searchable_snapshot=false")
    return ("OK", "delete phase present with delete_searchable_snapshot=true (default)")

grand = {}
for cluster, path in FILES.items():
    res = analyze(path)
    grand[cluster] = res
    ss_users = [r for r in res if r["uses_ss"]]
    print(f"\n{'='*70}\nCLUSTER: {cluster}   (total policies: {len(res)}, using searchable_snapshot: {len(ss_users)})\n{'='*70}")
    problems = []
    oks = []
    for r in ss_users:
        status, reason = classify(r)
        line = f"  [{status:9}] {r['name']}  (ss in: {','.join(r['ss_phases'])}; dss={r['delete_searchable_snapshot']}; delete_action={r['has_delete_action']})"
        if status == "OK":
            oks.append(line)
        else:
            problems.append(line)
    if problems:
        print(" -- POLICIES THAT DO NOT DELETE SEARCHABLE SNAPSHOTS --")
        for p in problems:
            print(p)
    else:
        print("  (no problem policies -- all searchable-snapshot policies delete their snapshots)")
    print(f"  ...plus {len(oks)} searchable-snapshot policies that ARE configured correctly (OK)")

# Summary of all problem policies across clusters
print(f"\n\n{'#'*70}\nSUMMARY: policies using searchable snapshots that will LEAK snapshots\n{'#'*70}")
for cluster, res in grand.items():
    bad = [r for r in res if r["uses_ss"] and classify(r)[0] != "OK"]
    print(f"\n{cluster}: {len(bad)} problem policy(ies)")
    for r in bad:
        print(f"   - {r['name']}  ({classify(r)[1]})")
