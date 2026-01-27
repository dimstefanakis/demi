---
name: unsplash-backfill
description: Use when replacing placeholder images with relevant Unsplash photos. Triggers: “Unsplash”, “placeholder images”, “backfill images”, or when a site needs real photography after design.
---

# Unsplash Backfill

Use the custom tool `mcp__claudius-unsplash__search_photos` to fetch relevant images after Gemini finishes a build/update.

## Workflow

1. Scan for placeholder images (`placehold.co`, `via.placeholder.com`, `dummyimage`, `picsum`, `loremflickr`, `placeholder` filenames, or obvious dummy URLs) or empty image tags.
2. Infer a short, specific query from nearby section copy (e.g., “barber shop interior”, “team portrait”, “coffee shop hero”).
3. Call the tool with an orientation that matches the layout (hero: landscape, avatar: squarish, portrait: portrait).
4. Replace the placeholder URL with the returned Unsplash URL and update `alt` text.
5. If using `next/image`, ensure `images.unsplash.com` is allowed in `next.config`.

## Tool Usage

```
mcp__claudius-unsplash__search_photos {"query": "barber shop interior", "count": 1, "orientation": "landscape"}
```

Return values include a ready-to-use URL plus photographer info for attribution if needed.
