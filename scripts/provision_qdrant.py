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
  4. Verifies every pilot has an embedding credential (OPENAI_API_KEY or
     QDRANT_EMBED_API_KEY) — without one the provider stays inactive, so
     provisioning FAILS instead of silently succeeding.
  5. Sets QDRANT_URL + QDRANT_API_KEY + QDRANT_ALLOW_SHARED_WRITE on every
     pilot agent service. It never writes a blank key — provisioning fails
     instead.

Exit code is non-zero if the persistent volume cannot be attached, unless
--allow-no-volume is passed explicitly.

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


def _embedding_works(base_url: str, key: str, model: str) -> bool:
    try:
        r = requests.post(
            f"{base_url.rstrip('/')}/embeddings",
            headers={"Authorization": f"Bearer {key}"},
            json={"model": model, "input": "provisioning probe"},
            timeout=20,
        )
        return r.status_code == 200 and bool(r.json().get("data"))
    except Exception:
        return False


def resolve_embedding_backend(agent_vars: dict):
    """Return (label, extra_env_vars) for a LIVE-validated embedding backend,
    or None if no credential on the service actually works. Mirrors the
    provider's Embedder.from_env precedence."""
    explicit = agent_vars.get("QDRANT_EMBED_API_KEY", "")
    if explicit:
        base = agent_vars.get("QDRANT_EMBED_BASE_URL", "https://api.openai.com/v1")
        model = agent_vars.get("QDRANT_EMBED_MODEL", "text-embedding-3-small")
        if _embedding_works(base, explicit, model):
            return f"explicit QDRANT_EMBED_* ({model})", {}
    openai_key = agent_vars.get("OPENAI_API_KEY", "")
    if openai_key and _embedding_works("https://api.openai.com/v1", openai_key,
                                       "text-embedding-3-small"):
        return "OpenAI (text-embedding-3-small)", {}
    openrouter_key = agent_vars.get("OPENROUTER_API_KEY", "")
    if openrouter_key and _embedding_works("https://openrouter.ai/api/v1",
                                           openrouter_key,
                                           "openai/text-embedding-3-small"):
        # Pin explicit QDRANT_EMBED_* vars so a (possibly dead) OPENAI_API_KEY
        # on the same service cannot take precedence at runtime.
        return "OpenRouter (openai/text-embedding-3-small)", {
            "QDRANT_EMBED_BASE_URL": "https://openrouter.ai/api/v1",
            "QDRANT_EMBED_API_KEY": openrouter_key,
            "QDRANT_EMBED_MODEL": "openai/text-embedding-3-small",
            "QDRANT_EMBED_DIM": "1536",
        }
    return None


def main():
    parser = argparse.ArgumentParser(description="Provision Qdrant memory service")
    parser.add_argument("--pilots", default=",".join(DEFAULT_PILOTS),
                        help="Comma-separated agent names to wire up "
                             "(use 'all' for the whole fleet)")
    parser.add_argument("--allow-no-volume", action="store_true",
                        help="Exit 0 even if the persistent volume cannot be "
                             "attached (data lost on qdrant redeploys)")
    parser.add_argument("--no-shared-write", action="store_true",
                        help="Do not authorize these agents to write "
                             "fleet-shared memories")
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
    have_volume = ensure_volume(service_id)

    print("== Embedding credentials (live-validated) ==")
    embed_vars_by_agent = {}
    failures = []
    for name in pilots:
        agent_vars = get_service_vars(AGENT_SERVICES[name])
        resolved = resolve_embedding_backend(agent_vars)
        if resolved is None:
            print(f"  ✗ {name}: NO WORKING embedding credential — tried "
                  "QDRANT_EMBED_API_KEY, OPENAI_API_KEY, OPENROUTER_API_KEY")
            failures.append(name)
        else:
            label, extra_vars = resolved
            embed_vars_by_agent[name] = extra_vars
            print(f"  ✓ {name}: embeddings via {label}")
    if failures:
        raise SystemExit(
            f"FATAL: agents without a working embedding credential: {failures}. "
            "Fix their OPENAI_API_KEY/OPENROUTER_API_KEY (or set explicit "
            "QDRANT_EMBED_* vars) and re-run."
        )

    print("== Pilot agents ==")
    for name in pilots:
        set_agent_vars(name, AGENT_SERVICES[name], {
            "QDRANT_URL": QDRANT_INTERNAL_URL,
            "QDRANT_API_KEY": api_key,
            "QDRANT_ALLOW_SHARED_WRITE": "false" if args.no_shared_write else "true",
            **embed_vars_by_agent.get(name, {}),
        })
        print(f"  ✓ {name}: QDRANT_URL + QDRANT_API_KEY + shared-write flag set")

    print("\nDone. Agents pick up the vars on their next deploy/restart.")
    print("Enable per agent with `memory.provider: qdrant` in deploy/<agent>/config.yaml.")
    if not have_volume and not args.allow_no_volume:
        raise SystemExit(
            "FAILING (exit 1): Qdrant has no persistent volume — memories are "
            "lost when the qdrant service redeploys. Free a volume slot or "
            "raise the project cap, re-run this script, or pass "
            "--allow-no-volume to accept non-persistence explicitly."
        )


if __name__ == "__main__":
    main()
