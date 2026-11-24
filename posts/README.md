# Blog Authoring Guide (`posts/`)

All posts in this folder are compiled into static HTML under `../blog/` by `render_blog.py` (`README.md` and files starting with `_` are ignored).

## Categories

Posts are organized into three fixed categories:
- `Research` — paper web editions, research updates, project write-ups
- `Thoughts` — reflections, reading notes, essays on ML & science
- `Ramblings` — casual notes, personal updates, occasional musings

## Quick Commands

```bash
# 1. Create a new Markdown post template
python3 render_blog.py --new my-topic -t "My Post Title" -c Thoughts

# 2. Build all published posts (updates blog/, sitemap.xml, blog/feed.xml)
python3 render_blog.py

# 3. Local preview with live auto-rebuild (includes drafts)
python3 render_blog.py --serve --port 8000

# 4. Auto-configure Giscus comments (after enabling Discussions & installing giscus app)
python3 render_blog.py --setup-giscus
```

## Supported Post Formats

1. **Single Markdown file**: `posts/2026-11-24-my-slug.md` -> `/blog/my-slug/`
2. **Markdown folder with figures**: `posts/my-slug/index.md` (+ `fig1.png`, etc.) -> `/blog/my-slug/`
3. **LaTeX paper web edition**: `posts/my-paper/paper.yaml` pointing `source_dir` to a directory containing `main.tex`, `sections/`, `build/main.aux`, and `build/main.bbl`.

## Frontmatter Fields (Markdown)

```yaml
---
title: "Post Title"
category: Thoughts   # Research | Thoughts | Ramblings
authors: "Xijie Gong"
date: 2026-11-24
summary: "Short summary shown on /blog/ archive list."
toc: true            # auto-enabled when >= 2 headings; set false to hide
math: true           # load local KaTeX (default: true)
comments: true       # show Giscus comments (default: true; set false to disable on a post)
draft: false         # set true to hide from normal builds (visible with --drafts or --serve)
---
```
