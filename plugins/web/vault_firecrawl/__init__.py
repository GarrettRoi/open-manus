"""Vault-backed Firecrawl web backend."""

from plugins.web.vault_firecrawl.provider import VaultFirecrawlWebSearchProvider


def register(ctx) -> None:
    ctx.register_web_search_provider(VaultFirecrawlWebSearchProvider())