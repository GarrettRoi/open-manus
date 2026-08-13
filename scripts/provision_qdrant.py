#!/usr/bin/env python3
"""
Provision the shared Qdrant vector-memory service on Railway and wire the
pilot agents to it. Idempotent — safe to re-run.

What it does:
  1. Ensures a `qdrant` service exists in the project (image qdrant/qdrant:latest).
  2. Ensures the service has QDRANT__SERVICE__API_KEY set (generates a strong
     key on first run; reuses the existing one afterwards).
  3. Attempts to attach a persistent volume at /qdrant/storage. Railway caps
     volumes per project, so this can fail — the script reports it loudly but
     continues (Qdrant then runs WITHOUT persistence until the cap is
     resolved; see plugins/memory/qdrant/README.md).
  4. Sets QDRANT_URL + QDRANT_API_KEY on every pilot agent service. It never
     writes a blank key — provisioning fails instead.

Run: RAILWAY_ACCOUNT_API=... python3 scripts/provision_qdrant.py
     python3 scripts/provision_qdrant.py --pilots samantha,bianca,lexi
"""
import argparse
import os
import secrets
import sys

import requests

from provision_env_vars import (  # noqa: E402
    AGENT_SERVICES, ENVIRONMENT_ID, PROJECT_ID, RAILWAY_API, HEADERS, gql,
    set_agent_vars,
)

QDRANT_IMAGE = "qdrant/qdrant:latest"
QDRANT_INTERNAL_URL = "http://qdrant.railway.internal:6333"
DEFAULT_PILOTS = ["samantha", "bianca", "lexi"]


def find_service(name: str):
    data = gql(
        """query($p:String!){ project(id:$p){ services{ edges{ node{ id name } } } } }""",
        {"p": PROJECT_ID},
    )
    for edge in data["project"]["services"]["edges"]:
        if edge["node"]["name"] == name:
            return edge["node"]["id"]
    return None


def create_service() -> str:
    data = gql(
        """mutation($in:ServiceCreateInput!){ serviceCreate(input:$in){ id } }""",
        {"in": {"projectId": PROJECT_ID, "name": "qdrant",
                "source": {"image": QDRANT_IMAGE}}},
    )
    return data["serviceCreate"]["id"]


def get_service_vars(service_id: str) -> dict:
    data = gql(
        """query($p:String!,$e:String!,$s:String!){
             variables(projectId:$p, environmentId:$e, serviceId:$s) }""",
        {"p": PROJECT_ID, "e": ENVIRONMENT_ID, "s": service_id},
    )
    return data["variables"] or {}


def set_service_vars(service_id: str, variables: dict):
    gql(
        """mutation($in:VariableCollectionUpsertInput!){
             variableCollectionUpsert(input:$in) }""",
        {"in": {"projectId": PROJECT_ID, "environmentId": ENVIRONMENT_ID,
                "serviceId": service_id, "variables": variables}},
    )


def ensure_volume(service_id: str) -> bool:
    data = gql(
        """query($p:String!){ project(id:$p){ volumes{ edges{ node{ id name
             volumeInstances{ edges{ node{ serviceId mountPath } } } } } } } }""",
        {"p": PROJECT_ID},
    )
    for edge in data["project"]["volumes"]["edges"]:
        for inst in edge["node"]["volumeInstances"]["edges"]:
            if inst["node"]["serviceId"] == service_id:
                print(f"  ✓ volume already attached at {inst['node']['mountPath']}")
                return True
    try:
        gql(
            """mutation($in:VolumeCreateInput!){ volumeCreate(input:$in){ id } }""",
            {"in": {"projectId": PROJECT_ID, "environmentId": ENVIRONMENT_ID,
                    "serviceId": service_id, "mountPath": "/qdrant/storage"}},
        )
        print("  ✓ created persistent volume at /qdrant/storage")
        return True
    except Exception as e:
        print(f"  ✗ VOLUME NOT ATTACHED: {e}")
        print("    Qdrant data will NOT survive redeploys of the qdrant service.")
        print("    Fix the project volume cap and re-run this script.")
        return False


def redeploy(service_id: str):
    try:
        gql(
            """mutation($e:String!,$s:String!){
                 serviceInstanceDeployV2(environmentId:$e, serviceId:$s) }""",
            {"e": ENVIRONMENT_ID, "s": service_id},
        )
        print("  ✓ redeploy triggered")
    except Exception as e:
        print(f"  ! redeploy trigger failed (may deploy automatically): {e}")


def main():
    parser = argparse.ArgumentParser(description="Provision Qdrant memory service")
    parser.add_argument("--pilots", default=",".join(DEFAULT_PILOTS),
                        help="Comma-separated agent names to wire up "
                             "(use 'all' for the whole fleet)")
    args = parser.parse_args()
    pilots = (list(AGENT_SERVICES) if args.pilots == "all"
              else [p.strip() for p in args.pilots.split(",") if p.strip()])
    unknown = [p for p in pilots if p not in AGENT_SERVICES]
    if unknown:
        raise SystemExit(f"Unknown agents: {unknown}")

    print("== Qdrant service ==")
    service_id = find_service("qdrant")
    if service_id:
        print(f"  ✓ service exists: {service_id}")
    else:
        service_id = create_service()
        print(f"  ✓ service created: {service_id}")

    svc_vars = get_service_vars(service_id)
    api_key = svc_vars.get("QDRANT__SERVICE__API_KEY", "")
    if api_key:
        print("  ✓ API key already configured")
    else:
        api_key = secrets.token_hex(24)
        set_service_vars(service_id, {"QDRANT__SERVICE__API_KEY": api_key})
        print("  ✓ generated and set QDRANT__SERVICE__API_KEY")
        redeploy(service_id)
    if not api_key:
        raise SystemExit("FATAL: no Qdrant API key available; refusing to "
                         "provision agents with a blank QDRANT_API_KEY")

    print("== Persistent volume ==")
    ensure_volume(service_id)

    print("== Pilot agents ==")
    for name in pilots:
        set_agent_vars(name, AGENT_SERVICES[name], {
            "QDRANT_URL": QDRANT_INTERNAL_URL,
            "QDRANT_API_KEY": api_key,
        })
        print(f"  ✓ {name}: QDRANT_URL + QDRANT_API_KEY set")

    print("\nDone. Agents pick up the vars on their next deploy/restart.")
    print("Enable per agent with `memory.provider: qdrant` in deploy/<agent>/config.yaml.")


if __name__ == "__main__":
    main()
