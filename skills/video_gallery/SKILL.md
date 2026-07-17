---
name: video_gallery
description: Share video links you find online with the human operator's dashboard video gallery so they can watch them in an embedded player.
---

# Video Gallery

When you find videos online that are relevant to the conversation (YouTube,
Vimeo, or direct .mp4/.webm/.m3u8 links), publish them to the shared gallery.
The operator sees them in the dashboard's **Videos** page and can play them
right there.

## Publish one video

```bash
python3 /app/skills/video_gallery/publish_video.py --action add \
  --url "https://example.com/clip.mp4" \
  --title "Short descriptive title" \
  --description "Why this is relevant" \
  --source-page "https://example.com/article" \
  --tags "research,demo"
```

## Publish many at once

Write a JSON file of `[{"url": ..., "title": ..., "description": ...,
"source_page": ..., "tags": [...]}, ...]` then:

```bash
python3 /app/skills/video_gallery/publish_video.py --action add-batch --file /tmp/videos.json
```

## Rules

- Publish **links only** — never download and re-upload video files.
- Always set a meaningful title and a one-line description of relevance.
- Include `--source-page` so the operator can see where the video came from.
- Duplicate URLs are deduplicated automatically (same URL = same entry).
- `--action list` shows recent entries; `--action remove --id <id>` deletes one.
