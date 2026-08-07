"""Discord /vault slash command group — admin UI for vault connections.

Kept in this file (not adapter.py) so upstream engine syncs cannot silently
wipe the vault admin surface.  adapter.py calls ``define_vault_ui_classes()``
at the same time as ``_define_discord_view_classes()``, and calls
``_register_vault_group()`` at the end of ``_register_slash_commands()``.

Secret-safety contract
----------------------
- Modal field values are NEVER logged.  Only connection IDs and auth kinds
  appear in log output from this module.
- All /vault responses are ephemeral (visible only to the invoking user).
- Nothing routes through ``_handle_message``, ``_build_slash_event``, or
  ``MessageEvent``.  The handlers call the vault admin JSON API directly.

Authorization model — FAIL CLOSED
----------------------------------
``/vault`` is **owner-only**.  The owner Discord user ID is resolved from
``DISCORD_OWNER_ID`` (the same env var used by the legacy gateway in
``gateway/platforms/discord.py``) with the same hardcoded fallback
(``"700339484507766826"``).  If ``DISCORD_OWNER_ID`` is explicitly set to an
empty string the gate rejects everyone.

Critically, the DISCORD_ALLOWED_USERS / DISCORD_ALLOWED_ROLES / channel
allowlist is **never consulted** for /vault.  Those lists can authorize whole
roles, channels, or wildcards; vault admin must always be single-owner only.
``_vault_owner_gate`` intentionally does NOT call
``_check_slash_authorization``.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

# Service names / auth kinds that are redirected to the browser dashboard.
#
# The edit modal checks against auth_kind; the add modal checks against the
# service name the user typed.  Only bearer and oauth2 are supported in the
# Discord modal (5-field limit).
#
# "header" is in this set because header-kind connections require
# header_name and prefix fields that the 5-field modal cannot fit.
# For edit, any existing header-kind connection is always redirected to the
# dashboard.  For add, the service name "header" is uncommon (users normally
# type a service like "rapidapi"); post-add header-kind detection is handled
# in VaultAddModal.on_submit with a note pointing to the dashboard.
_DASHBOARD_ONLY_KINDS = frozenset({
    "email", "apple", "google", "microsoft", "github", "slack", "header",
})

# Module-level class references — populated by define_vault_ui_classes().
# They are None until discord.py (or its mock) is available.
VaultAddModal = None
VaultEditModal = None
VaultDeleteConfirmView = None

# ---------------------------------------------------------------------------
# Lazy vault client singleton
# ---------------------------------------------------------------------------

_vault_client = None


def _get_vault_client():
    """Return the process-local VaultAdminClient singleton.

    Imported and instantiated lazily so this module loads cleanly even when
    vault_admin_client.py is not on sys.path (e.g. in non-Discord contexts).
    """
    global _vault_client
    if _vault_client is None:
        try:
            from vault_admin_client import VaultAdminClient
        except ImportError:
            from .vault_admin_client import VaultAdminClient
        _vault_client = VaultAdminClient()
    return _vault_client


# Convenient module-level alias used by autocomplete callbacks in adapter.py.
# The name ``vault_client`` is imported by _register_vault_group.
@property
def vault_client():  # type: ignore[override]
    return _get_vault_client()


# ---------------------------------------------------------------------------
# Helper utilities
# ---------------------------------------------------------------------------

def _requires_dashboard(service_or_kind: str) -> bool:
    """Return True when service/kind needs the browser dashboard for credentials."""
    return service_or_kind.lower() in _DASHBOARD_ONLY_KINDS


def _dashboard_pointer_message(service_or_kind: str) -> str:
    """Return an ephemeral message redirecting the user to the browser dashboard."""
    base_url = os.getenv("VAULT_BASE_URL", "").strip().rstrip("/")
    if base_url:
        link = f"\n\nOpen the dashboard: {base_url}"
    else:
        link = "\n\nSet `VAULT_BASE_URL` to see the dashboard URL."
    return (
        f"🔒 **{service_or_kind.capitalize()} connections** require platform-specific "
        f"login flows or more credential fields than Discord modals support.  "
        f"Please use the vault browser dashboard to add or edit this connection.{link}"
    )


# ---------------------------------------------------------------------------
# Owner resolution — single source of truth for all vault authorization
# ---------------------------------------------------------------------------

# Same env var and same hardcoded fallback as gateway/platforms/discord.py
# line 124.  Using the same source keeps vault access consistent with the
# fleet-wide owner definition without introducing a new env var.
_VAULT_OWNER_FALLBACK = "700339484507766826"


def _resolve_vault_owner_id() -> str:
    """Return the authoritative vault owner Discord user ID.

    Source: ``DISCORD_OWNER_ID`` env var, with fallback
    ``"700339484507766826"`` (Garrett's Discord ID, same as
    ``gateway/platforms/discord.py``).

    Returns an empty string only when ``DISCORD_OWNER_ID`` is explicitly set
    to the empty string, which causes the gate to reject everyone (fail
    closed).  Callers must treat an empty return as "no owner configured —
    reject all".
    """
    raw = os.environ.get("DISCORD_OWNER_ID")
    if raw is None:
        # Env var absent → use hardcoded fallback.
        return _VAULT_OWNER_FALLBACK
    stripped = raw.strip()
    # Env var present but empty → explicit "no owner" → fail closed.
    return stripped


async def _vault_owner_gate(adapter, interaction) -> bool:  # noqa: ARG001
    """Fail-closed authorization gate for all /vault subcommands.

    ONLY the Discord user whose ID matches ``_resolve_vault_owner_id()`` may
    proceed.  The DISCORD_ALLOWED_USERS / DISCORD_ALLOWED_ROLES / channel
    allowlist is **never consulted** — those lists can authorize whole roles,
    channels, or wildcards, which must never grant vault admin access.

    ``adapter`` is accepted but intentionally unused; the parameter is kept
    so call-sites do not need updating if future auditing needs are added.

    Returns ``True`` to proceed.  Returns ``False`` after sending an ephemeral
    rejection — the caller MUST stop on ``False``.  No vault API call is made
    on ``False``.
    """
    owner_id = _resolve_vault_owner_id()
    user_id = str(getattr(getattr(interaction, "user", None), "id", ""))

    if not owner_id:
        # DISCORD_OWNER_ID explicitly set to empty — fail closed for everyone.
        try:
            await interaction.response.send_message(
                "🔒 `/vault` is not configured: no owner ID set. "
                "Set `DISCORD_OWNER_ID` to enable vault management.",
                ephemeral=True,
            )
        except Exception as e:
            logger.debug("[vault-ui] owner-gate not-configured send failed: %s", e)
        return False

    if user_id == owner_id:
        return True

    try:
        await interaction.response.send_message(
            "🔒 `/vault` is restricted to the vault owner.",
            ephemeral=True,
        )
    except Exception as e:
        logger.debug("[vault-ui] owner-gate rejection send failed: %s", e)
    return False


# ---------------------------------------------------------------------------
# UI class definitions (called after discord.py is available)
# ---------------------------------------------------------------------------

def define_vault_ui_classes() -> None:
    """Define vault modal/view classes and register them as module globals.

    Called from ``_define_discord_view_classes()`` in adapter.py so these are
    (re)defined whenever ``DISCORD_AVAILABLE`` becomes True, including after a
    lazy discord.py install.  Idempotent — safe to call more than once.
    """
    global VaultAddModal, VaultEditModal, VaultDeleteConfirmView

    import discord

    # ------------------------------------------------------------------
    # VaultAddModal
    # ------------------------------------------------------------------

    class _VaultAddModal(discord.ui.Modal, title="➕ Add Vault Connection"):
        """Modal for adding a new bearer or OAuth2 vault connection.

        Supports bearer (api_key) and OAuth2 (client_id + client_secret) auth
        kinds.  Services that use header, email, apple, google, microsoft, or
        other auth kinds with more than five credential fields are redirected to
        the browser dashboard instead of calling the vault API.

        Modal field values are NEVER logged — they may contain credentials.
        """

        name_field = discord.ui.TextInput(
            label="Connection Name",
            placeholder="e.g. openai  (becomes the connection ID)",
            max_length=80,
        )
        service_field = discord.ui.TextInput(
            label="Service",
            placeholder="e.g. openai, anthropic, elevenlabs, custom",
            max_length=80,
        )
        credential_field = discord.ui.TextInput(
            label="API Key / Client ID",
            placeholder="API key for bearer auth, or OAuth2 client_id",
            max_length=512,
        )
        client_secret_field = discord.ui.TextInput(
            label="Client Secret  (OAuth2 only — blank for bearer)",
            placeholder="Leave blank for bearer auth (API key only)",
            required=False,
            max_length=512,
        )
        base_url_field = discord.ui.TextInput(
            label="Base URL  (blank = use catalog default)",
            placeholder="e.g. https://api.openai.com",
            required=False,
            max_length=512,
        )

        def __init__(self, adapter_ref, invoker_id: str) -> None:
            super().__init__()
            self._adapter_ref = adapter_ref
            self._invoker_id = invoker_id

        async def on_submit(self, interaction: discord.Interaction) -> None:
            # Guard: only the original invoker may submit.
            user_id = str(getattr(getattr(interaction, "user", None), "id", ""))
            if user_id != self._invoker_id:
                await interaction.response.send_message(
                    "You cannot submit another user's form.", ephemeral=True
                )
                return

            # Read field values — NEVER log these raw values.
            name_val = (self.name_field.value or "").strip()
            service_val = (self.service_field.value or "").strip().lower()
            credential_val = (self.credential_field.value or "").strip()
            secret_val = (self.client_secret_field.value or "").strip()
            base_url_val = (self.base_url_field.value or "").strip()

            if not name_val or not service_val or not credential_val:
                await interaction.response.send_message(
                    "❌ Connection name, service, and credential are required.",
                    ephemeral=True,
                )
                return

            # Dashboard-only services: redirect without calling vault API.
            if _requires_dashboard(service_val):
                await interaction.response.send_message(
                    _dashboard_pointer_message(service_val), ephemeral=True
                )
                return

            await interaction.response.defer(ephemeral=True)

            # Use the connection name as the human-readable label too.
            # The user can update the label separately in the dashboard.
            body: dict = {"name": name_val, "label": name_val, "service": service_val}
            if secret_val:
                # OAuth2: client_id + client_secret
                body["client_id"] = credential_val
                body["client_secret"] = secret_val
            else:
                # Bearer: api_key
                body["api_key"] = credential_val
            if base_url_val:
                body["base_url"] = base_url_val

            try:
                from vault_admin_client import VaultClientError
            except ImportError:
                from .vault_admin_client import VaultClientError

            try:
                result = await _get_vault_client().add(body)
                conn = (result or {}).get("connection") or {}
                conn_id = conn.get("id", name_val.upper())
                needs_login = (result or {}).get("needs_login", False)
                auth_kind = conn.get("auth_kind", "")
                msg = (
                    f"✅ Connection **{conn_id}** created.\n\n"
                    f"Agents with a grant will see it after their next sync "
                    f"(within 5 min), or immediately if they run "
                    f"`vault(action='refresh')`.  Use `/vault grant <agent> {conn_id}` "
                    f"to give an agent access."
                )
                if needs_login:
                    msg += (
                        f"\n\nThis service requires OAuth authorization. "
                        f"Use `/vault connect {conn_id}` to get the login URL."
                    )
                if auth_kind == "header":
                    # Header-kind connections also need header_name / prefix
                    # configured, which the modal cannot collect.  Point to
                    # the dashboard for fine-tuning.
                    msg += (
                        f"\n\n⚠️ This connection uses **header auth**.  "
                        f"The `header_name` and `prefix` fields were set to "
                        f"their service defaults — use the vault dashboard to "
                        f"update them if needed."
                    )
            except VaultClientError as exc:
                msg = f"❌ Could not create connection: {exc}"
            except Exception as exc:
                logger.error("[vault-ui] add connection failed for service=%s: %s", service_val, exc)
                msg = f"❌ Unexpected error: {exc}"

            try:
                await interaction.edit_original_response(content=msg)
            except Exception as e:
                logger.debug("[vault-ui] add response edit failed: %s", e)
                try:
                    await interaction.followup.send(msg, ephemeral=True)
                except Exception:
                    pass

        async def on_error(
            self, interaction: discord.Interaction, error: Exception
        ) -> None:
            logger.error("[vault-ui] VaultAddModal.on_error: %s", error)
            try:
                await interaction.response.send_message(
                    "❌ An error occurred while processing the form.", ephemeral=True
                )
            except Exception:
                pass

    # ------------------------------------------------------------------
    # VaultEditModal
    # ------------------------------------------------------------------

    class _VaultEditModal(discord.ui.Modal, title="✏️ Edit Vault Connection"):
        """Partial-update modal for an existing vault connection.

        All fields are optional: blank or absent keeps the existing value.
        For dashboard-only auth kinds, ``on_submit`` redirects to the browser
        dashboard instead of calling the vault API.

        Modal field values are NEVER logged — they may contain credentials.
        """

        label_field = discord.ui.TextInput(
            label="Label  (blank = keep current)",
            placeholder="Human-readable connection name",
            required=False,
            max_length=120,
        )
        credential_field = discord.ui.TextInput(
            label="API Key / Client ID  (blank = keep current)",
            placeholder="Leave blank to keep the existing credential",
            required=False,
            max_length=512,
        )
        client_secret_field = discord.ui.TextInput(
            label="Client Secret  (blank = keep current)",
            placeholder="Leave blank to keep the existing secret",
            required=False,
            max_length=512,
        )
        base_url_field = discord.ui.TextInput(
            label="Base URL  (blank = keep current)",
            placeholder="e.g. https://api.openai.com",
            required=False,
            max_length=512,
        )
        description_field = discord.ui.TextInput(
            label="Description  (blank = keep current)",
            placeholder="What this connection is used for",
            required=False,
            style=discord.TextStyle.paragraph,
            max_length=512,
        )

        def __init__(
            self,
            adapter_ref,
            conn_id: str,
            auth_kind: str,
            invoker_id: str,
        ) -> None:
            super().__init__()
            self._adapter_ref = adapter_ref
            self.conn_id = conn_id
            self._auth_kind = auth_kind
            self._invoker_id = invoker_id

        async def on_submit(self, interaction: discord.Interaction) -> None:
            user_id = str(getattr(getattr(interaction, "user", None), "id", ""))
            if user_id != self._invoker_id:
                await interaction.response.send_message(
                    "You cannot submit another user's form.", ephemeral=True
                )
                return

            # Dashboard-only auth kinds: redirect.
            if _requires_dashboard(self._auth_kind):
                await interaction.response.send_message(
                    _dashboard_pointer_message(self._auth_kind), ephemeral=True
                )
                return

            await interaction.response.defer(ephemeral=True)

            # Build the partial-update body — blank field = keep existing.
            # NEVER log the credential values.
            body: dict = {}
            label_val = (self.label_field.value or "").strip()
            if label_val:
                body["label"] = label_val
            cred_val = (self.credential_field.value or "").strip()
            if cred_val:
                if self._auth_kind == "oauth2":
                    body["client_id"] = cred_val
                else:
                    body["api_key"] = cred_val
            secret_val = (self.client_secret_field.value or "").strip()
            if secret_val:
                body["client_secret"] = secret_val
            base_url_val = (self.base_url_field.value or "").strip()
            if base_url_val:
                body["base_url"] = base_url_val
            # description: key absent = keep existing; key present = update.
            # In real discord.py an untouched optional TextInput always yields
            # "" (empty string), never None.  Sending description:"" would
            # silently erase the stored value, so we treat blank as "keep".
            desc_val = (self.description_field.value or "").strip()
            if desc_val:
                body["description"] = desc_val

            if not body:
                try:
                    await interaction.edit_original_response(
                        content="No changes submitted — all fields were blank."
                    )
                except Exception:
                    pass
                return

            try:
                from vault_admin_client import VaultClientError
            except ImportError:
                from .vault_admin_client import VaultClientError

            try:
                result = await _get_vault_client().update(self.conn_id, body)
                conn = (result or {}).get("connection") or {}
                updated_id = conn.get("id", self.conn_id)
                msg = f"✅ Connection **{updated_id}** updated."
            except VaultClientError as exc:
                msg = f"❌ Could not update connection: {exc}"
            except Exception as exc:
                logger.error(
                    "[vault-ui] edit failed for conn_id=%s: %s", self.conn_id, exc
                )
                msg = f"❌ Unexpected error: {exc}"

            try:
                await interaction.edit_original_response(content=msg)
            except Exception as e:
                logger.debug("[vault-ui] edit response edit failed: %s", e)
                try:
                    await interaction.followup.send(msg, ephemeral=True)
                except Exception:
                    pass

        async def on_error(
            self, interaction: discord.Interaction, error: Exception
        ) -> None:
            logger.error("[vault-ui] VaultEditModal.on_error: %s", error)
            try:
                await interaction.response.send_message(
                    "❌ An error occurred while processing the form.", ephemeral=True
                )
            except Exception:
                pass

    # ------------------------------------------------------------------
    # VaultDeleteConfirmView
    # ------------------------------------------------------------------

    class _VaultDeleteConfirmView(discord.ui.View):
        """Two-button ephemeral confirm dialog for /vault delete.

        Only the user who invoked /vault delete may click either button.
        Times out after 2 minutes; a timed-out view records the fact in the
        message so the owner knows to re-run the command if needed.
        """

        def __init__(self, conn_id: str, invoker_id: str) -> None:
            super().__init__(timeout=120)
            self.conn_id = conn_id
            self._invoker_id = invoker_id
            self.resolved = False

        def _check_invoker(self, interaction: discord.Interaction) -> bool:
            uid = str(getattr(getattr(interaction, "user", None), "id", ""))
            return uid == self._invoker_id

        @discord.ui.button(label="🗑 Confirm Delete", style=discord.ButtonStyle.danger)
        async def confirm_btn(
            self, interaction: discord.Interaction, button: discord.ui.Button
        ) -> None:
            if self.resolved:
                await interaction.response.send_message(
                    "Already resolved.", ephemeral=True
                )
                return
            if not self._check_invoker(interaction):
                await interaction.response.send_message(
                    "Only the user who ran /vault delete may confirm.",
                    ephemeral=True,
                )
                return
            self.resolved = True
            for child in self.children:
                child.disabled = True
            await interaction.response.edit_message(
                content=f"⏳ Deleting **{self.conn_id}**…", view=self
            )

            try:
                from vault_admin_client import VaultClientError
            except ImportError:
                from .vault_admin_client import VaultClientError

            try:
                await _get_vault_client().delete(self.conn_id)
                msg = f"✅ Connection **{self.conn_id}** deleted."
            except VaultClientError as exc:
                msg = f"❌ Delete failed: {exc}"
            except Exception as exc:
                logger.error(
                    "[vault-ui] delete failed for conn_id=%s: %s", self.conn_id, exc
                )
                msg = f"❌ Unexpected error: {exc}"
            try:
                await interaction.edit_original_response(content=msg, view=None)
            except Exception as e:
                logger.debug("[vault-ui] delete response edit failed: %s", e)

        @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
        async def cancel_btn(
            self, interaction: discord.Interaction, button: discord.ui.Button
        ) -> None:
            if self.resolved:
                await interaction.response.send_message(
                    "Already resolved.", ephemeral=True
                )
                return
            if not self._check_invoker(interaction):
                await interaction.response.send_message(
                    "Only the user who ran /vault delete may cancel.",
                    ephemeral=True,
                )
                return
            self.resolved = True
            for child in self.children:
                child.disabled = True
            await interaction.response.edit_message(
                content=f"Cancelled — **{self.conn_id}** was not deleted.",
                view=None,
            )

        async def on_timeout(self) -> None:
            self.resolved = True
            for child in self.children:
                child.disabled = True
            msg_obj = getattr(self, "_message", None)
            if msg_obj:
                try:
                    await msg_obj.edit(
                        content=(
                            f"⏱ Delete confirmation timed out — "
                            f"**{self.conn_id}** was NOT deleted."
                        ),
                        view=self,
                    )
                except Exception:
                    pass

    VaultAddModal = _VaultAddModal
    VaultEditModal = _VaultEditModal
    VaultDeleteConfirmView = _VaultDeleteConfirmView


# ---------------------------------------------------------------------------
# /vault subcommand handlers
# ---------------------------------------------------------------------------

async def handle_vault_list(adapter, interaction) -> None:
    """Handle /vault list — show all connections with their auth kind and grants."""
    if not await _vault_owner_gate(adapter, interaction):
        return
    await interaction.response.defer(ephemeral=True)

    try:
        from vault_admin_client import VaultClientError
    except ImportError:
        from .vault_admin_client import VaultClientError

    try:
        data = await _get_vault_client().overview()
        connections = data.get("connections", [])
        grants = data.get("grants", {})
        if not connections:
            msg = "No vault connections configured yet."
        else:
            lines = ["**🔐 Vault Connections**\n"]
            for conn in sorted(connections, key=lambda c: c.get("id", "")):
                cid = conn.get("id", "?")
                kind = conn.get("auth_kind", "?")
                status = conn.get("status", "?")
                grantees = sorted(a for a, cids in grants.items() if cid in (cids or []))
                granted_str = (
                    f"\n  └ granted to: {', '.join(grantees)}"
                    if grantees else ""
                )
                lines.append(f"• **{cid}** `{kind}` · {status}{granted_str}")
            msg = "\n".join(lines)[:1900]
    except VaultClientError as exc:
        msg = f"❌ Could not fetch vault overview: {exc}"
    except Exception as exc:
        logger.error("[vault-ui] list failed: %s", exc)
        msg = f"❌ Unexpected error: {exc}"

    try:
        await interaction.edit_original_response(content=msg)
    except Exception as e:
        logger.debug("[vault-ui] list response failed: %s", e)


async def handle_vault_add(adapter, interaction) -> None:
    """Handle /vault add — open the add-connection modal."""
    if not await _vault_owner_gate(adapter, interaction):
        return
    if VaultAddModal is None:
        await interaction.response.send_message(
            "❌ Vault UI is not initialized. Try restarting the bot.",
            ephemeral=True,
        )
        return
    invoker_id = str(getattr(getattr(interaction, "user", None), "id", ""))
    modal = VaultAddModal(adapter_ref=adapter, invoker_id=invoker_id)
    await interaction.response.send_modal(modal)


async def handle_vault_edit(adapter, interaction, connection: str) -> None:
    """Handle /vault edit <connection> — look up auth kind then open edit modal."""
    if not await _vault_owner_gate(adapter, interaction):
        return

    conn_id = (connection or "").strip().upper()
    if not conn_id:
        await interaction.response.send_message(
            "Please provide a connection ID to edit.", ephemeral=True
        )
        return

    if VaultEditModal is None:
        await interaction.response.send_message(
            "❌ Vault UI is not initialized. Try restarting the bot.",
            ephemeral=True,
        )
        return

    try:
        from vault_admin_client import VaultClientError
    except ImportError:
        from .vault_admin_client import VaultClientError

    # Fetch connection to get auth_kind before opening the modal.
    try:
        data = await _get_vault_client().overview()
        conn_map = {c["id"]: c for c in data.get("connections", [])}
        conn = conn_map.get(conn_id)
        if conn is None:
            await interaction.response.send_message(
                f"❌ Connection **{conn_id}** not found.", ephemeral=True
            )
            return
        auth_kind = conn.get("auth_kind", "bearer")
    except VaultClientError as exc:
        await interaction.response.send_message(
            f"❌ Could not fetch connection info: {exc}", ephemeral=True
        )
        return
    except Exception as exc:
        logger.error("[vault-ui] edit pre-fetch failed for conn_id=%s: %s", conn_id, exc)
        await interaction.response.send_message(
            f"❌ Unexpected error fetching connection: {exc}", ephemeral=True
        )
        return

    # Dashboard-only connections: redirect without showing a modal.
    if _requires_dashboard(auth_kind):
        await interaction.response.send_message(
            _dashboard_pointer_message(auth_kind), ephemeral=True
        )
        return

    invoker_id = str(getattr(getattr(interaction, "user", None), "id", ""))
    modal = VaultEditModal(
        adapter_ref=adapter,
        conn_id=conn_id,
        auth_kind=auth_kind,
        invoker_id=invoker_id,
    )
    await interaction.response.send_modal(modal)


async def handle_vault_delete(adapter, interaction, connection: str) -> None:
    """Handle /vault delete <connection> — show confirmation buttons."""
    if not await _vault_owner_gate(adapter, interaction):
        return

    conn_id = (connection or "").strip().upper()
    if not conn_id:
        await interaction.response.send_message(
            "Please provide a connection ID to delete.", ephemeral=True
        )
        return

    if VaultDeleteConfirmView is None:
        await interaction.response.send_message(
            "❌ Vault UI is not initialized. Try restarting the bot.",
            ephemeral=True,
        )
        return

    invoker_id = str(getattr(getattr(interaction, "user", None), "id", ""))
    view = VaultDeleteConfirmView(conn_id=conn_id, invoker_id=invoker_id)
    await interaction.response.send_message(
        (
            f"⚠️ Delete vault connection **{conn_id}**?\n\n"
            "This will permanently remove the connection and revoke all agent "
            "grants.  This action cannot be undone."
        ),
        view=view,
        ephemeral=True,
    )


async def _handle_grant_revoke(
    adapter, interaction, agent: str, connection: str, *, granted: bool
) -> None:
    """Shared logic for /vault grant and /vault revoke."""
    if not await _vault_owner_gate(adapter, interaction):
        return

    agent_name = (agent or "").strip()
    conn_id = (connection or "").strip().upper()
    if not agent_name or not conn_id:
        await interaction.response.send_message(
            "Please provide both an agent name and a connection ID.",
            ephemeral=True,
        )
        return

    await interaction.response.defer(ephemeral=True)

    try:
        from vault_admin_client import VaultClientError
    except ImportError:
        from .vault_admin_client import VaultClientError

    verb = "grant" if granted else "revoke"
    try:
        await _get_vault_client().set_grant(agent_name, conn_id, granted)
        if granted:
            msg = f"✅ Granted **{agent_name}** access to **{conn_id}**."
        else:
            msg = f"✅ Revoked **{agent_name}**'s access to **{conn_id}**."
    except VaultClientError as exc:
        msg = f"❌ Could not {verb} access: {exc}"
    except Exception as exc:
        logger.error(
            "[vault-ui] %s failed for agent=%s conn=%s: %s",
            verb, agent_name, conn_id, exc,
        )
        msg = f"❌ Unexpected error: {exc}"

    try:
        await interaction.edit_original_response(content=msg)
    except Exception as e:
        logger.debug("[vault-ui] %s response failed: %s", verb, e)


async def handle_vault_grant(adapter, interaction, agent: str, connection: str) -> None:
    """Handle /vault grant <agent> <connection>."""
    await _handle_grant_revoke(adapter, interaction, agent, connection, granted=True)


async def handle_vault_revoke(adapter, interaction, agent: str, connection: str) -> None:
    """Handle /vault revoke <agent> <connection>."""
    await _handle_grant_revoke(adapter, interaction, agent, connection, granted=False)


async def handle_vault_connect(adapter, interaction, connection: str) -> None:
    """Handle /vault connect <connection> — return the OAuth authorize URL."""
    if not await _vault_owner_gate(adapter, interaction):
        return

    conn_id = (connection or "").strip().upper()
    if not conn_id:
        await interaction.response.send_message(
            "Please provide a connection ID.", ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=True)

    try:
        from vault_admin_client import VaultClientError
    except ImportError:
        from .vault_admin_client import VaultClientError

    try:
        url = await _get_vault_client().connect_link(conn_id)
        msg = (
            f"🔗 **OAuth Authorization URL for {conn_id}**\n\n"
            f"Open this URL in your browser to authorize the connection:\n"
            f"{url}\n\n"
            "_This link may expire — run `/vault connect` again if needed._"
        )[:1900]
    except VaultClientError as exc:
        msg = f"❌ Could not get connect link: {exc}"
    except Exception as exc:
        logger.error(
            "[vault-ui] connect-link failed for conn_id=%s: %s", conn_id, exc
        )
        msg = f"❌ Unexpected error: {exc}"

    try:
        await interaction.edit_original_response(content=msg)
    except Exception as e:
        logger.debug("[vault-ui] connect response failed: %s", e)
