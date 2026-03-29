#!/usr/bin/env python3
"""
Vault Client — Secure API key fetcher for Open Manus agents.

This module provides a simple interface for agents to fetch API keys
from the centralized vault without ever seeing raw key values in
their environment variables.

Usage:
    # In Python code:
    from vault_client import vault
    api_key = vault.get("OPENAI_API_KEY")

    # Or from command line:
    python3 /app/skills/vault_client/vault_client.py get OPENAI_API_KEY
    python3 /app/skills/vault_client/vault_client.py list
    python3 /app/skills/vault_client/vault_client.py skill OPENAI_API_KEY
    python3 /app/skills/vault_client/vault_client.py export  # exports all granted keys as env vars
"""

import json
import os
import sys
from typing import Optional, Dict, List
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

VAULT_URL = os.getenv("VAULT_URL", "http://vault.railway.internal:8080")
VAULT_TOKEN = os.getenv("VAULT_TOKEN", "")

# In-memory cache to avoid repeated network calls within a single session
_cache: Dict[str, str] = {}


class VaultError(Exception):
    """Raised when vault operations fail."""
    pass


class VaultClient:
    """Client for the Open Manus API Key Vault."""

    def __init__(self, url: str = None, token: str = None):
        self.url = (url or VAULT_URL).rstrip("/")
        self.token = token or VAULT_TOKEN

    def _request(self, path: str) -> dict:
        """Make an authenticated request to the vault."""
        if not self.token:
            raise VaultError(
                "VAULT_TOKEN not set. Cannot authenticate with the vault. "
                "Ask Harmony to set up your vault access."
            )

        req = Request(
            f"{self.url}{path}",
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/json",
            },
        )

        try:
            with urlopen(req, timeout=10) as resp:
                return json.loads(resp.read().decode())
        except HTTPError as e:
            body = e.read().decode() if e.fp else ""
            try:
                detail = json.loads(body).get("detail", body)
            except Exception:
                detail = body
            raise VaultError(f"Vault error ({e.code}): {detail}")
        except URLError as e:
            raise VaultError(f"Cannot reach vault at {self.url}: {e.reason}")

    def get(self, key_name: str, use_cache: bool = True) -> str:
        """
        Fetch an API key value from the vault.

        Args:
            key_name: The name of the key (e.g., "OPENAI_API_KEY")
            use_cache: Whether to use cached values (default True)

        Returns:
            The decrypted key value as a string.

        Raises:
            VaultError: If the key doesn't exist, access is denied, or vault is unreachable.
        """
        if use_cache and key_name in _cache:
            return _cache[key_name]

        data = self._request(f"/api/vault/fetch/{key_name}")
        value = data["value"]
        _cache[key_name] = value
        return value

    def list_keys(self) -> List[dict]:
        """
        List all keys this agent has access to.

        Returns:
            List of dicts with key_name, service, description, skill_description.
        """
        data = self._request("/api/vault/list")
        return data.get("available_keys", [])

    def get_skill(self, key_name: str) -> dict:
        """
        Get the skill/tool usage description for a key.

        Returns:
            Dict with key_name, service, description, skill_description.
        """
        return self._request(f"/api/vault/skill/{key_name}")

    def export_env(self) -> Dict[str, str]:
        """
        Fetch all granted keys and set them as environment variables.
        Returns a dict of key_name -> value for all exported keys.

        This is useful for tools that expect keys in env vars.
        """
        keys = self.list_keys()
        exported = {}
        for key_info in keys:
            name = key_info["key_name"]
            try:
                value = self.get(name)
                os.environ[name] = value
                exported[name] = value
            except VaultError as e:
                print(f"Warning: Could not fetch {name}: {e}", file=sys.stderr)
        return exported

    def clear_cache(self):
        """Clear the in-memory key cache."""
        _cache.clear()



    def store(self, key_name: str, key_value: str, service: str = "", description: str = "", skill_description: str = "") -> dict:
        """
        Store a new key in the vault.
        
        Args:
            key_name: The name of the key (e.g., "GOOGLE_ADMIN_KEY")
            key_value: The actual key value to store
            service: Optional service name (e.g., "Google Cloud")
            description: Optional description
            skill_description: Optional skill/tool usage guide
            
        Returns:
            Dict with success status and message.
            
        Raises:
            VaultError: If the store operation fails.
        """
        if not self.token:
            raise VaultError("VAULT_TOKEN not set. Cannot authenticate with the vault.")
            
        import urllib.request
        import json
        
        payload = json.dumps({
            "key_name": key_name,
            "key_value": key_value,
            "service": service,
            "description": description,
            "skill_description": skill_description
        }).encode()
        
        req = urllib.request.Request(
            f"{self.url}/api/vault/store",
            data=payload,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="POST"
        )
        
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            body = e.read().decode() if e.fp else ""
            try:
                detail = json.loads(body).get("detail", body)
            except Exception:
                detail = body
            raise VaultError(f"Vault error ({e.code}): {detail}")
        except urllib.error.URLError as e:
            raise VaultError(f"Cannot reach vault at {self.url}: {e.reason}")

# Singleton instance for easy import
vault = VaultClient()


def main():
    """CLI interface for the vault client."""
    if len(sys.argv) < 2:
        print("Usage:")
        print("  vault_client.py get <KEY_NAME>     — Fetch a key value")
        print("  vault_client.py list               — List available keys")
        print("  vault_client.py skill <KEY_NAME>    — Get skill description for a key")
        print("  vault_client.py export              — Export all keys as env vars")
        print("  vault_client.py store <KEY_NAME> <VALUE> [--service SVC] [--desc DESC]")
        sys.exit(1)

    command = sys.argv[1].lower()

    try:
        if command == "get":
            if len(sys.argv) < 3:
                print("ERROR: Key name required. Usage: vault_client.py get OPENAI_API_KEY")
                sys.exit(1)
            key_name = sys.argv[2].upper()
            value = vault.get(key_name)
            print(value)

        elif command == "list":
            keys = vault.list_keys()
            if not keys:
                print("No keys available. Ask Harmony to grant you access.")
                return
            print(f"Available keys ({len(keys)}):")
            print("-" * 60)
            for k in keys:
                skill_marker = " [has skill doc]" if k.get("skill_description") else ""
                print(f"  {k['key_name']}")
                if k.get("service"):
                    print(f"    Service: {k['service']}")
                if k.get("description"):
                    print(f"    Description: {k['description']}")
                if skill_marker:
                    print(f"    {skill_marker}")
                print()

        elif command == "skill":
            if len(sys.argv) < 3:
                print("ERROR: Key name required. Usage: vault_client.py skill OPENAI_API_KEY")
                sys.exit(1)
            key_name = sys.argv[2].upper()
            info = vault.get_skill(key_name)
            print(f"Key: {info['key_name']}")
            print(f"Service: {info.get('service', '—')}")
            print(f"Description: {info.get('description', '—')}")
            if info.get("skill_description"):
                print(f"\nSkill / Tool Usage Guide:")
                print("-" * 40)
                print(info["skill_description"])
            else:
                print("\nNo skill description available for this key.")

        elif command == "export":
            exported = vault.export_env()
            if not exported:
                print("No keys exported. You may not have any grants.")
                return
            print(f"Exported {len(exported)} keys as environment variables:")
            for name in sorted(exported.keys()):
                print(f"  {name} = ***")

        elif command == "store":
            if len(sys.argv) < 4:
                print("ERROR: Key name and value required.")
                print("Usage: vault_client.py store KEY_NAME KEY_VALUE [--service SVC] [--desc DESC]")
                sys.exit(1)
            key_name = sys.argv[2].upper()
            key_value = sys.argv[3]
            
            # Parse optional args
            service = ""
            description = ""
            skill_desc = ""
            i = 4
            while i < len(sys.argv):
                if sys.argv[i] == "--service" and i + 1 < len(sys.argv):
                    service = sys.argv[i + 1]
                    i += 2
                elif sys.argv[i] == "--desc" and i + 1 < len(sys.argv):
                    description = sys.argv[i + 1]
                    i += 2
                elif sys.argv[i] == "--skill" and i + 1 < len(sys.argv):
                    skill_desc = sys.argv[i + 1]
                    i += 2
                else:
                    i += 1
            
            result = vault.store(key_name, key_value, service, description, skill_desc)
            print(f"✅ {result['message']}")

        else:
            print(f"Unknown command: {command}")
            print("Available commands: get, list, skill, export, store")
            sys.exit(1)

    except VaultError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
