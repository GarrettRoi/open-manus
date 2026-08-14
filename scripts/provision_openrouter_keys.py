#!/usr/bin/env python3
"""Provision one OpenRouter API key per fleet agent for per-agent credit tracking.

Uses the OpenRouter Provisioning API (OPENROUTER_PROVISIONING_KEY) to create a
named runtime key per agent ("agent-<name>"), then sets it as
OPENROUTER_API_KEY on the agent's Railway service. Agents that also use the
key for Qdrant embeddings (explicit QDRANT_EMBED_API_KEY) get that var updated
in lockstep.

Key values are only returned by OpenRouter at creation time. If a key with the
agent's name already exists, it is left alone unless --rotate is passed
(which disables the old key and creates a fresh one).

Env: OPENROUTER_PROVISIONING_KEY, RAILWAY_ACCOUNT_API (or RAILWAY_TOKEN).
Usage: provision_openrouter_keys.py [--agents lexi,bianca] [--rotate] [--limit N]
"""
import argparse
import os
import sys

import requests

RAILWAY_TOKEN = os.environ.get("RAILWAY_ACCOUNT_API") or os.environ.get("RAILWAY_TOKEN")
PROVISIONING_KEY = os.environ.get("OPENROUTER_PROVISIONING_KEY")
PROJECT_ID = "ea6649cb-ac92-44fd-bea9-3fbf6ad5e473"
ENVIRONMENT_ID = "e57f146e-e0b8-4d5c-a443-c30e0baf016f"
OR_BASE = "https://openrouter.ai/api/v1/keys"

AGENT_SERVICES = {
    "harmony":   "fb56002a-09d9-48c5-87ab-6453bae2b325",
    "samantha":  "55729960-9915-4b58-be4b-0502418e5f60",
    "tatiana":   "35016475-6a1e-42a2-95be-1c3ef62982cb",
    "jade":      "5e296395-c8f8-451b-ab2d-5d46e9cf9699",
    "sasha":     "52155bb0-e561-4e58-9c1e-13ae5b359943",
    "scarlett":  "f4a3cad5-3328-4bf5-aab5-ce185ebb99ff",
    "sabrina":   "85b08450-d0f7-4454-a3b8-2118bd30cd6c",
    "cora":      "144238cf-424d-4e4c-af6b-b8ebdd25cebe",
    "raven":     "333c04b2-a264-429c-a11c-343b7eca19b7",
    "bianca":    "03310b9d-eb82-48d1-aef9-b206c358e85a",
    "valentina": "ffe6a337-2475-47ab-83f0-8fceb80312b0",
    "addison":   "4fbd8c66-944b-46b5-83b2-ce2f1c8b6bd9",
    "lexi":      "08006723-2b99-4fa5-aec0-f4afe96a242c",
    "victoria":  "f52fbe26-83e8-48e1-8397-969f84c467c8",
    "vivian":    "ff1f2730-3eaf-43c6-8a27-4c176b354e8f",
}

OR_HEADERS = {"Authorization": f"Bearer {PROVISIONING_KEY}",
              "Content-Type": "application/json"}
RW_HEADERS = {"Authorization": f"Bearer {RAILWAY_TOKEN}",
              "Content-Type": "application/json"}


def rw_gql(query, variables):
    r = requests.post("https://backboard.railway.com/graphql/v2",
                      headers=RW_HEADERS,
                      json={"query": query, "variables": variables}, timeout=60)
    r.raise_for_status()
    out = r.json()
    if out.get("errors"):
        raise RuntimeError(out["errors"])
    return out["data"]


def get_service_vars(service_id):
    return rw_gql(
        """query($p:String!,$e:String!,$s:String!){
             variables(projectId:$p, environmentId:$e, serviceId:$s) }""",
        {"p": PROJECT_ID, "e": ENVIRONMENT_ID, "s": service_id})["variables"]


def set_service_var(service_id, name, value):
    rw_gql(
        """mutation($in:VariableUpsertInput!){ variableUpsert(input:$in) }""",
        {"in": {"projectId": PROJECT_ID, "environmentId": ENVIRONMENT_ID,
                "serviceId": service_id, "name": name, "value": value}})


def list_or_keys():
    keys, offset = [], 0
    while True:
        r = requests.get(f"{OR_BASE}?offset={offset}&include_disabled=false",
                         headers=OR_HEADERS, timeout=30)
        r.raise_for_status()
        batch = r.json().get("data", [])
        keys.extend(batch)
        if len(batch) < 100:
            return keys
        offset += len(batch)


def create_or_key(name, limit=None):
    body = {"name": name}
    if limit is not None:
        body["limit"] = limit
    r = requests.post(OR_BASE, headers=OR_HEADERS, json=body, timeout=30)
    r.raise_for_status()
    out = r.json()
    return out["key"], out["data"]["hash"]


def disable_or_key(key_hash):
    r = requests.patch(f"{OR_BASE}/{key_hash}", headers=OR_HEADERS,
                       json={"disabled": True}, timeout=30)
    r.raise_for_status()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--agents", help="comma-separated subset (default: all)")
    ap.add_argument("--rotate", action="store_true",
                    help="disable an existing same-named key and mint a new one")
    ap.add_argument("--limit", type=float, default=None,
                    help="optional per-key credit limit (USD)")
    args = ap.parse_args()

    if not PROVISIONING_KEY:
        sys.exit("FATAL: OPENROUTER_PROVISIONING_KEY not set")
    if not RAILWAY_TOKEN:
        sys.exit("FATAL: RAILWAY_ACCOUNT_API / RAILWAY_TOKEN not set")

    agents = list(AGENT_SERVICES) if not args.agents else [
        a.strip() for a in args.agents.split(",")]
    unknown = [a for a in agents if a not in AGENT_SERVICES]
    if unknown:
        sys.exit(f"FATAL: unknown agents: {unknown}")

    existing = {k["name"]: k for k in list_or_keys()}
    failures = []
    for name in agents:
        key_name = f"agent-{name}"
        sid = AGENT_SERVICES[name]
        try:
            prior = existing.get(key_name)
            if prior and not args.rotate:
                print(f"  = {name}: key '{key_name}' already exists — leaving "
                      "as-is (value not retrievable; use --rotate to replace)")
                continue
            if prior and args.rotate:
                disable_or_key(prior["hash"])
                print(f"  - {name}: disabled old key '{key_name}'")
            value, _ = create_or_key(key_name, args.limit)
            set_service_var(sid, "OPENROUTER_API_KEY", value)
            updated = ["OPENROUTER_API_KEY"]
            # Keep embedding credential in lockstep where explicitly pinned.
            if get_service_vars(sid).get("QDRANT_EMBED_API_KEY"):
                set_service_var(sid, "QDRANT_EMBED_API_KEY", value)
                updated.append("QDRANT_EMBED_API_KEY")
            print(f"  ✓ {name}: created '{key_name}', set {' + '.join(updated)}")
        except Exception as e:
            print(f"  ✗ {name}: FAILED — {e}")
            failures.append(name)

    if failures:
        sys.exit(f"FAILED for: {failures}")
    print("\nDone. Railway services pick up new vars on next deploy/restart.")
    print("Per-agent usage: openrouter.ai → Settings → API Keys (one row per agent).")


if __name__ == "__main__":
    main()
