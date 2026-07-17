import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  ExternalLink,
  Film,
  Link2,
  Plus,
  RefreshCw,
  Trash2,
} from "lucide-react";
import Plyr from "plyr";
import "plyr/dist/plyr.css";
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
import type { VideoEntry } from "@/lib/api";

const DATE_FORMAT = new Intl.DateTimeFormat(undefined, {
  dateStyle: "medium",
  timeStyle: "short",
});

function formatDate(iso: string): string {
  const parsed = new Date(iso);
  return Number.isNaN(parsed.getTime()) ? "" : DATE_FORMAT.format(parsed);
}

/** Extract a YouTube video id from watch/short/embed URLs. */
function youtubeId(url: string): string | null {
  try {
    const u = new URL(url);
    const host = u.hostname.replace(/^www\./, "");
    if (host === "youtu.be") return u.pathname.slice(1).split("/")[0] || null;
    if (u.pathname.startsWith("/watch")) return u.searchParams.get("v");
    const m = u.pathname.match(/^\/(embed|shorts|live)\/([^/?]+)/);
    return m ? m[2] : null;
  } catch {
    return null;
  }
}

function vimeoId(url: string): string | null {
  try {
    const u = new URL(url);
    const m = u.pathname.match(/\/(\d+)/);
    return m ? m[1] : null;
  } catch {
    return null;
  }
}

/** Defense-in-depth: only ever render http(s) URLs as links/players. */
function safeUrl(url: string): string | null {
  try {
    const parsed = new URL(url);
    return parsed.protocol === "http:" || parsed.protocol === "https:"
      ? url
      : null;
  } catch {
    return null;
  }
}

function videoThumbnail(video: VideoEntry): string | null {
  if (video.kind === "youtube") {
    const id = youtubeId(video.url);
    return id ? `https://i.ytimg.com/vi/${id}/hqdefault.jpg` : null;
  }
  return null;
}

/** Player for a single entry: Plyr for direct files + YouTube/Vimeo embeds. */
function VideoPlayer({ video }: { video: VideoEntry }) {
  const containerRef = useRef<HTMLDivElement>(null);
  const plyrRef = useRef<Plyr | null>(null);

  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;
    container.innerHTML = "";

    if (!safeUrl(video.url)) return;
    let target: HTMLElement;
    if (video.kind === "youtube" || video.kind === "vimeo") {
      const id =
        video.kind === "youtube" ? youtubeId(video.url) : vimeoId(video.url);
      if (!id) return;
      target = document.createElement("div");
      target.setAttribute("data-plyr-provider", video.kind);
      target.setAttribute("data-plyr-embed-id", id);
    } else {
      const el = document.createElement("video");
      el.src = video.url;
      el.controls = true;
      el.playsInline = true;
      target = el;
    }
    container.appendChild(target);
    plyrRef.current = new Plyr(target, { ratio: "16:9" });

    return () => {
      plyrRef.current?.destroy();
      plyrRef.current = null;
    };
  }, [video]);

  if (video.kind === "page") {
    // Arbitrary web page (not an embeddable/playable URL) — link out.
    return (
      <div className="flex aspect-video flex-col items-center justify-center gap-3 rounded-md border bg-muted/30 text-sm text-muted-foreground">
        <Film className="h-8 w-8" />
        <p>This link is a web page, not a playable video file.</p>
        <Button asChild variant="outline" size="sm">
          <a href={safeUrl(video.url) ?? "#"} target="_blank" rel="noreferrer noopener">
            Open in new tab <ExternalLink className="ml-1 h-3.5 w-3.5" />
          </a>
        </Button>
      </div>
    );
  }
  return <div ref={containerRef} className="overflow-hidden rounded-md" />;
}

export default function VideosPage() {
  const { toast, showToast } = useToast();
  const [videos, setVideos] = useState<VideoEntry[]>([]);
  const [agents, setAgents] = useState<string[]>([]);
  const [agentFilter, setAgentFilter] = useState<string>("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [playing, setPlaying] = useState<VideoEntry | null>(null);
  const [deleting, setDeleting] = useState<VideoEntry | null>(null);
  const [addOpen, setAddOpen] = useState(false);
  const [addUrl, setAddUrl] = useState("");
  const [addTitle, setAddTitle] = useState("");
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await api.listVideos();
      setVideos(res.videos);
      setAgents(res.agents);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const shown = useMemo(
    () => (agentFilter ? videos.filter((v) => v.agent === agentFilter) : videos),
    [videos, agentFilter],
  );

  const handleAdd = async () => {
    setBusy(true);
    try {
      await api.addVideo({ url: addUrl.trim(), title: addTitle.trim() });
      showToast("Video added");
      setAddOpen(false);
      setAddUrl("");
      setAddTitle("");
      await load();
    } catch (err) {
      showToast(err instanceof Error ? err.message : "Failed to add video");
    } finally {
      setBusy(false);
    }
  };

  const handleDelete = async () => {
    if (!deleting) return;
    setBusy(true);
    try {
      await api.deleteVideo(deleting.id);
      showToast("Video removed");
      setDeleting(null);
      await load();
    } catch (err) {
      showToast(err instanceof Error ? err.message : "Failed to remove video");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="space-y-4 p-6">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <h1 className="text-xl font-semibold">Videos</h1>
          <p className="text-sm text-muted-foreground">
            Videos your agents found online, ready to watch.
          </p>
        </div>
        <div className="flex items-center gap-2">
          <select
            className="h-9 rounded-md border bg-background px-2 text-sm"
            value={agentFilter}
            onChange={(e) => setAgentFilter(e.target.value)}
          >
            <option value="">All agents</option>
            {agents.map((a) => (
              <option key={a} value={a}>
                {a}
              </option>
            ))}
          </select>
          <Button variant="outline" size="sm" onClick={() => setAddOpen(true)}>
            <Plus className="mr-1 h-4 w-4" /> Add link
          </Button>
          <Button variant="outline" size="sm" onClick={() => void load()}>
            <RefreshCw className="mr-1 h-4 w-4" /> Refresh
          </Button>
        </div>
      </div>

      {error && (
        <div className="rounded-md border border-destructive/40 bg-destructive/10 p-3 text-sm text-destructive">
          {error}
        </div>
      )}

      {loading ? (
        <div className="flex justify-center py-16">
          <Spinner />
        </div>
      ) : shown.length === 0 ? (
        <div className="flex flex-col items-center gap-2 py-16 text-muted-foreground">
          <Film className="h-10 w-10" />
          <p className="text-sm">
            No videos yet. Agents publish links here with the video_gallery
            skill, or add one yourself.
          </p>
        </div>
      ) : (
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
          {shown.map((video) => {
            const thumb = videoThumbnail(video);
            return (
              <Card key={video.id} className="overflow-hidden">
                <button
                  type="button"
                  className="block w-full"
                  onClick={() => setPlaying(video)}
                >
                  {thumb ? (
                    <img
                      src={thumb}
                      alt={video.title}
                      className="aspect-video w-full object-cover"
                      loading="lazy"
                    />
                  ) : (
                    <div className="flex aspect-video w-full items-center justify-center bg-muted/40">
                      <Film className="h-8 w-8 text-muted-foreground" />
                    </div>
                  )}
                </button>
                <CardContent className="space-y-2 p-3">
                  <button
                    type="button"
                    className="line-clamp-2 text-left text-sm font-medium hover:underline"
                    onClick={() => setPlaying(video)}
                    title={video.title}
                  >
                    {video.title}
                  </button>
                  {video.description && (
                    <p className="line-clamp-2 text-xs text-muted-foreground">
                      {video.description}
                    </p>
                  )}
                  <div className="flex flex-wrap items-center gap-1">
                    <Badge variant="secondary">{video.agent}</Badge>
                    <Badge variant="outline">{video.kind}</Badge>
                    {video.tags.slice(0, 3).map((t) => (
                      <Badge key={t} variant="outline">
                        {t}
                      </Badge>
                    ))}
                  </div>
                  <div className="flex items-center justify-between text-xs text-muted-foreground">
                    <span>{formatDate(video.added_at)}</span>
                    <span className="flex items-center gap-1">
                      {safeUrl(video.source_page) && (
                        <a
                          href={safeUrl(video.source_page)!}
                          target="_blank"
                          rel="noreferrer noopener"
                          title="Source page"
                          className="rounded p-1 hover:bg-muted"
                        >
                          <Link2 className="h-3.5 w-3.5" />
                        </a>
                      )}
                      <a
                        href={safeUrl(video.url) ?? "#"}
                        target="_blank"
                        rel="noreferrer noopener"
                        title="Open original"
                        className="rounded p-1 hover:bg-muted"
                      >
                        <ExternalLink className="h-3.5 w-3.5" />
                      </a>
                      <button
                        type="button"
                        title="Remove"
                        className="rounded p-1 hover:bg-muted"
                        onClick={() => setDeleting(video)}
                      >
                        <Trash2 className="h-3.5 w-3.5" />
                      </button>
                    </span>
                  </div>
                </CardContent>
              </Card>
            );
          })}
        </div>
      )}

      <Dialog open={!!playing} onOpenChange={(open) => !open && setPlaying(null)}>
        <DialogContent className="max-w-3xl">
          {playing && (
            <>
              <DialogHeader>
                <DialogTitle className="pr-8">{playing.title}</DialogTitle>
                {playing.description && (
                  <DialogDescription>{playing.description}</DialogDescription>
                )}
              </DialogHeader>
              <VideoPlayer video={playing} />
              <div className="flex items-center justify-between text-xs text-muted-foreground">
                <span>
                  Shared by <span className="font-medium">{playing.agent}</span>
                  {" · "}
                  {formatDate(playing.added_at)}
                </span>
                <a
                  href={safeUrl(playing.url) ?? "#"}
                  target="_blank"
                  rel="noreferrer noopener"
                  className="flex items-center gap-1 hover:underline"
                >
                  Open original <ExternalLink className="h-3.5 w-3.5" />
                </a>
              </div>
            </>
          )}
        </DialogContent>
      </Dialog>

      <Dialog open={addOpen} onOpenChange={setAddOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Add a video link</DialogTitle>
            <DialogDescription>
              Paste a YouTube, Vimeo, or direct video file URL.
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-3">
            <input
              className="h-9 w-full rounded-md border bg-background px-3 text-sm"
              placeholder="https://…"
              value={addUrl}
              onChange={(e) => setAddUrl(e.target.value)}
            />
            <input
              className="h-9 w-full rounded-md border bg-background px-3 text-sm"
              placeholder="Title (optional)"
              value={addTitle}
              onChange={(e) => setAddTitle(e.target.value)}
            />
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setAddOpen(false)}>
              Cancel
            </Button>
            <Button onClick={() => void handleAdd()} disabled={busy || !addUrl.trim()}>
              {busy ? <Spinner className="h-4 w-4" /> : "Add"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <DeleteConfirmDialog
        open={!!deleting}
        onCancel={() => setDeleting(null)}
        title="Remove video?"
        description={`"${deleting?.title ?? ""}" will be removed from the gallery. The original stays online.`}
        onConfirm={() => void handleDelete()}
        loading={busy}
      />

      <Toast toast={toast} />
    </div>
  );
}
