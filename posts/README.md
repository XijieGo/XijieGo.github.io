# Blog Authoring Guide (`posts/`)

All Markdown posts in this folder are compiled into static HTML under `../blog/` by `render_blog.py` (`README.md` and files starting with `_` are ignored).

## Quick Commands

```bash
# 1. Create a new post template
python3 render_blog.py --new my-topic -t "My Post Title"

# 2. Build all published posts (outputs to blog/)
python3 render_blog.py

# 3. Local preview with live auto-rebuild (includes drafts)
python3 render_blog.py --serve --port 8000
```

## Two Supported Post Formats

1. **Single Markdown file**: `posts/2026-11-24-my-slug.md` -> `/blog/my-slug/`
2. **Folder with images/assets**: `posts/my-slug/index.md` (+ `fig1.png`, etc.) -> `/blog/my-slug/` (all images in the folder are automatically copied alongside the post).

## Frontmatter Fields

```yaml
---
title: "Post Title"
subtitle: "Optional subtitle shown under the title"
date: 2026-11-24
tags: [Foundation Models, Interpretability]
summary: "Short summary shown on /blog/ archive list."
toc: true      # auto-enabled when >= 2 headings; set false to hide
math: true     # load local KaTeX (default: true)
draft: false   # set true to hide from normal builds (visible with --drafts or --serve)
macros:        # optional custom KaTeX macros (merged with \R, \E, \KL, \norm, etc.)
  "\\cL": "\\mathcal{L}"
---
```
