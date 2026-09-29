"""Vault-backed Keenable web backend."""

from plugins.web.vault_keenable.provider import VaultKeenableWebSearchProvider


def register(ctx) -> None:
    ctx.register_web_search_provider(VaultKeenableWebSearchProvider())