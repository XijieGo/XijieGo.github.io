#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Static Academic Blog Generator for xijiegong.com (Husky'Log Editorial Style).

Supports both:
  1. Markdown posts:      posts/YYYY-MM-DD-slug.md  or  posts/slug/index.md
  2. LaTeX paper posts:   posts/slug/paper.yaml  (compiles main.tex + .bbl + PDF figures with KaTeX SSR)

Usage:
  python3 render_blog.py                           # Build published posts -> blog/
  python3 render_blog.py --drafts                  # Include draft: true posts
  python3 render_blog.py --new my-slug -t "Title"  # Create a new Markdown post template
  python3 render_blog.py --serve                   # Local live-reload preview on :8000
"""

from __future__ import annotations

import argparse
import datetime as dt
from email.utils import format_datetime
import html
import http.server
import json
import re
import shutil
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import markdown
import yaml

import tex_paper

ROOT = Path(__file__).resolve().parent
POSTS_DIR = ROOT / "posts"
BLOG_DIR = ROOT / "blog"
SITE_URL = "https://xijiegong.com"
GA_ID = "G-0JT5H48NFQ"
DEFAULT_AUTHOR = "Xijie Gong"

DEFAULT_KATEX_MACROS: dict[str, str] = {
    r"\R": r"\mathbb{R}",
    r"\N": r"\mathbb{N}",
    r"\Z": r"\mathbb{Z}",
    r"\E": r"\mathbb{E}",
    r"\Prob": r"\mathbb{P}",
    r"\Var": r"\mathrm{Var}",
    r"\KL": r"D_{\mathrm{KL}}",
    r"\argmax": r"\operatorname*{arg\,max}",
    r"\argmin": r"\operatorname*{arg\,min}",
    r"\norm": r"\left\lVert #1 \right\rVert",
    r"\abs": r"\left\lvert #1 \right\rvert",
    r"\inner": r"\left\langle #1, #2 \right\rangle",
}

DATE_PREFIX_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})-(.+)$")
FRONTMATTER_RE = re.compile(r"\A---\s*\r?\n(.*?)\r?\n---\s*(?:\r?\n|$)", re.DOTALL)
FENCED_CODE_RE = re.compile(
    r"(^|\n)(?P<indent>[ \t]*)(?P<fence>`{3,}|~{3,})(?P<info>[^\n]*)\n(?P<code>.*?)\n(?P=indent)(?P=fence)[ \t]*(?=\n|$)",
    re.DOTALL,
)
INLINE_CODE_RE = re.compile(r"(`+)([^`\n]+?)\1")
DISPLAY_DOLLAR_RE = re.compile(r"\$\$(.+?)\$\$", re.DOTALL)
DISPLAY_BRACKET_RE = re.compile(r"\\\[(.+?)\\\]", re.DOTALL)
INLINE_PAREN_RE = re.compile(r"\\\((.+?)\\\)")
INLINE_DOLLAR_RE = re.compile(r"(?<![0-9\\])\$(?!\s)([^$\n]+?)(?<!\s)\$(?![0-9])")

HEADING_HTML_RE = re.compile(r"<h([234])(?:\s+id=\"([^\"]+)\")?>(.*?)</h\1>", re.DOTALL)
CALLOUT_RE = re.compile(
    r"<blockquote>\s*<p>\[!(NOTE|TIP|IMPORTANT|WARNING|CAUTION)\](?:[ \t]+([^\n<]*))?\s*(?:</p>|<br\s*/?>|\n)\s*([\s\S]*?)</blockquote>",
    re.IGNORECASE,
)
STANDALONE_IMG_RE = re.compile(
    r"<p>\s*<img\s+([^>]*?)alt=\"([^\"]*)\"([^>]*?)\s*/?>\s*</p>",
    re.DOTALL,
)
CJK_RE = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf]")
EN_WORD_RE = re.compile(r"[A-Za-z0-9_]+")


@dataclass
class TocItem:
    level: int
    ident: str
    title: str


@dataclass
class Post:
    slug: str
    title: str
    subtitle: str
    authors: str
    date: dt.date
    date_display: str
    year: int
    tags: list[str]
    summary: str
    word_count: int
    reading_minutes: int
    html_body: str
    toc: list[TocItem] = field(default_factory=list)
    show_toc: bool = True
    use_math: bool = True
    draft: bool = False
    macros: dict[str, str] = field(default_factory=dict)
    source_path: Path = Path()
    asset_dir: Path | None = None
    is_latex_paper: bool = False
    paper_config: dict[str, Any] = field(default_factory=dict)
    compiled_paper: tex_paper.CompiledPaper | None = None


def parse_date(raw: Any, fallback: dt.date | None = None) -> dt.date:
    if isinstance(raw, dt.datetime):
        return raw.date()
    if isinstance(raw, dt.date):
        return raw
    if isinstance(raw, str) and raw.strip():
        cleaned = raw.strip().replace(".", "-").replace("/", "-")
        try:
            return dt.date.fromisoformat(cleaned)
        except ValueError:
            pass
    return fallback or dt.date.today()


def count_words_and_minutes(text: str) -> tuple[int, int]:
    cleaned = FENCED_CODE_RE.sub(" ", text)
    # Strip rendered KaTeX markup & dense numeric tables so word count reflects actual prose
    cleaned = re.sub(r'<span class="katex-mathml">[\s\S]*?</span>', " ", cleaned)
    cleaned = re.sub(r'<span class="katex-html"[\s\S]*?</span></span></span>', " ", cleaned)
    cleaned = re.sub(r"<table\b[\s\S]*?</table>", " ", cleaned)
    cleaned = re.sub(r"<[^>]+>", " ", cleaned)
    cleaned = re.sub(r"[#>*`~\[\]()!_-]", " ", cleaned)
    cjk = len(CJK_RE.findall(cleaned))
    en = len(EN_WORD_RE.findall(CJK_RE.sub(" ", cleaned)))
    total_words = cjk + en
    minutes = max(1, round(cjk / 350.0 + en / 220.0))
    return total_words, minutes


def strip_html_tags(s: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", s)).strip()


def extract_auto_summary(md_body: str, max_len: int = 220) -> str:
    text = FENCED_CODE_RE.sub("", md_body)
    text = DISPLAY_DOLLAR_RE.sub("", text)
    text = DISPLAY_BRACKET_RE.sub("", text)
    for para in re.split(r"\n\s*\n", text):
        line = para.strip()
        if not line or line.startswith(("#", ">", "!", "|", "- ", "* ", "1. ", "<")):
            continue
        line = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", line)
        line = re.sub(r"[*_`]", "", line)
        line = re.sub(r"\s+", " ", line).strip()
        if line:
            if len(line) > max_len:
                return line[: max_len - 1].rstrip() + "…"
            return line
    return ""


def slugify_heading(text: str, used: set[str]) -> str:
    base = re.sub(r"[^\w\u4e00-\u9fff-]+", "-", text.lower()).strip("-") or "section"
    candidate = base
    idx = 2
    while candidate in used:
        candidate = f"{base}-{idx}"
        idx += 1
    used.add(candidate)
    return candidate


def strip_blockquote_prefix_in_math(tex: str) -> str:
    lines = tex.splitlines()
    if len(lines) > 1:
        return "\n".join([lines[0]] + [re.sub(r"^\s*>\s?", "", ln) for ln in lines[1:]]).strip()
    return tex.strip()


def render_markdown_with_math(md_text: str) -> tuple[str, list[TocItem]]:
    code_blocks: list[str] = []
    inline_codes: list[str] = []
    math_blocks: list[str] = []
    math_inlines: list[str] = []

    def save_fenced_code(match: re.Match[str]) -> str:
        leading = match.group(1)
        indent = match.group("indent")
        info = match.group("info").strip()
        lang = info.split()[0] if info else ""
        raw_code = match.group("code")
        if indent:
            raw_code = "\n".join(
                ln[len(indent) :] if ln.startswith(indent) else ln
                for ln in raw_code.splitlines()
            )
        escaped_code = html.escape(raw_code, quote=False)
        lang_badge = f'<span class="code-lang">{html.escape(lang)}</span>' if lang else ""
        rendered = (
            f'<div class="code-wrap">'
            f"{lang_badge}"
            f'<button class="copy-btn" type="button" onclick="copyCode(this)">Copy</button>'
            f"<pre><code>{escaped_code}</code></pre>"
            f"</div>"
        )
        token = f"ZXCODEBLOCK{len(code_blocks)}XZ"
        code_blocks.append(rendered)
        return f"{leading}{indent}{token}"

    work = FENCED_CODE_RE.sub(save_fenced_code, md_text)

    def save_inline_code(match: re.Match[str]) -> str:
        token = f"ZXINLINECODE{len(inline_codes)}XZ"
        inline_codes.append(f"<code>{html.escape(match.group(2), quote=False)}</code>")
        return token

    work = INLINE_CODE_RE.sub(save_inline_code, work)
    work = work.replace(r"\$", "ZXESCAPEDDOLLARXZ")

    def save_display_math(match: re.Match[str]) -> str:
        tex = strip_blockquote_prefix_in_math(match.group(1))
        token = f"ZXMATHBLOCK{len(math_blocks)}XZ"
        math_blocks.append(html.escape(tex, quote=False))
        return token

    work = DISPLAY_DOLLAR_RE.sub(save_display_math, work)
    work = DISPLAY_BRACKET_RE.sub(save_display_math, work)

    def save_inline_math(match: re.Match[str]) -> str:
        tex = match.group(1).strip()
        token = f"ZXMATHINLINE{len(math_inlines)}XZ"
        math_inlines.append(rf"\({html.escape(tex, quote=False)}\)")
        return token

    work = INLINE_PAREN_RE.sub(save_inline_math, work)
    work = INLINE_DOLLAR_RE.sub(save_inline_math, work)

    md = markdown.Markdown(
        extensions=["tables", "footnotes", "attr_list", "sane_lists", "md_in_html"],
        output_format="html",
    )
    rendered_html = md.convert(work)

    default_titles = {
        "note": "Note",
        "tip": "Tip",
        "important": "Important",
        "warning": "Warning",
        "caution": "Caution",
    }

    def replace_callout(match: re.Match[str]) -> str:
        kind = match.group(1).lower()
        custom_title = (match.group(2) or "").strip()
        body = match.group(3).strip()
        if body and not body.startswith(("<p>", "<ul>", "<ol>")):
            body = f"<p>{body}"
        title = custom_title or default_titles.get(kind, kind.capitalize())
        return (
            f'<div class="callout-block">'
            f'<div class="callout-header"><span class="callout-title">{html.escape(title)}</span></div>'
            f'<div class="callout-content">{body}</div>'
            f"</div>"
        )

    rendered_html = CALLOUT_RE.sub(replace_callout, rendered_html)

    rendered_html = re.sub(
        r'<div class="table-wrap">\s*(<table\b[\s\S]*?</table>)\s*</div>',
        r"\1",
        rendered_html,
    )
    rendered_html = re.sub(
        r"(<table\b[\s\S]*?</table>)",
        r'<div class="table-wrap">\1</div>',
        rendered_html,
    )

    def replace_figure(match: re.Match[str]) -> str:
        before_alt, alt_text, after_alt = match.group(1), match.group(2).strip(), match.group(3)
        if not alt_text:
            return match.group(0)
        return (
            f'<figure class="post-figure">'
            f'<img {before_alt}alt="{alt_text}"{after_alt} loading="lazy">'
            f"<figcaption>{alt_text}</figcaption>"
            f"</figure>"
        )

    rendered_html = STANDALONE_IMG_RE.sub(replace_figure, rendered_html)

    for idx, math_span in enumerate(math_inlines):
        rendered_html = rendered_html.replace(f"ZXMATHINLINE{idx}XZ", math_span)
    for idx, code_span in enumerate(inline_codes):
        rendered_html = rendered_html.replace(f"ZXINLINECODE{idx}XZ", code_span)

    toc_items: list[TocItem] = []
    used_ids: set[str] = set()

    def enrich_heading(match: re.Match[str]) -> str:
        level = int(match.group(1))
        existing_id = match.group(2)
        inner_html = match.group(3).strip()
        plain_title = strip_html_tags(inner_html)
        ident = existing_id or slugify_heading(plain_title, used_ids)
        if existing_id:
            used_ids.add(existing_id)
        if level in (2, 3):
            toc_items.append(TocItem(level=level, ident=ident, title=re.sub(r"</?a[^>]*>", "", inner_html)))
        return (
            f'<h{level} id="{html.escape(ident)}">'
            f"{inner_html}"
            f'<a class="heading-anchor" href="#{html.escape(ident)}" aria-label="Link to section">#</a>'
            f"</h{level}>"
        )

    rendered_html = HEADING_HTML_RE.sub(enrich_heading, rendered_html)

    for idx, escaped_tex in enumerate(math_blocks):
        token = f"ZXMATHBLOCK{idx}XZ"
        rendered_html = re.sub(
            rf"<p>\s*{token}\s*</p>",
            lambda _m, t=escaped_tex: f'<div class="math-block">\\[\n{t}\n\\]</div>',
            rendered_html,
        )
        rendered_html = rendered_html.replace(
            token,
            f'<span class="math-block" style="display:block">\\[\n{escaped_tex}\n\\]</span>',
        )

    for idx, block_html in enumerate(code_blocks):
        token = f"ZXCODEBLOCK{idx}XZ"
        rendered_html = re.sub(rf"<p>\s*{token}\s*</p>", lambda _m, b=block_html: b, rendered_html)
        rendered_html = rendered_html.replace(token, block_html)

    rendered_html = rendered_html.replace("ZXESCAPEDDOLLARXZ", "$")
    return rendered_html, toc_items


def discover_post_sources() -> list[tuple[Path, str, dt.date | None, Path | None]]:
    if not POSTS_DIR.exists():
        return []
    found: list[tuple[Path, str, dt.date | None, Path | None]] = []
    for entry in sorted(POSTS_DIR.iterdir()):
        if entry.name.startswith((".", "_")):
            continue
        if entry.is_file() and entry.suffix.lower() == ".md":
            if entry.name.lower() == "readme.md":
                continue
            m = DATE_PREFIX_RE.match(entry.stem)
            d, slug = (parse_date(m.group(1)), m.group(2)) if m else (None, entry.stem)
            found.append((entry, slug, d, None))
        elif entry.is_dir():
            paper_yaml = entry / "paper.yaml"
            index_md = entry / "index.md"
            target_file = paper_yaml if paper_yaml.is_file() else index_md
            if not target_file.is_file():
                continue
            m = DATE_PREFIX_RE.match(entry.name)
            d, slug = (parse_date(m.group(1)), m.group(2)) if m else (None, entry.name)
            found.append((target_file, slug, d, entry))
    return found


def load_post(
    md_path: Path,
    default_slug: str,
    fallback_date: dt.date | None,
    asset_dir: Path | None,
) -> Post:
    if md_path.name == "paper.yaml":
        cfg = yaml.safe_load(md_path.read_text(encoding="utf-8")) or {}
        slug = str(cfg.get("slug", default_slug)).strip().strip("/")
        title = str(cfg.get("title", default_slug)).strip()
        subtitle = str(cfg.get("subtitle", "")).strip()
        authors = str(cfg.get("authors", DEFAULT_AUTHOR)).strip()
        post_date = parse_date(
            cfg.get("date"),
            fallback=fallback_date or dt.date.fromtimestamp(md_path.stat().st_mtime),
        )
        raw_tags = cfg.get("tags", [])
        tags = [str(t).strip() for t in raw_tags] if isinstance(raw_tags, list) else []
        return Post(
            slug=slug,
            title=title,
            subtitle=subtitle,
            authors=authors,
            date=post_date,
            date_display=post_date.strftime("%Y.%m.%d"),
            year=post_date.year,
            tags=tags,
            summary=str(cfg.get("summary", "")).strip(),
            word_count=0,
            reading_minutes=1,
            html_body="",
            draft=bool(cfg.get("draft", False)),
            source_path=md_path,
            asset_dir=asset_dir,
            is_latex_paper=True,
            paper_config=cfg,
        )

    raw = md_path.read_text(encoding="utf-8")
    meta: dict[str, Any] = {}
    body = raw

    fm_match = FRONTMATTER_RE.match(raw)
    if fm_match:
        loaded = yaml.safe_load(fm_match.group(1))
        if isinstance(loaded, dict):
            meta = loaded
        body = raw[fm_match.end() :]

    leading_h1 = re.match(r"^\s*#\s+([^\n]+)\n*", body)
    title = str(meta.get("title", "")).strip()
    if leading_h1:
        h1_text = leading_h1.group(1).strip()
        if not title:
            title = h1_text
            body = body[leading_h1.end() :]
        elif title.lower() == h1_text.lower():
            body = body[leading_h1.end() :]
    if not title:
        title = default_slug.replace("-", " ").title()

    slug = str(meta.get("slug", default_slug)).strip().strip("/")
    subtitle = str(meta.get("subtitle", "")).strip()
    authors = str(meta.get("authors", DEFAULT_AUTHOR)).strip()
    post_date = parse_date(
        meta.get("date"),
        fallback=fallback_date or dt.date.fromtimestamp(md_path.stat().st_mtime),
    )
    raw_tags = meta.get("tags", [])
    if isinstance(raw_tags, str):
        tags = [t.strip() for t in raw_tags.split(",") if t.strip()]
    elif isinstance(raw_tags, list):
        tags = [str(t).strip() for t in raw_tags if str(t).strip()]
    else:
        tags = []

    summary = str(meta.get("summary") or meta.get("description") or "").strip() or extract_auto_summary(body)
    words, mins = count_words_and_minutes(body)
    draft = bool(meta.get("draft", False))
    use_math = bool(meta.get("math", True))

    macros = dict(DEFAULT_KATEX_MACROS)
    if isinstance(meta.get("macros"), dict):
        for k, v in meta["macros"].items():
            macros[str(k)] = str(v)

    html_body, toc_items = render_markdown_with_math(body)
    toc_setting = meta.get("toc", None)
    show_toc = (len(toc_items) >= 2) if toc_setting is None else (bool(toc_setting) and len(toc_items) >= 1)

    return Post(
        slug=slug,
        title=title,
        subtitle=subtitle,
        authors=authors,
        date=post_date,
        date_display=post_date.strftime("%Y.%m.%d"),
        year=post_date.year,
        tags=tags,
        summary=summary,
        word_count=words,
        reading_minutes=mins,
        html_body=html_body,
        toc=toc_items,
        show_toc=show_toc,
        use_math=use_math,
        draft=draft,
        macros=macros,
        source_path=md_path,
        asset_dir=asset_dir,
    )


# ---------------------------------------------------------------------------
# Shared HTML Chrome (Native xijiegong.com Quiet Academic Theme)
# ---------------------------------------------------------------------------

def render_head(
    title: str,
    description: str,
    root_prefix: str,
    canonical_path: str,
    include_katex_css: bool = True,
    og_type: str = "website",
    og_image: str = "",
    extra_meta_html: str = "",
    json_ld: dict[str, Any] | None = None,
) -> str:
    esc_title = html.escape(title)
    esc_desc = html.escape(description)
    canonical_url = f"{SITE_URL}/{canonical_path.lstrip('/')}"
    image_url = og_image or f"{SITE_URL}/assets/avatar.jpg"
    twitter_card = "summary_large_image" if og_image else "summary"
    katex_link = (
        f'\n  <link rel="stylesheet" href="{root_prefix}assets/katex/katex.min.css">'
        if include_katex_css
        else ""
    )
    json_ld_block = ""
    if json_ld:
        ld_str = json.dumps(json_ld, ensure_ascii=False, indent=2).replace("</", "<\\/")
        json_ld_block = f'\n  <script type="application/ld+json">\n{ld_str}\n  </script>'
    extra_block = f"\n{extra_meta_html}" if extra_meta_html else ""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{esc_title}</title>

  <!-- Google tag (gtag.js) -->
  <script async src="https://www.googletagmanager.com/gtag/js?id={GA_ID}"></script>
  <script>
    window.dataLayer = window.dataLayer || [];
    function gtag(){{dataLayer.push(arguments);}}
    gtag('js', new Date());
    gtag('config', '{GA_ID}');
  </script>
  <meta name="description" content="{esc_desc}">
  <meta name="robots" content="index, follow, max-image-preview:large">
  <link rel="canonical" href="{canonical_url}">
  <link rel="alternate" type="application/rss+xml" title="Xijie Gong Blog" href="{SITE_URL}/blog/feed.xml">
  <meta property="og:type" content="{html.escape(og_type)}">
  <meta property="og:site_name" content="Xijie Gong">
  <meta property="og:title" content="{esc_title}">
  <meta property="og:description" content="{esc_desc}">
  <meta property="og:url" content="{canonical_url}">
  <meta property="og:image" content="{html.escape(image_url)}">
  <meta name="twitter:card" content="{twitter_card}">
  <meta name="twitter:title" content="{esc_title}">
  <meta name="twitter:description" content="{esc_desc}">
  <meta name="twitter:image" content="{html.escape(image_url)}">{extra_block}{json_ld_block}
  <link rel="icon" type="image/svg+xml" href="{root_prefix}assets/favicon.svg">
  <link rel="stylesheet" href="{root_prefix}assets/fonts/fonts.css">
  <link rel="stylesheet" href="{root_prefix}style.css">{katex_link}
  <link rel="stylesheet" href="{root_prefix}blog/blog.css">
</head>"""


def render_navbar(root_prefix: str, blog_href: str, with_progress: bool = False) -> str:
    progress_html = (
        '\n    <div class="nav-progress" aria-hidden="true"><div class="nav-progress-bar" id="nav-progress-bar"></div></div>'
        if with_progress
        else ""
    )
    return f"""  <!-- TOP NAVBAR -->
  <nav class="navbar">
    <div class="nav-container">
      <a class="nav-brand" href="{root_prefix}">Xijie Gong</a>
      <ul class="nav-links">
        <li><a class="nav-link" href="{root_prefix}#about">About</a></li>
        <li><a class="nav-link" href="{root_prefix}#news">News</a></li>
        <li><a class="nav-link" href="{root_prefix}#publications">Publications</a></li>
        <li><a class="nav-link" href="{root_prefix}#experience">Experience</a></li>
        <li><a class="nav-link" href="{root_prefix}#education">Education</a></li>
        <li><a class="nav-link" href="{root_prefix}#awards">Honors</a></li>
        <li><a class="nav-link nav-link-keep active" href="{blog_href}">Blog</a></li>
      </ul>
    </div>{progress_html}
  </nav>"""


def render_footer() -> str:
    now_str = dt.date.today().strftime("%B %Y")
    year = dt.date.today().year
    return f"""    <!-- FOOTER -->
    <footer class="site-footer">
      <span>&copy; {year} Xijie Gong (龚熙杰)</span>
      <span>Last updated: {now_str}</span>
    </footer>"""


def render_floating_controls() -> str:
    return """<button type="button" id="floating-back-btn" class="floating-btn floating-back-btn" aria-label="Back to previous position" title="Back to previous position">
  <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M9 14 4 9 9 4"/><path d="M20 20v-7a4 4 0 0 0-4-4H4"/></svg>
</button>
<a href="#top" id="top-link" class="floating-btn top-link" aria-label="Go to top" title="Go to top">
  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m18 15-6-6-6 6"/></svg>
</a>"""


SHARED_READING_JS = r"""
  <script>
    function toggleBib(id) {
      const box = document.getElementById(id);
      if (box) box.classList.toggle('show');
    }

    function copyBib(id) {
      const box = document.getElementById(id);
      if (!box) return;
      const text = box.innerText.replace(/^Copy\s*/, '').trim();
      navigator.clipboard.writeText(text).then(() => {
        const btn = box.querySelector('.copy-btn');
        if (btn) {
          btn.textContent = 'Copied!';
          setTimeout(() => { btn.textContent = 'Copy'; }, 1800);
        }
      });
    }

    function copyCode(btn) {
      const pre = btn.parentElement.querySelector('pre');
      if (!pre) return;
      navigator.clipboard.writeText(pre.innerText).then(() => {
        const prev = btn.textContent;
        btn.textContent = 'Copied!';
        setTimeout(() => { btn.textContent = prev; }, 1800);
      });
    }

    // 1.5px reading progress bar docked to .navbar
    (function() {
      const bar = document.getElementById('nav-progress-bar');
      if (!bar) return;
      function update() {
        const h = document.documentElement.scrollHeight - window.innerHeight;
        bar.style.width = (h > 0 ? Math.min(100, (window.scrollY / h) * 100) : 0) + '%';
      }
      window.addEventListener('scroll', update, { passive: true });
      window.addEventListener('resize', update);
      update();
    })();

    // Sticky TOC active section highlighter
    (function() {
      const links = Array.from(document.querySelectorAll('#sticky-toc a'));
      if (!links.length) return;
      const targets = links
        .map(a => document.getElementById(decodeURIComponent(a.getAttribute('href').slice(1))))
        .filter(Boolean);
      function onScroll() {
        let idx = 0;
        const y = window.scrollY + 135;
        for (let i = 0; i < targets.length; i++) {
          if (targets[i].offsetTop <= y) idx = i;
        }
        links.forEach((a, i) => a.classList.toggle('active', i === idx));
      }
      window.addEventListener('scroll', onScroll, { passive: true });
      onScroll();
    })();

    // Husky'Log-style anchor jump + target flash + floating return button + go-to-top
    (function() {
      const topBtn = document.getElementById('top-link');
      const backBtn = document.getElementById('floating-back-btn');
      let savedPos = null;
      let flashTimer = null;

      function updateTopBtn() {
        if (!topBtn) return;
        topBtn.classList.toggle('is-visible', window.scrollY > 600);
      }

      function flashTarget(el) {
        if (!el) return;
        el.classList.remove('anchor-target-flash');
        void el.offsetWidth;
        el.classList.add('anchor-target-flash');
        if (flashTimer) clearTimeout(flashTimer);
        flashTimer = setTimeout(() => el.classList.remove('anchor-target-flash'), 1800);
      }

      document.addEventListener('click', e => {
        const a = e.target.closest('a[href^="#"]');
        if (!a) return;
        const hash = a.getAttribute('href').slice(1);
        if (!hash) return;
        e.preventDefault();
        if (hash === 'top') {
          window.scrollTo({ top: 0, behavior: 'smooth' });
          history.replaceState(null, '', window.location.pathname);
          return;
        }
        const target = document.getElementById(decodeURIComponent(hash));
        if (!target) return;
        savedPos = window.scrollY;
        if (backBtn) backBtn.classList.add('is-visible');
        history.pushState(null, '', '#' + hash);
        const y = Math.max(0, window.scrollY + target.getBoundingClientRect().top - 96);
        window.scrollTo({ top: y, behavior: 'smooth' });
        setTimeout(() => flashTarget(target), 220);
      });

      if (backBtn) {
        backBtn.addEventListener('click', () => {
          if (savedPos === null) {
            backBtn.classList.remove('is-visible');
            return;
          }
          window.scrollTo({ top: savedPos, behavior: 'smooth' });
          savedPos = null;
          backBtn.classList.remove('is-visible');
          history.replaceState(null, '', window.location.pathname);
        });
      }

      window.addEventListener('scroll', updateTopBtn, { passive: true });
      updateTopBtn();
    })();

    // Click-to-zoom lightbox on figures
    (function() {
      const imgs = document.querySelectorAll('.tex-figure img, .post-figure img');
      if (!imgs.length) return;
      const overlay = document.createElement('div');
      overlay.className = 'img-lightbox';
      document.body.appendChild(overlay);

      imgs.forEach(img => {
        img.addEventListener('click', () => {
          overlay.innerHTML = '';
          const fullImg = document.createElement('img');
          fullImg.src = img.src;
          fullImg.alt = img.alt || '';
          overlay.appendChild(fullImg);
          overlay.classList.add('open');
        });
      });
      overlay.addEventListener('click', () => overlay.classList.remove('open'));
      document.addEventListener('keydown', e => {
        if (e.key === 'Escape') overlay.classList.remove('open');
      });
    })();
  </script>"""


# ---------------------------------------------------------------------------
# Page Renderers
# ---------------------------------------------------------------------------

def render_blog_index(posts: list[Post]) -> str:
    head = render_head(
        title="Blog · Xijie Gong (龚熙杰)",
        description="Research notes, papers, and reflections on foundation models, the science of LLMs, and interpretability.",
        root_prefix="../",
        canonical_path="blog/",
        include_katex_css=True,
    )
    navbar = render_navbar(root_prefix="../", blog_href="./", with_progress=False)

    tag_counts: dict[str, int] = {}
    for p in posts:
        for t in p.tags:
            tag_counts[t] = tag_counts.get(t, 0) + 1
    sorted_tags = sorted(tag_counts.keys(), key=lambda t: (-tag_counts[t], t.lower()))

    btns = ['<button class="filter-btn active" type="button" onclick="filterPosts(\'all\', this)">All</button>']
    for tag in sorted_tags:
        btns.append(
            f"<button class=\"filter-btn\" type=\"button\" onclick='filterPosts({json.dumps(tag)}, this)'>{html.escape(tag)}</button>"
        )
    filter_html = '      <div class="pub-filter-row">\n        ' + "\n        ".join(btns) + "\n      </div>\n"

    if not posts:
        list_html = '      <div class="blog-empty">No posts published yet.</div>'
    else:
        groups: dict[int, list[Post]] = {}
        for p in posts:
            groups.setdefault(p.year, []).append(p)
        blocks: list[str] = []
        for year in sorted(groups.keys(), reverse=True):
            items: list[str] = []
            for p in groups[year]:
                tags_attr = html.escape("|".join(p.tags))
                draft_html = ' <span class="draft-badge">DRAFT</span>' if p.draft else ""
                subtitle_html = (
                    f'\n              <div class="post-row-sub">{html.escape(p.subtitle)}</div>'
                    if p.subtitle
                    else ""
                )
                excerpt_html = (
                    f'\n              <p class="post-row-excerpt">{html.escape(p.summary)}</p>'
                    if p.summary
                    else ""
                )
                meta_parts = [f"{p.reading_minutes} min read"]
                if p.word_count > 0:
                    meta_parts.append(f"{p.word_count:,} words")
                if p.authors:
                    meta_parts.append(html.escape(p.authors))
                meta_str = " &middot; ".join(meta_parts)

                tags_html = ""
                if p.tags:
                    t_spans = "".join(f"<span>{html.escape(t)}</span>" for t in p.tags)
                    tags_html = f'<span class="post-row-sep">&middot;</span><span class="post-row-tags">{t_spans}</span>'

                items.append(
                    f"""          <article class="post-row" data-tags="{tags_attr}">
            <div class="post-row-date">{html.escape(p.date_display)}</div>
            <div class="post-row-body">
              <h2 class="post-row-title"><a href="{html.escape(p.slug)}/">{html.escape(p.title)}</a>{draft_html}</h2>{subtitle_html}{excerpt_html}
              <div class="post-row-meta">
                <span>{meta_str}</span>{tags_html}
              </div>
            </div>
          </article>"""
                )
            blocks.append(
                f"""      <div class="blog-year-group" data-year="{year}">
        <div class="blog-year-title">{year}</div>
        <div class="post-list">
{chr(10).join(items)}
        </div>
      </div>"""
            )
        list_html = "\n".join(blocks)

    return f"""{head}
<body id="top">

{navbar}

  <div class="page-wrap">
    <header class="blog-header">
      <h1 class="blog-heading">Blog</h1>
      <p class="blog-lead">A quiet corner for research updates, personal thoughts, and occasional ramblings.</p>
    </header>

    <section class="section">
{filter_html}{list_html}
    </section>

{render_footer()}
  </div>

  <script>
    function filterPosts(tag, btn) {{
      document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      document.querySelectorAll('.post-row').forEach(item => {{
        const raw = item.getAttribute('data-tags') || '';
        const tags = raw ? raw.split('|') : [];
        item.style.display = (tag === 'all' || tags.includes(tag)) ? '' : 'none';
      }});
      document.querySelectorAll('.blog-year-group').forEach(g => {{
        const anyVisible = Array.from(g.querySelectorAll('.post-row')).some(i => i.style.display !== 'none');
        g.style.display = anyVisible ? '' : 'none';
      }});
    }}
  </script>
</body>
</html>
"""


def render_latex_paper_page(post: Post) -> str:
    compiled = post.compiled_paper
    assert compiled is not None
    cfg = post.paper_config
    pdf_url = str(cfg.get("pdf_url", "")).strip()
    code_url = str(cfg.get("code_url", "")).strip()
    canonical_url = f"{SITE_URL}/blog/{post.slug}/"
    abs_pdf_url = f"{SITE_URL}{pdf_url}" if pdf_url.startswith("/") else pdf_url

    # Prefer Figure 1 as rich social/Google Discover preview image
    og_image = ""
    if (BLOG_DIR / post.slug / "figure1.png").is_file():
        og_image = f"{SITE_URL}/blog/{post.slug}/figure1.png"

    author_names = [a.strip() for a in post.authors.split(",") if a.strip()] or [DEFAULT_AUTHOR]
    raw_kw = cfg.get("keywords", [])
    seo_keywords = [str(k).strip() for k in raw_kw] if isinstance(raw_kw, list) and raw_kw else post.tags
    scholar_meta_lines = [
        f'  <meta name="author" content="{html.escape(", ".join(author_names))}">',
        f'  <meta name="keywords" content="{html.escape(", ".join(seo_keywords))}">',
        f'  <meta property="article:published_time" content="{post.date.isoformat()}">',
        f'  <meta name="citation_title" content="{html.escape(compiled.title)}">',
    ]
    for a_name in author_names:
        scholar_meta_lines.append(f'  <meta name="citation_author" content="{html.escape(a_name)}">')
    scholar_meta_lines.append(f'  <meta name="citation_publication_date" content="{post.date.strftime("%Y/%m/%d")}">')
    scholar_meta_lines.append(f'  <meta name="citation_fulltext_html_url" content="{canonical_url}">')
    scholar_meta_lines.append(f'  <meta name="citation_abstract_html_url" content="{canonical_url}">')
    if abs_pdf_url:
        scholar_meta_lines.append(f'  <meta name="citation_pdf_url" content="{html.escape(abs_pdf_url)}">')
    if seo_keywords:
        scholar_meta_lines.append(f'  <meta name="citation_keywords" content="{html.escape("; ".join(seo_keywords))}">')

    json_ld: dict[str, Any] = {
        "@context": "https://schema.org",
        "@type": "ScholarlyArticle",
        "headline": compiled.title,
        "name": compiled.title,
        "description": post.summary or compiled.title,
        "datePublished": post.date.isoformat(),
        "dateModified": dt.date.today().isoformat(),
        "url": canonical_url,
        "mainEntityOfPage": {"@type": "WebPage", "@id": canonical_url},
        "author": [
            {
                "@type": "Person",
                "name": name,
                "affiliation": {"@type": "Organization", "name": strip_html_tags(compiled.affiliation_html)},
            }
            for name in author_names
        ],
        "publisher": {"@type": "Person", "name": DEFAULT_AUTHOR, "url": f"{SITE_URL}/"},
        "keywords": seo_keywords,
        "inLanguage": "en",
    }
    if og_image:
        json_ld["image"] = og_image
    if code_url:
        json_ld["sameAs"] = [code_url]
    if abs_pdf_url:
        json_ld["encoding"] = {
            "@type": "MediaObject",
            "encodingFormat": "application/pdf",
            "contentUrl": abs_pdf_url,
        }

    head = render_head(
        title=f"{compiled.title} · Xijie Gong",
        description=post.summary or compiled.title,
        root_prefix="../../",
        canonical_path=f"blog/{post.slug}/",
        include_katex_css=True,
        og_type="article",
        og_image=og_image,
        extra_meta_html="\n".join(scholar_meta_lines),
        json_ld=json_ld,
    )
    navbar = render_navbar(root_prefix="../../", blog_href="../", with_progress=True)

    toc_li_html = "\n".join(
        f'          <li class="{"toc-sub" if item.level == 3 else ""}"><a href="#{html.escape(item.ident)}">{html.escape(item.label)}</a></li>'
        for item in compiled.toc
    )

    subtitle_html = (
        f'\n          <p class="article-subtitle">{html.escape(post.subtitle)}</p>'
        if post.subtitle
        else ""
    )
    meta_parts = [html.escape(post.date_display), f"{post.reading_minutes} min read", f"{post.word_count:,} words"]
    meta_line = " &middot; ".join(meta_parts)

    res_btns: list[str] = []
    if pdf_url:
        res_btns.append(f'<a class="btn-link" href="{html.escape(pdf_url)}" target="_blank" rel="noopener">[Paper]</a>')
    if code_url:
        res_btns.append(f'<a class="btn-link" href="{html.escape(code_url)}" target="_blank" rel="noopener">[Code]</a>')
    res_btns.append('<button class="btn-link" type="button" onclick="toggleBib(\'bib-paper\')">[BibTeX]</button>')
    res_btns_html = "\n            ".join(res_btns)

    tags_footer = ""
    if post.tags:
        tags_footer = (
            '\n        <div class="article-tags-footer">'
            + "".join(f'<span class="article-tag-chip">{html.escape(t)}</span>' for t in post.tags)
            + "</div>"
        )

    esc_bib = html.escape(compiled.bibtex_self)
    tooltips_json = json.dumps(compiled.ref_tooltips, ensure_ascii=False)

    return f"""{head}
<body id="top">

{navbar}

  <div class="reading-shell">
    <div class="reading-layout">
      <aside class="sticky-toc" id="sticky-toc" aria-label="Table of Contents">
        <a class="toc-back" href="../">&larr; All Posts</a>
        <div class="toc-title">On this page</div>
        <nav>
          <ul>
{toc_li_html}
          </ul>
        </nav>
      </aside>

      <main class="reading-main">
        <div class="reading-mobile-nav">
          <a class="toc-back" href="../">&larr; All Posts</a>
          <details class="toc-mobile">
            <summary>Contents</summary>
            <ul>
{toc_li_html}
            </ul>
          </details>
        </div>

        <article>
          <header class="article-header">
            <h1 class="article-title">{html.escape(compiled.title)}</h1>{subtitle_html}
            <div class="article-byline">
              <span class="article-authors">{compiled.authors_html}</span> &middot;
              <span class="article-affil">{compiled.affiliation_html}</span>
            </div>
            <div class="article-notes">{compiled.notes_html}</div>
            <div class="article-meta">{meta_line}</div>
            <div class="article-actions">
              {res_btns_html}
            </div>
            <div class="bibtex-pane" id="bib-paper">
              <button class="copy-btn" type="button" onclick="copyBib('bib-paper')">Copy</button>{esc_bib}</div>
          </header>

          <section class="callout-block" id="abstract">
            <div class="callout-header"><span class="callout-title">Abstract</span></div>
            <div class="callout-content">
              <p>{compiled.abstract_html}</p>
            </div>
          </section>

          <div class="post-content">
{compiled.body_html}
{compiled.references_html}
          </div>{tags_footer}
        </article>
      </main>
    </div>
  </div>

  <div class="reading-footer-wrap">
{render_footer()}
  </div>

{render_floating_controls()}

  <div class="cite-tooltip" id="cite-tooltip" role="tooltip"></div>

  <script defer src="../../assets/katex/copy-tex.min.js"></script>
{SHARED_READING_JS}
  <script>
    // Citation hover preview tooltip
    (function() {{
      const refs = {tooltips_json};
      const tip = document.getElementById('cite-tooltip');
      if (!tip) return;
      document.querySelectorAll('a.tex-cite[data-ref]').forEach(link => {{
        const text = refs[link.getAttribute('data-ref')];
        if (!text) return;
        link.addEventListener('mouseenter', e => {{
          tip.textContent = text;
          tip.style.display = 'block';
          positionTip(e);
        }});
        link.addEventListener('mousemove', positionTip);
        link.addEventListener('mouseleave', () => {{ tip.style.display = 'none'; }});
      }});
      function positionTip(e) {{
        const pad = 14;
        let x = e.clientX + pad;
        let y = e.clientY + pad;
        const rect = tip.getBoundingClientRect();
        if (x + rect.width > window.innerWidth - 12) x = window.innerWidth - rect.width - 12;
        if (y + rect.height > window.innerHeight - 12) y = e.clientY - rect.height - pad;
        tip.style.left = Math.max(12, x) + 'px';
        tip.style.top = Math.max(12, y) + 'px';
      }}
    }})();
  </script>
</body>
</html>
"""


def render_markdown_post_page(post: Post, prev_post: Post | None, next_post: Post | None) -> str:
    canonical_url = f"{SITE_URL}/blog/{post.slug}/"
    author_names = [a.strip() for a in post.authors.split(",") if a.strip()] or [DEFAULT_AUTHOR]
    extra_meta = "\n".join(
        [
            f'  <meta name="author" content="{html.escape(", ".join(author_names))}">',
            f'  <meta property="article:published_time" content="{post.date.isoformat()}">',
        ]
        + ([f'  <meta name="keywords" content="{html.escape(", ".join(post.tags))}">'] if post.tags else [])
    )
    json_ld = {
        "@context": "https://schema.org",
        "@type": "BlogPosting",
        "headline": post.title,
        "description": post.summary or post.title,
        "datePublished": post.date.isoformat(),
        "url": canonical_url,
        "mainEntityOfPage": {"@type": "WebPage", "@id": canonical_url},
        "author": [{"@type": "Person", "name": name} for name in author_names],
        "keywords": post.tags,
    }
    head = render_head(
        title=f"{post.title} · Xijie Gong",
        description=post.summary or post.title,
        root_prefix="../../",
        canonical_path=f"blog/{post.slug}/",
        include_katex_css=post.use_math,
        og_type="article",
        extra_meta_html=extra_meta,
        json_ld=json_ld,
    )
    navbar = render_navbar(root_prefix="../../", blog_href="../", with_progress=True)

    subtitle_html = (
        f'\n          <p class="article-subtitle">{html.escape(post.subtitle)}</p>'
        if post.subtitle
        else ""
    )
    draft_html = ' <span class="draft-badge">DRAFT</span>' if post.draft else ""
    meta_parts = [html.escape(post.date_display), f"{post.reading_minutes} min read", f"{post.word_count:,} words"]
    if post.authors:
        meta_parts.append(html.escape(post.authors))
    meta_line = " &middot; ".join(meta_parts)

    toc_aside_html = ""
    toc_mobile_html = ""
    if post.show_toc:
        toc_li = "\n".join(
            f'          <li class="{"toc-sub" if item.level == 3 else ""}"><a href="#{html.escape(item.ident)}">{item.title}</a></li>'
            for item in post.toc
        )
        toc_aside_html = f"""      <aside class="sticky-toc" id="sticky-toc" aria-label="Table of Contents">
        <a class="toc-back" href="../">&larr; All Posts</a>
        <div class="toc-title">On this page</div>
        <nav>
          <ul>
{toc_li}
          </ul>
        </nav>
      </aside>"""
        toc_mobile_html = f"""        <div class="reading-mobile-nav">
          <a class="toc-back" href="../">&larr; All Posts</a>
          <details class="toc-mobile">
            <summary>Contents</summary>
            <ul>
{toc_li}
            </ul>
          </details>
        </div>"""
    else:
        toc_mobile_html = '        <div style="margin-bottom:1.2rem"><a class="toc-back" href="../">&larr; All Posts</a></div>'

    tags_footer = ""
    if post.tags:
        tags_footer = (
            '\n        <div class="article-tags-footer">'
            + "".join(f'<span class="article-tag-chip">{html.escape(t)}</span>' for t in post.tags)
            + "</div>"
        )

    nav_html = ""
    if prev_post or next_post:
        p_cell = (
            f'<div class="post-nav-item"><span class="post-nav-label">&larr; Older</span><a class="post-nav-title" href="../{html.escape(prev_post.slug)}/">{html.escape(prev_post.title)}</a></div>'
            if prev_post
            else '<div class="post-nav-item"></div>'
        )
        n_cell = (
            f'<div class="post-nav-item post-nav-next"><span class="post-nav-label">Newer &rarr;</span><a class="post-nav-title" href="../{html.escape(next_post.slug)}/">{html.escape(next_post.title)}</a></div>'
            if next_post
            else '<div class="post-nav-item post-nav-next"></div>'
        )
        nav_html = f'\n        <nav class="post-nav">{p_cell}{n_cell}</nav>'

    katex_scripts = ""
    if post.use_math:
        macros_json = json.dumps(post.macros)
        katex_scripts = f"""
  <script defer src="../../assets/katex/katex.min.js"></script>
  <script defer src="../../assets/katex/copy-tex.min.js"></script>
  <script defer src="../../assets/katex/auto-render.min.js"></script>
  <script>
    document.addEventListener('DOMContentLoaded', function() {{
      if (window.renderMathInElement) {{
        renderMathInElement(document.body, {{
          delimiters: [
            {{left: '\\\\[', right: '\\\\]', display: true}},
            {{left: '$$', right: '$$', display: true}},
            {{left: '\\\\(', right: '\\\\)', display: false}},
            {{left: '$', right: '$', display: false}}
          ],
          macros: {macros_json},
          throwOnError: false
        }});
      }}
    }});
  </script>"""

    return f"""{head}
<body id="top">

{navbar}

  <div class="reading-shell">
    <div class="reading-layout">
{toc_aside_html}
      <main class="reading-main">
{toc_mobile_html}
        <article>
          <header class="article-header">
            <h1 class="article-title">{html.escape(post.title)}{draft_html}</h1>{subtitle_html}
            <div class="article-meta">{meta_line}</div>
          </header>
          <div class="post-content">
{post.html_body}
          </div>{tags_footer}{nav_html}
        </article>
      </main>
    </div>
  </div>

  <div class="reading-footer-wrap">
{render_footer()}
  </div>

{render_floating_controls()}
{katex_scripts}
{SHARED_READING_JS}
</body>
</html>
"""


def write_sitemap_and_rss(posts: list[Post]) -> None:
    today_iso = dt.date.today().isoformat()
    pub_posts = [p for p in posts if not p.draft]

    # 1. Root sitemap.xml (prioritize HTML web editions over raw PDFs)
    urls: list[tuple[str, str, str, str]] = [
        (f"{SITE_URL}/", today_iso, "weekly", "1.0"),
        (f"{SITE_URL}/blog/", today_iso, "weekly", "0.9"),
    ]
    seen_pdfs: set[str] = set()
    for p in pub_posts:
        urls.append((f"{SITE_URL}/blog/{p.slug}/", p.date.isoformat(), "monthly", "0.9"))
        if p.is_latex_paper:
            pdf_url = str(p.paper_config.get("pdf_url", "")).strip()
            if pdf_url.startswith("/") and pdf_url not in seen_pdfs:
                seen_pdfs.add(pdf_url)
                urls.append((f"{SITE_URL}{pdf_url}", p.date.isoformat(), "monthly", "0.6"))

    url_nodes = "\n".join(
        f"  <url>\n"
        f"    <loc>{html.escape(loc)}</loc>\n"
        f"    <lastmod>{lastmod}</lastmod>\n"
        f"    <changefreq>{freq}</changefreq>\n"
        f"    <priority>{prio}</priority>\n"
        f"  </url>"
        for loc, lastmod, freq, prio in urls
    )
    sitemap_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        f"{url_nodes}\n"
        "</urlset>\n"
    )
    (ROOT / "sitemap.xml").write_text(sitemap_xml, encoding="utf-8")

    # 2. RSS 2.0 feed at blog/feed.xml
    now_rfc = format_datetime(dt.datetime.now(dt.timezone.utc))
    rss_items: list[str] = []
    for p in pub_posts:
        link = f"{SITE_URL}/blog/{p.slug}/"
        pub_dt = dt.datetime(p.date.year, p.date.month, p.date.day, tzinfo=dt.timezone.utc)
        cats = "".join(f"\n      <category>{html.escape(t)}</category>" for t in p.tags)
        rss_items.append(
            f"    <item>\n"
            f"      <title>{html.escape(p.title)}</title>\n"
            f"      <link>{link}</link>\n"
            f"      <guid isPermaLink=\"true\">{link}</guid>\n"
            f"      <pubDate>{format_datetime(pub_dt)}</pubDate>\n"
            f"      <description>{html.escape(p.summary or p.title)}</description>{cats}\n"
            f"    </item>"
        )
    rss_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">\n'
        "  <channel>\n"
        "    <title>Xijie Gong — Blog</title>\n"
        f"    <link>{SITE_URL}/blog/</link>\n"
        "    <description>Research notes, paper editions, and thoughts on foundation models, the science of LLMs, and interpretability.</description>\n"
        "    <language>en</language>\n"
        f"    <lastBuildDate>{now_rfc}</lastBuildDate>\n"
        f'    <atom:link href="{SITE_URL}/blog/feed.xml" rel="self" type="application/rss+xml"/>\n'
        f"{chr(10).join(rss_items)}\n"
        "  </channel>\n"
        "</rss>\n"
    )
    (BLOG_DIR / "feed.xml").write_text(rss_xml, encoding="utf-8")


# ---------------------------------------------------------------------------
# Build Pipeline & Local Threaded Dev Server
# ---------------------------------------------------------------------------

def build_blog(include_drafts: bool = False, quiet: bool = False) -> list[Post]:
    POSTS_DIR.mkdir(parents=True, exist_ok=True)
    BLOG_DIR.mkdir(parents=True, exist_ok=True)

    sources = discover_post_sources()
    all_posts: list[Post] = []
    seen_slugs: set[str] = set()

    for md_path, default_slug, fallback_date, asset_dir in sources:
        post = load_post(md_path, default_slug, fallback_date, asset_dir)
        if post.draft and not include_drafts:
            continue
        if post.slug in seen_slugs:
            raise ValueError(f"Duplicate post slug '{post.slug}' from {md_path}")
        seen_slugs.add(post.slug)
        all_posts.append(post)

    all_posts.sort(key=lambda p: (p.date, p.slug), reverse=True)

    for child in BLOG_DIR.iterdir():
        if child.is_dir():
            shutil.rmtree(child)

    # Compile LaTeX papers first so accurate word counts & reading minutes appear on blog/index.html too!
    for post in all_posts:
        out_dir = BLOG_DIR / post.slug
        out_dir.mkdir(parents=True, exist_ok=True)
        if post.is_latex_paper:
            cfg = post.paper_config
            source_dir = Path(cfg["source_dir"]).expanduser()
            if not source_dir.is_absolute():
                source_dir = (post.source_path.parent / source_dir).resolve()
            compiled = tex_paper.compile_latex_paper(
                paper_dir=source_dir,
                out_asset_dir=out_dir,
                pdf_url=str(cfg.get("pdf_url", "")).strip() or "#",
                code_url=str(cfg.get("code_url", "")).strip(),
            )
            post.compiled_paper = compiled
            words, mins = count_words_and_minutes(compiled.abstract_html + " " + compiled.body_html)
            post.word_count = words
            post.reading_minutes = mins

    (BLOG_DIR / "index.html").write_text(render_blog_index(all_posts), encoding="utf-8")

    for idx, post in enumerate(all_posts):
        out_dir = BLOG_DIR / post.slug
        if post.is_latex_paper:
            (out_dir / "index.html").write_text(render_latex_paper_page(post), encoding="utf-8")
            continue

        if post.asset_dir is not None:
            for item in post.asset_dir.iterdir():
                if item.name in ("index.md", "paper.yaml") or item.name.startswith("."):
                    continue
                dest = out_dir / item.name
                if item.is_dir():
                    shutil.copytree(item, dest, dirs_exist_ok=True)
                else:
                    shutil.copy2(item, dest)

        next_post = all_posts[idx - 1] if idx > 0 else None
        prev_post = all_posts[idx + 1] if idx + 1 < len(all_posts) else None
        (out_dir / "index.html").write_text(
            render_markdown_post_page(post, prev_post=prev_post, next_post=next_post),
            encoding="utf-8",
        )

    write_sitemap_and_rss(all_posts)

    if not quiet:
        print(f"Built {len(all_posts)} post(s) -> {BLOG_DIR}/", flush=True)
        for p in all_posts:
            flag = " [DRAFT]" if p.draft else ""
            print(f"  - {p.date_display}  /blog/{p.slug}/  ({p.word_count:,} words, {p.reading_minutes} min)  {p.title}{flag}", flush=True)

    return all_posts


def create_new_post(slug_arg: str, title_arg: str | None) -> Path:
    POSTS_DIR.mkdir(parents=True, exist_ok=True)
    today = dt.date.today().isoformat()
    clean_slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", slug_arg.strip()).strip("-").lower()
    if not clean_slug:
        raise ValueError("Slug must contain at least one alphanumeric character.")
    title = title_arg or clean_slug.replace("-", " ").title()
    target = POSTS_DIR / f"{today}-{clean_slug}.md"
    if target.exists():
        raise FileExistsError(f"Post already exists: {target}")

    template = f"""---
title: "{title}"
subtitle: ""
authors: "{DEFAULT_AUTHOR}"
date: {today}
tags: [Foundation Models, Notes]
summary: ""
draft: false
---

Write your introduction here. Inline math like $h_t \\in \\R^d$ works out of the box.

## 1. Introduction

Display equations are rendered cleanly with KaTeX:

$$
\\mathcal{{L}}(\\theta) = \\E_{{x \\sim \\mathcal{{D}}}} \\left[ -\\log p_\\theta(x) \\right]
$$

> [!NOTE] Observation
> Use subtle editorial callout boxes for key takeaways or remarks.
"""
    target.write_text(template, encoding="utf-8")
    print(f"Created new post: {target}", flush=True)
    return target


def snapshot_posts_mtimes() -> dict[Path, float]:
    state: dict[Path, float] = {}
    if POSTS_DIR.exists():
        for p in POSTS_DIR.rglob("*"):
            if p.is_file() and not p.name.startswith("."):
                state[p] = p.stat().st_mtime
    return state


def serve_local(port: int, include_drafts: bool) -> None:
    build_blog(include_drafts=include_drafts)

    def watch_loop() -> None:
        last_state = snapshot_posts_mtimes()
        while True:
            time.sleep(1.0)
            curr_state = snapshot_posts_mtimes()
            if curr_state != last_state:
                last_state = curr_state
                try:
                    print("\n[watch] Change detected in posts/, rebuilding...", flush=True)
                    build_blog(include_drafts=include_drafts)
                except Exception as exc:
                    print(f"[watch] Build error: {exc}", flush=True)

    threading.Thread(target=watch_loop, daemon=True).start()

    class NoCacheHandler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, directory=str(ROOT), **kwargs)

        def setup(self) -> None:
            super().setup()
            self.request.settimeout(15.0)

        def end_headers(self) -> None:
            self.send_header("Cache-Control", "no-store, must-revalidate")
            super().end_headers()

    class ThreadedServer(http.server.ThreadingHTTPServer):
        allow_reuse_address = True
        daemon_threads = True

    with ThreadedServer(("0.0.0.0", port), NoCacheHandler) as httpd:
        print(f"\nServing homepage at http://localhost:{port}/", flush=True)
        print(f"Serving blog at     http://localhost:{port}/blog/", flush=True)
        print("Editing files in posts/ will automatically rebuild. Press Ctrl+C to stop.", flush=True)
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nStopped local server.", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Build static blog for xijiegong.com")
    parser.add_argument("--drafts", action="store_true", help="Include draft: true posts")
    parser.add_argument("--new", metavar="SLUG", help="Create a new post template in posts/")
    parser.add_argument("-t", "--title", help="Title when using --new")
    parser.add_argument("--serve", action="store_true", help="Build, watch posts/, and serve locally")
    parser.add_argument("--port", type=int, default=8000, help="Port for --serve (default: 8000)")
    args = parser.parse_args()

    if args.new:
        create_new_post(args.new, args.title)
        build_blog(include_drafts=args.drafts)
        return

    if args.serve:
        serve_local(port=args.port, include_drafts=True)
        return

    build_blog(include_drafts=args.drafts)


if __name__ == "__main__":
    main()
