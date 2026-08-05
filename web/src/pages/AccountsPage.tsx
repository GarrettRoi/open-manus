import { useCallback, useEffect, useMemo, useState } from "react";
import {
  ChevronDown,
  ChevronUp,
  ExternalLink,
  Link2,
  Plus,
  RefreshCw,
  Trash2,
  X,
} from "lucide-react";
import { api } from "@/lib/api";
import type {
  VaultCatalogEntry,
  VaultConnection,
  VaultConnectionCreate,
  VaultOverview,
} from "@/lib/api";
import { Button } from "@nous-research/ui/ui/components/button";
import { Spinner } from "@nous-research/ui/ui/components/spinner";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@nous-research/ui/ui/components/card";
import { Badge } from "@nous-research/ui/ui/components/badge";
import { Input } from "@nous-research/ui/ui/components/input";
import { Label } from "@nous-research/ui/ui/components/label";
import { Toast } from "@nous-research/ui/ui/components/toast";
import { useToast } from "@nous-research/ui/hooks/use-toast";
import { useProfileScope } from "@/contexts/useProfileScope";

/**
 * Accounts — per-agent account & API-key connections, backed by the fleet
 * credential vault. The vault holds the credentials (agents only ever proxy
 * through it); this page manages which accounts exist and which agent can
 * use each one.
 *
 * The "current agent" is the profile selected in the agent tabs / profile
 * switcher (e.g. `agent-samantha` → vault agent `samantha`).
 */

/** Map a dashboard profile name to the vault's short agent name. */
function vaultAgentName(profile: string, currentProfile: string): string {
  const p = (profile || currentProfile || "").toLowerCase();
  return p.replace(/^agent-/, "");
}

// Providers surfaced as one-click choices, in display order. Everything
// else in the catalog is still reachable via "More…".
const FEATURED = ["email", "google", "outlook", "github", "railway", "custom", "custom_oauth"];

export default function AccountsPage() {
  const { profile, currentProfile } = useProfileScope();
  const agent = vaultAgentName(profile, currentProfile);
  const { toast, showToast } = useToast();

  const [overview, setOverview] = useState<VaultOverview | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [adding, setAdding] = useState<string | null>(null); // service key
  const [busy, setBusy] = useState(false);
  const [showMore, setShowMore] = useState(false);

  const reload = useCallback(() => {
    setLoading(true);
    api
      .getVaultOverview()
      .then((o) => {
        setOverview(o);
        setError("");
      })
      .catch((e) => setError(e?.message || String(e)))
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => {
    reload();
  }, [reload]);

  const agentKnown = !!overview?.agents.includes(agent);
  const grantedIds = useMemo(
    () => new Set(overview?.grants[agent] ?? []),
    [overview, agent],
  );

  const toggleGrant = async (conn: VaultConnection, granted: boolean) => {
    if (!overview) return;
    setBusy(true);
    try {
      await api.setVaultGrant(agent, conn.id, granted);
      setOverview({
        ...overview,
        grants: {
          ...overview.grants,
          [agent]: granted
            ? [...(overview.grants[agent] ?? []), conn.id]
            : (overview.grants[agent] ?? []).filter((id) => id !== conn.id),
        },
      });
      showToast(
        granted
          ? `${agent} can now use ${conn.label || conn.id}`
          : `Removed ${conn.label || conn.id} from ${agent}`,
        "success",
      );
    } catch (e) {
      showToast(String((e as Error)?.message || e), "error");
    } finally {
      setBusy(false);
    }
  };

  const startOAuthLogin = async (conn: VaultConnection) => {
    try {
      const { url } = await api.getVaultConnectLink(conn.id);
      window.open(url, "_blank", "noopener");
      showToast(
        "Login opened in a new tab. Come back and hit Refresh when done.",
        "success",
      );
    } catch (e) {
      showToast(String((e as Error)?.message || e), "error");
    }
  };

  const deleteConnection = async (conn: VaultConnection) => {
    if (
      !window.confirm(
        `Delete "${conn.label || conn.id}" for ALL agents? This removes the stored credential.`,
      )
    )
      return;
    setBusy(true);
    try {
      await api.deleteVaultConnection(conn.id);
      showToast("Connection deleted", "success");
      reload();
    } catch (e) {
      showToast(String((e as Error)?.message || e), "error");
    } finally {
      setBusy(false);
    }
  };

  if (loading) {
    return (
      <div className="flex items-center gap-2 py-10 text-sm text-muted-foreground">
        <Spinner /> Loading accounts…
      </div>
    );
  }

  if (error || !overview) {
    return (
      <Card>
        <CardHeader>
          <CardTitle>Accounts unavailable</CardTitle>
          <CardDescription>
            {error ||
              "The credential vault could not be reached from this dashboard."}
          </CardDescription>
        </CardHeader>
        <CardContent>
          <Button onClick={reload}>
            <RefreshCw className="mr-2 h-4 w-4" /> Retry
          </Button>
        </CardContent>
      </Card>
    );
  }

  const connections = overview.connections;
  const grantedConns = connections.filter((c) => grantedIds.has(c.id));
  const otherConns = connections.filter((c) => !grantedIds.has(c.id));

  return (
    <div className="flex flex-col gap-4">
      <Toast toast={toast} />

      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <div className="text-sm text-muted-foreground">
            Managing accounts for{" "}
            <span className="font-semibold text-foreground">{agent}</span>
            {!agentKnown && (
              <span className="ml-2 text-amber-400">
                (not a known vault agent — pick an agent tab above)
              </span>
            )}
          </div>
          {overview.public_url_missing && (
            <div className="mt-1 text-xs text-amber-400">
              OAuth logins are disabled until VAULT_PUBLIC_URL is set on the
              vault service.
            </div>
          )}
        </div>
        <Button ghost onClick={reload} disabled={busy}>
          <RefreshCw className="mr-2 h-4 w-4" /> Refresh
        </Button>
      </div>

      {/* Add-account chooser */}
      <Card>
        <CardHeader>
          <CardTitle>Add an account</CardTitle>
          <CardDescription>
            Connect a login (Google, Outlook, GitHub…) or paste an API key.
            You can add the same provider more than once — e.g. a personal
            Gmail and a work Gmail — just give each one its own name.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <div className="grid grid-cols-2 gap-2 sm:grid-cols-3">
            {FEATURED.filter((k) => overview.catalog[k]).map((key) => (
              <ProviderTile
                key={key}
                label={overview.catalog[key].label}
                active={adding === key}
                onClick={() => setAdding(adding === key ? null : key)}
              />
            ))}
          </div>
          {Object.keys(overview.catalog).some((k) => !FEATURED.includes(k)) && (
            <>
              <button
                type="button"
                className="mt-3 flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground"
                onClick={() => setShowMore((v) => !v)}
              >
                {showMore ? (
                  <ChevronUp className="h-3.5 w-3.5" />
                ) : (
                  <ChevronDown className="h-3.5 w-3.5" />
                )}
                {showMore ? "Hide other providers" : "More providers…"}
              </button>
              {showMore && (
                <div className="mt-2 grid grid-cols-2 gap-2 sm:grid-cols-3">
                  {Object.keys(overview.catalog)
                    .filter((k) => !FEATURED.includes(k))
                    .map((key) => (
                      <ProviderTile
                        key={key}
                        label={overview.catalog[key].label}
                        active={adding === key}
                        onClick={() => setAdding(adding === key ? null : key)}
                      />
                    ))}
                </div>
              )}
            </>
          )}

          {adding && overview.catalog[adding] && (
            <AddConnectionForm
              agent={agent}
              serviceKey={adding}
              entry={overview.catalog[adding]}
              redirectUri={overview.redirect_uri}
              onCancel={() => setAdding(null)}
              onCreated={(needsLogin, connId) => {
                setAdding(null);
                reload();
                if (needsLogin && connId) {
                  api
                    .getVaultConnectLink(connId)
                    .then(({ url }) => window.open(url, "_blank", "noopener"))
                    .catch((e) =>
                      showToast(String((e as Error)?.message || e), "error"),
                    );
                }
              }}
              onError={(msg) => showToast(msg, "error")}
            />
          )}
        </CardContent>
      </Card>

      {/* Accounts this agent can use */}
      <Card>
        <CardHeader>
          <CardTitle>
            {agent}
            {"'"}s accounts ({grantedConns.length})
          </CardTitle>
          <CardDescription>
            Accounts and keys this agent can use through the vault. The agent
            never sees the raw credential.
          </CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col gap-2">
          {grantedConns.length === 0 && (
            <div className="text-sm text-muted-foreground">
              No accounts yet — add one above or enable one from the shared
              list below.
            </div>
          )}
          {grantedConns.map((conn) => (
            <ConnectionRow
              key={conn.id}
              conn={conn}
              granted
              busy={busy}
              onToggle={() => toggleGrant(conn, false)}
              onLogin={() => startOAuthLogin(conn)}
              onDelete={() => deleteConnection(conn)}
            />
          ))}
        </CardContent>
      </Card>

      {/* Other connections in the vault */}
      <Card>
        <CardHeader>
          <CardTitle>Shared vault ({otherConns.length})</CardTitle>
          <CardDescription>
            Accounts other agents use. Enable one to let {agent} use it too.
          </CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col gap-2">
          {otherConns.length === 0 && (
            <div className="text-sm text-muted-foreground">
              Nothing else in the vault.
            </div>
          )}
          {otherConns.map((conn) => (
            <ConnectionRow
              key={conn.id}
              conn={conn}
              granted={false}
              busy={busy}
              onToggle={() => toggleGrant(conn, true)}
              onLogin={() => startOAuthLogin(conn)}
              onDelete={() => deleteConnection(conn)}
            />
          ))}
        </CardContent>
      </Card>
    </div>
  );
}

function ProviderTile({
  label,
  active,
  onClick,
}: {
  label: string;
  active: boolean;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={
        "flex min-h-[3rem] items-center justify-center gap-1.5 rounded-lg border px-2 py-2 text-sm transition-colors " +
        (active
          ? "border-midground bg-midground/15 font-semibold text-midground"
          : "border-border text-foreground hover:bg-midground/5")
      }
    >
      <Plus className="h-4 w-4 shrink-0" />
      <span className="truncate">{label}</span>
    </button>
  );
}

function ConnectionRow({
  conn,
  granted,
  busy,
  onToggle,
  onLogin,
  onDelete,
}: {
  conn: VaultConnection;
  granted: boolean;
  busy: boolean;
  onToggle: () => void;
  onLogin: () => void;
  onDelete: () => void;
}) {
  const isOAuth = conn.auth_kind === "oauth2";
  const needsLogin = isOAuth && !conn.connected;
  return (
    <div className="flex flex-col gap-2 rounded-lg border border-border px-3 py-2.5 sm:flex-row sm:items-center">
      <div className="flex min-w-0 flex-1 items-start gap-2">
        <Link2 className="mt-0.5 h-4 w-4 shrink-0 text-text-tertiary" />
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
            <span className="truncate text-sm font-medium">
              {conn.label || conn.id}
            </span>
            <Badge>{conn.service}</Badge>
            {isOAuth ? (
              conn.connected ? (
                <Badge className="border-emerald-500/40 text-emerald-400">
                  logged in
                </Badge>
              ) : (
                <Badge className="border-amber-500/40 text-amber-400">
                  needs login
                </Badge>
              )
            ) : conn.connected ? (
              <Badge className="border-emerald-500/40 text-emerald-400">
                key set
              </Badge>
            ) : (
              <Badge className="border-amber-500/40 text-amber-400">
                no key
              </Badge>
            )}
          </div>
          <div className="truncate text-xs text-muted-foreground">
            {conn.id}
            {conn.base_url ? ` · ${conn.base_url}` : ""}
          </div>
        </div>
      </div>
      <div className="flex items-center gap-1.5 sm:shrink-0">
        {needsLogin && (
          <Button size="sm" onClick={onLogin} disabled={busy} className="flex-1 sm:flex-none">
            <ExternalLink className="mr-1 h-3.5 w-3.5" /> Log in
          </Button>
        )}
        <Button size="sm" ghost onClick={onToggle} disabled={busy} className="flex-1 sm:flex-none">
          {granted ? "Disable" : "Enable for agent"}
        </Button>
        <Button
          size="sm"
          ghost
          onClick={onDelete}
          disabled={busy}
          aria-label="Delete connection"
          className="shrink-0 text-red-400 hover:text-red-300"
        >
          <Trash2 className="h-3.5 w-3.5" />
        </Button>
      </div>
    </div>
  );
}

function AddConnectionForm({
  agent,
  serviceKey,
  entry,
  redirectUri,
  onCancel,
  onCreated,
  onError,
}: {
  agent: string;
  serviceKey: string;
  entry: VaultCatalogEntry;
  redirectUri: string;
  onCancel: () => void;
  onCreated: (needsLogin: boolean, connId: string) => void;
  onError: (msg: string) => void;
}) {
  const isOAuth = entry.auth_kind === "oauth2";
  const isEmail = entry.auth_kind === "email";
  const [name, setName] = useState("");
  const [fields, setFields] = useState<Record<string, string>>({});
  const [saving, setSaving] = useState(false);

  const set = (k: string, v: string) => setFields((f) => ({ ...f, [k]: v }));

  const submit = async () => {
    if (!name.trim()) {
      onError("Give this account a name (e.g. gmail_personal)");
      return;
    }
    const body: VaultConnectionCreate = {
      service: serviceKey,
      name: name.trim(),
      grant_agents: [agent],
      ...fields,
    };
    setSaving(true);
    try {
      const res = await api.createVaultConnection(body);
      onCreated(res.needs_login, res.connection?.id ?? "");
    } catch (e) {
      onError(String((e as Error)?.message || e));
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="mt-4 flex flex-col gap-3 rounded border border-border p-4">
      <div className="flex items-center justify-between">
        <div className="text-sm font-medium">Add {entry.label}</div>
        <Button ghost size="icon" onClick={onCancel} aria-label="Cancel">
          <X className="h-4 w-4" />
        </Button>
      </div>

      {entry.setup_help && (
        <p className="text-xs text-muted-foreground">{entry.setup_help}</p>
      )}
      {isOAuth && redirectUri && (
        <p className="text-xs text-muted-foreground">
          Redirect URL to add in the provider{"'"}s OAuth app settings:{" "}
          <code className="break-all">{redirectUri}</code>
        </p>
      )}

      <div className="grid gap-3 sm:grid-cols-2">
        <div className="flex flex-col gap-1">
          <Label>Account name</Label>
          <Input
            placeholder={
              isOAuth ? `${serviceKey}_personal` : `${serviceKey}_main`
            }
            value={name}
            onChange={(e) => setName(e.target.value)}
          />
        </div>

        {isOAuth ? (
          <>
            <div className="flex flex-col gap-1">
              <Label>Client ID</Label>
              <Input
                value={fields.client_id ?? ""}
                onChange={(e) => set("client_id", e.target.value)}
              />
            </div>
            <div className="flex flex-col gap-1">
              <Label>Client secret</Label>
              <Input
                type="password"
                value={fields.client_secret ?? ""}
                onChange={(e) => set("client_secret", e.target.value)}
              />
            </div>
          </>
        ) : isEmail ? (
          <div className="flex flex-col gap-1">
            <Label>Password (or app password)</Label>
            <Input
              type="password"
              value={fields.password ?? ""}
              onChange={(e) => set("password", e.target.value)}
            />
          </div>
        ) : (
          <div className="flex flex-col gap-1">
            <Label>API key</Label>
            <Input
              type="password"
              value={fields.api_key ?? ""}
              onChange={(e) => set("api_key", e.target.value)}
            />
          </div>
        )}

        {(entry.fields ?? []).map((f) => (
          <div className="flex flex-col gap-1" key={f.name}>
            <Label>
              {f.label}
              {f.required ? " *" : ""}
            </Label>
            <Input
              placeholder={f.placeholder ?? ""}
              value={fields[f.name] ?? ""}
              onChange={(e) => set(f.name, e.target.value)}
            />
          </div>
        ))}
      </div>

      <div className="flex flex-col-reverse gap-2 sm:flex-row sm:justify-end">
        <Button ghost onClick={onCancel} className="w-full sm:w-auto">
          Cancel
        </Button>
        <Button onClick={submit} disabled={saving} className="w-full sm:w-auto">
          {saving ? <Spinner /> : isOAuth ? "Save & log in" : "Save"}
        </Button>
      </div>
    </div>
  );
}
