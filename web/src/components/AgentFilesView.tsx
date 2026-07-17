import { useCallback, useEffect, useRef, useState } from "react";
import {
  ArrowUp,
  Download,
  FileIcon,
  Folder,
  Pencil,
  RefreshCw,
  Trash2,
  Upload,
  Users,
} from "lucide-react";
import { Badge } from "@nous-research/ui/ui/components/badge";
import { Button } from "@nous-research/ui/ui/components/button";
import { Card, CardContent } from "@nous-research/ui/ui/components/card";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@nous-research/ui/ui/components/dialog";
import { Spinner } from "@nous-research/ui/ui/components/spinner";
import { useToast } from "@nous-research/ui/hooks/use-toast";
import { Toast } from "@nous-research/ui/ui/components/toast";
import { DeleteConfirmDialog } from "@/components/DeleteConfirmDialog";
import { api } from "@/lib/api";
import type {
  AgentFileArea,
  AgentFileEntry,
  AgentFilesAgent,
  AgentFilesListResponse,
} from "@/lib/api";

const DATE_FORMAT = new Intl.DateTimeFormat(undefined, {
  dateStyle: "medium",
  timeStyle: "short",
});

const TEXT_EDIT_MAX_BYTES = 1024 * 1024;

function formatBytes(size: number | null): string {
  if (size === null) return "-";
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / (1024 * 1024)).toFixed(1)} MB`;
}

function formatSyncTime(iso: string | null): string {
  if (!iso) return "never";
  const parsed = new Date(iso);
  if (Number.isNaN(parsed.getTime())) return "unknown";
  return DATE_FORMAT.format(parsed);
}

function downloadDataUrl(dataUrl: string, name: string) {
  const link = document.createElement("a");
  link.href = dataUrl;
  link.download = name || "download";
  document.body.appendChild(link);
  link.click();
  link.remove();
}

function decodeDataUrlText(dataUrl: string): string | null {
  const comma = dataUrl.indexOf(",");
  if (comma < 0) return null;
  try {
    const bytes = Uint8Array.from(atob(dataUrl.slice(comma + 1)), (c) =>
      c.charCodeAt(0),
    );
    return new TextDecoder("utf-8", { fatal: true }).decode(bytes);
  } catch {
    return null;
  }
}

function fileToDataUrl(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result));
    reader.onerror = () => reject(reader.error ?? new Error("Read failed"));
    reader.readAsDataURL(file);
  });
}

export function AgentFilesView() {
  const { toast, showToast } = useToast();
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const [agents, setAgents] = useState<AgentFilesAgent[]>([]);
  const [redisConnected, setRedisConnected] = useState(true);
  const [selectedAgent, setSelectedAgent] = useState<string | null>(null);
  const [listing, setListing] = useState<AgentFilesListResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [pendingDelete, setPendingDelete] = useState<AgentFileEntry | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [editor, setEditor] = useState<{
    area: AgentFileArea;
    path: string;
    name: string;
    content: string;
  } | null>(null);
  const [saving, setSaving] = useState(false);

  const loadAgents = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await api.listAgentFileAgents();
      setAgents(res.agents);
      setRedisConnected(res.redis_connected);
    } catch (e) {
      setError(String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  const loadListing = useCallback(async (agent: string, path = "") => {
    setLoading(true);
    setError(null);
    try {
      const res = await api.listAgentFiles(agent, path);
      setListing(res);
    } catch (e) {
      setError(String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (selectedAgent) {
      void loadListing(selectedAgent);
    } else {
      setListing(null);
      void loadAgents();
    }
  }, [selectedAgent, loadAgents, loadListing]);

  const refresh = () => {
    if (selectedAgent) void loadListing(selectedAgent, listing?.path ?? "");
    else void loadAgents();
  };

  const openEntry = async (entry: AgentFileEntry) => {
    if (!selectedAgent) return;
    if (entry.is_directory) {
      void loadListing(selectedAgent, entry.path);
      return;
    }
    try {
      const res = await api.readAgentFile(selectedAgent, entry.area, entry.path);
      const text =
        res.size <= TEXT_EDIT_MAX_BYTES ? decodeDataUrlText(res.data_url) : null;
      if (text !== null) {
        setEditor({ area: entry.area, path: entry.path, name: entry.name, content: text });
      } else {
        downloadDataUrl(res.data_url, entry.name);
      }
    } catch (e) {
      showToast(String(e));
    }
  };

  const download = async (entry: AgentFileEntry) => {
    if (!selectedAgent) return;
    try {
      const res = await api.readAgentFile(selectedAgent, entry.area, entry.path);
      downloadDataUrl(res.data_url, entry.name);
    } catch (e) {
      showToast(String(e));
    }
  };

  const saveEditor = async () => {
    if (!selectedAgent || !editor) return;
    setSaving(true);
    try {
      const res = await api.writeAgentFile(
        selectedAgent,
        editor.area,
        editor.path,
        editor.content,
      );
      showToast(
        res.pushed_to_agent
          ? `Saved ${editor.name} — the agent will pick it up on its next sync.`
          : `Saved ${editor.name} locally (Redis unreachable; ships on next deploy).`,
      );
      setEditor(null);
      void loadListing(selectedAgent, listing?.path ?? "");
    } catch (e) {
      showToast(String(e));
    } finally {
      setSaving(false);
    }
  };

  const uploadFiles = async (files: FileList | null) => {
    if (!selectedAgent || !files?.length) return;
    setUploading(true);
    try {
      for (const file of Array.from(files)) {
        const dataUrl = await fileToDataUrl(file);
        const base = listing?.path ? `${listing.path}/` : "";
        await api.uploadAgentFile(selectedAgent, `${base}${file.name}`, dataUrl);
      }
      showToast(
        selectedAgent === "shared"
          ? `Uploaded ${files.length} file${files.length > 1 ? "s" : ""} to the shared folder — all agents get them on next sync.`
          : `Uploaded ${files.length} file${files.length > 1 ? "s" : ""} to ${selectedAgent}'s workspace.`,
      );
      void loadListing(selectedAgent, listing?.path ?? "");
    } catch (e) {
      showToast(String(e));
    } finally {
      setUploading(false);
      if (fileInputRef.current) fileInputRef.current.value = "";
    }
  };

  const confirmDelete = async () => {
    if (!selectedAgent || !pendingDelete) return;
    setDeleting(true);
    try {
      await api.deleteAgentFile(selectedAgent, pendingDelete.path);
      showToast(`Deleted ${pendingDelete.name}. The agent removes its copy on next sync.`);
      setPendingDelete(null);
      void loadListing(selectedAgent, listing?.path ?? "");
    } catch (e) {
      showToast(String(e));
    } finally {
      setDeleting(false);
    }
  };

  const goUp = () => {
    if (!selectedAgent) return;
    const current = listing?.path ?? "";
    if (!current) {
      setSelectedAgent(null);
      return;
    }
    const idx = current.lastIndexOf("/");
    void loadListing(selectedAgent, idx > 0 ? current.slice(0, idx) : "");
  };

  // ---- agent grid ----
  if (!selectedAgent) {
    return (
      <div className="space-y-4">
        <Toast toast={toast} />
        {!redisConnected && (
          <Card>
            <CardContent className="py-3 text-sm text-muted-foreground">
              Redis is not reachable — live workspace mirrors and sync times are
              unavailable. Deploy files can still be browsed once you pick an agent.
            </CardContent>
          </Card>
        )}
        {error && (
          <Card>
            <CardContent className="py-3 text-sm text-destructive">{error}</CardContent>
          </Card>
        )}
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
          {agents.map((agent) => (
            <Card
              key={agent.name}
              className="cursor-pointer transition-colors hover:bg-accent/50"
              onClick={() => setSelectedAgent(agent.name)}
            >
              <CardContent className="flex items-center gap-3 py-4">
                {agent.shared ? (
                  <Folder className="size-5 shrink-0 text-muted-foreground" />
                ) : (
                  <Users className="size-5 shrink-0 text-muted-foreground" />
                )}
                <div className="min-w-0">
                  <div className="truncate font-medium capitalize">
                    {agent.shared ? "Shared" : agent.name}
                  </div>
                  <div className="truncate text-xs text-muted-foreground">
                    {agent.shared
                      ? "One folder synced to every agent"
                      : `Last sync: ${formatSyncTime(agent.last_sync)}`}
                  </div>
                </div>
              </CardContent>
            </Card>
          ))}
        </div>
        {loading && agents.length === 0 && (
          <div className="flex justify-center py-8">
            <Spinner />
          </div>
        )}
      </div>
    );
  }

  // ---- per-agent listing ----
  return (
    <div className="space-y-4">
      <Toast toast={toast} />
      <div className="flex flex-wrap items-center gap-2">
        <Button ghost size="icon" type="button" onClick={goUp} aria-label="Go up">
          <ArrowUp />
        </Button>
        <Badge tone="outline" className="capitalize">
          {selectedAgent}
        </Badge>
        {listing?.path && (
          <Badge tone="outline" className="max-w-[18rem] truncate">
            /{listing.path}
          </Badge>
        )}
        {selectedAgent !== "shared" && (
          <Badge tone="outline" className="text-xs">
            Last sync: {formatSyncTime(listing?.last_sync ?? null)}
          </Badge>
        )}
        <div className="ml-auto flex items-center gap-2">
          <Button
            type="button"
            onClick={() => fileInputRef.current?.click()}
            disabled={uploading}
            prefix={uploading ? <Spinner /> : <Upload />}
          >
            Upload
          </Button>
          <Button
            ghost
            size="icon"
            type="button"
            onClick={refresh}
            disabled={loading}
            aria-label="Refresh"
          >
            {loading ? <Spinner /> : <RefreshCw />}
          </Button>
        </div>
        <input
          ref={fileInputRef}
          type="file"
          multiple
          hidden
          onChange={(e) => void uploadFiles(e.target.files)}
        />
      </div>

      {listing?.redis_error && (
        <Card>
          <CardContent className="py-3 text-sm text-muted-foreground">
            Workspace mirror unavailable: {listing.redis_error}
          </CardContent>
        </Card>
      )}
      {error && (
        <Card>
          <CardContent className="py-3 text-sm text-destructive">{error}</CardContent>
        </Card>
      )}

      <Card>
        <CardContent className="p-0">
          <table className="w-full text-sm">
            <tbody>
              {(listing?.entries ?? []).map((entry) => (
                <tr
                  key={`${entry.area}:${entry.path}`}
                  className="cursor-pointer border-b last:border-b-0 hover:bg-accent/40"
                  onClick={() => void openEntry(entry)}
                >
                  <td className="w-8 py-2 pl-4">
                    {entry.is_directory ? (
                      <Folder className="size-4 text-muted-foreground" />
                    ) : (
                      <FileIcon className="size-4 text-muted-foreground" />
                    )}
                  </td>
                  <td className="py-2 pr-2">
                    <span className="font-medium">{entry.name}</span>
                  </td>
                  <td className="py-2 pr-2">
                    <Badge tone="outline" className="text-[10px] uppercase">
                      {entry.area}
                    </Badge>
                  </td>
                  <td className="hidden py-2 pr-2 text-muted-foreground sm:table-cell">
                    {formatBytes(entry.size)}
                  </td>
                  <td className="py-2 pr-4">
                    {!entry.is_directory && (
                      <div
                        className="flex justify-end gap-1"
                        onClick={(e) => e.stopPropagation()}
                      >
                        <Button
                          ghost
                          size="icon"
                          type="button"
                          aria-label={`Edit ${entry.name}`}
                          onClick={() => void openEntry(entry)}
                        >
                          <Pencil />
                        </Button>
                        <Button
                          ghost
                          size="icon"
                          type="button"
                          aria-label={`Download ${entry.name}`}
                          onClick={() => void download(entry)}
                        >
                          <Download />
                        </Button>
                        {entry.area === "workspace" && (
                          <Button
                            ghost
                            size="icon"
                            type="button"
                            aria-label={`Delete ${entry.name}`}
                            onClick={() => setPendingDelete(entry)}
                          >
                            <Trash2 />
                          </Button>
                        )}
                      </div>
                    )}
                  </td>
                </tr>
              ))}
              {!loading && (listing?.entries ?? []).length === 0 && (
                <tr>
                  <td colSpan={5} className="py-8 text-center text-muted-foreground">
                    No files here yet.
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </CardContent>
      </Card>

      <Dialog open={Boolean(editor)} onOpenChange={(open) => !open && setEditor(null)}>
        <DialogContent className="max-w-3xl">
          <DialogHeader>
            <DialogTitle>{editor?.name}</DialogTitle>
            <DialogDescription>
              {editor?.area === "deploy"
                ? "Deploy file — saved to the repo and pushed to the running agent via Redis."
                : selectedAgent === "shared"
                  ? "Shared file — every agent receives it in workspace/shared/ on its next sync."
                  : "Workspace file — pushed to Redis; the agent pulls it on its next sync."}
            </DialogDescription>
          </DialogHeader>
          <textarea
            className="h-96 w-full resize-y rounded-md border bg-background p-3 font-mono text-xs"
            value={editor?.content ?? ""}
            onChange={(e) =>
              setEditor((prev) => (prev ? { ...prev, content: e.target.value } : prev))
            }
            spellCheck={false}
          />
          <DialogFooter>
            <Button ghost type="button" onClick={() => setEditor(null)} disabled={saving}>
              Cancel
            </Button>
            <Button
              type="button"
              onClick={() => void saveEditor()}
              disabled={saving}
              prefix={saving ? <Spinner /> : undefined}
            >
              Save
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <DeleteConfirmDialog
        open={Boolean(pendingDelete)}
        loading={deleting}
        onCancel={() => setPendingDelete(null)}
        onConfirm={() => void confirmDelete()}
        title={pendingDelete ? `Delete ${pendingDelete.name}?` : "Delete file?"}
        description="This removes the file from the synced workspace. The agent deletes its local copy on next sync."
      />
    </div>
  );
}
