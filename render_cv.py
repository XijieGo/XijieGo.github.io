#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""Render MyResume.md -> MyResume.pdf (A4, Academic Style via Headless Chromium).

Academic Style Design:
  - Typography: Pure Times New Roman (LaTeX / Academic standard)
  - 3 Font Sizes: Large (大: Name), Medium (中: Section Headings), Small (小: Body)
  - Selective Bolding: Bold where appropriate (Name, Headings, Institutions, Paper Titles, Awards, Skills categories); Normal elsewhere
  - True Black & White: #ffffff background, #000000 text and rules, no grey artifacts
  - Single-page A4 layout with clean margins and balanced spacing
"""
import os
import re
import shutil
from pathlib import Path
import yaml
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "MyResume.md"
OUT_PDF = ROOT / "MyResume.pdf"
ASSET_PDF = ROOT / "assets" / "cv.pdf"

BUILD = Path("/home/xijie/.tmp-pw/cv-build")
CHROME = "/home/xijie/.cache/ms-playwright/chromium_headless_shell-1217/chrome-headless-shell-linux64/chrome-headless-shell"

os.environ.setdefault("TMPDIR", "/home/xijie/.tmp-pw")
BUILD.mkdir(parents=True, exist_ok=True)

ICON_RE = re.compile(
    r'<span class="iconify"[^>]*>\s*</span>|<span class="iconify"[^>]*/>|<span class="iconify"[^>]*>|</span>'
)
LINK_RE = re.compile(r"\[([^\[\]]+)\]\(([^)\s]+)\)")
BOLD_RE = re.compile(r"\*\*([^*]+)\*\*")
ITAL_RE = re.compile(r"(?<!\*)\*([^*]+)\*(?!\*)")
TEX_RE = re.compile(r"\$([^$]+)\$")


def inline(s: str) -> str:
    s = ICON_RE.sub("", s).strip()
    s = LINK_RE.sub(lambda m: f'<a href="{m.group(2)}">{m.group(1)}</a>', s)
    s = BOLD_RE.sub(r"<strong>\1</strong>", s)
    s = ITAL_RE.sub(r"<em>\1</em>", s)
    s = TEX_RE.sub(lambda m: m.group(1).replace("\\", ""), s)
    return s


def clean_text(s: str) -> str:
    s = ICON_RE.sub("", s)
    s = re.sub(r"<[^>]+>", "", s)
    return s.strip()


def render_header(meta: dict) -> str:
    name = meta.get("name", "Xijie Gong")
    target_pos = meta.get("target", "").strip()
    raw_header = meta.get("header", [])

    contact_items = []
    for item in raw_header:
        t = str(item.get("text", "")).strip()
        t_clean = clean_text(t)
        if "Target Position:" in t_clean and not target_pos:
            target_pos = t_clean
            continue
        link = item.get("link")
        if not link:
            links = re.findall(r'<a\s+href="([^"]+)">([^<]+)</a>', t)
            if links:
                for l_href, l_text in links:
                    contact_items.append((clean_text(l_text), l_href))
                continue
            else:
                contact_items.append((t_clean, ""))
        else:
            contact_items.append((t_clean, link))

    hdr_lines = [f'<div class="cv-name">{name}</div>']
    if target_pos:
        hdr_lines.append(f'<div class="hdr-target">{target_pos}</div>')

    contact_spans = []
    for txt, lnk in contact_items:
        if not txt:
            continue
        if lnk:
            contact_spans.append(f'<a href="{lnk}">{txt}</a>')
        else:
            contact_spans.append(f"<span>{txt}</span>")

    contact_html = " &middot; ".join(contact_spans)
    hdr_lines.append(f'<div class="hdr-contact">{contact_html}</div>')
    return "\n".join(hdr_lines)


def render_body(body: str) -> str:
    lines = body.splitlines()
    n = len(lines)
    out = []
    ul_open = False
    paper_open = False

    def close_ul():
        nonlocal ul_open
        if ul_open:
            out.append("</ul>")
            ul_open = False

    def close_paper():
        nonlocal paper_open
        if paper_open:
            out.append("</div>")
            paper_open = False

    i = 0
    while i < n:
        line = lines[i].strip()
        if not line:
            i += 1
            continue

        if line.startswith("## "):
            close_ul()
            close_paper()
            sec_title = line[3:].strip()
            out.append(f"<h2>{sec_title}</h2>")
        elif line.startswith("~ "):
            close_ul()
            out.append(
                f'<div class="row"><span class="l"></span><span class="r">{inline(line[2:])}</span></div>'
            )
        elif line.startswith("- "):
            close_paper()
            if not ul_open:
                out.append("<ul>")
                ul_open = True
            out.append(f"<li>{inline(line[2:])}</li>")
        else:
            close_ul()
            m = re.match(r"^\[~P\d+\]:\s*(.*)$", line)
            nxt = lines[i + 1].strip() if i + 1 < n else ""
            if m:
                close_paper()
                paper_open = True
                out.append('<div class="paper">')
                out.append(f'<div class="pt">{inline(m.group(1))}</div>')
            elif nxt.startswith("~ "):
                out.append(
                    f'<div class="row"><span class="l">{inline(line)}</span>'
                    f'<span class="r">{inline(nxt[2:])}</span></div>'
                )
                i += 1
            elif paper_open and line.startswith("*") and not line.startswith("**"):
                out.append(f'<div class="pv">{inline(line)}</div>')
            elif line.startswith("Research Intern,") or line.startswith("B.Eng. in"):
                out.append(f'<div class="sub-role">{inline(line)}</div>')
            else:
                out.append(f"<p>{inline(line)}</p>")
        i += 1

    close_ul()
    close_paper()
    return "\n".join(out)


def build_css() -> str:
    font_family = "'Times New Roman', Times, 'Liberation Serif', serif"
    return f"""
    @page {{
      size: A4;
      margin: 13mm 16mm 13mm 16mm;
    }}
    :root {{
      --fs-lg: 20pt;
      --fs-md: 11.2pt;
      --fs-sm: 9.5pt;
    }}
    * {{
      box-sizing: border-box;
      margin: 0;
      padding: 0;
    }}
    body {{
      font-family: {font_family};
      font-size: var(--fs-sm);
      line-height: 1.37;
      color: #000000;
      background-color: #ffffff;
      -webkit-font-smoothing: antialiased;
    }}
    a {{
      color: #000000;
      text-decoration: none;
    }}
    .cv-header {{
      text-align: center;
      margin-bottom: 11px;
    }}
    .cv-name {{
      font-family: {font_family};
      font-size: var(--fs-lg);
      font-weight: bold;
      letter-spacing: 0.5px;
      line-height: 1.15;
      margin-bottom: 4px;
    }}
    .hdr-target {{
      font-family: {font_family};
      font-size: var(--fs-sm);
      font-weight: normal;
      color: #000000;
      margin-bottom: 2.5px;
    }}
    .hdr-contact {{
      font-family: {font_family};
      font-size: var(--fs-sm);
      font-weight: normal;
      color: #000000;
      line-height: 1.35;
    }}
    .hdr-contact a {{
      color: #000000;
      text-decoration: none;
    }}
    h2 {{
      font-family: {font_family};
      font-size: var(--fs-md);
      font-weight: bold;
      color: #000000;
      text-transform: uppercase;
      letter-spacing: 0.8px;
      margin: 12px 0 4.5px;
      padding-bottom: 2px;
      border-bottom: 0.85pt solid #000000;
      break-after: avoid;
    }}
    p {{
      font-size: var(--fs-sm);
      margin: 0 0 3px;
    }}
    .sub-role {{
      font-size: var(--fs-sm);
      font-style: italic;
      color: #000000;
      margin: 0 0 1.5px;
    }}
    .row {{
      display: flex;
      justify-content: space-between;
      align-items: baseline;
      font-size: var(--fs-sm);
      margin: 0 0 2px;
      break-inside: avoid;
    }}
    .row .l {{
      flex: 1 1 auto;
    }}
    .row .r {{
      flex: 0 0 auto;
      text-align: right;
      white-space: nowrap;
      font-weight: normal;
      color: #000000;
    }}
    ul {{
      margin: 1.5px 0 5px;
      padding-left: 17px;
    }}
    li {{
      font-size: var(--fs-sm);
      margin: 0 0 1.5px;
      line-height: 1.35;
    }}
    .paper {{
      font-size: var(--fs-sm);
      margin: 0 0 6px;
      break-inside: avoid;
    }}
    .paper .pt {{
      font-weight: bold;
      font-size: var(--fs-sm);
      color: #000000;
    }}
    .paper p, .paper .pv {{
      font-size: var(--fs-sm);
      margin: 0.8px 0 0;
      color: #000000;
    }}
    .paper .pv em {{
      font-style: italic;
    }}
    .paper a {{
      color: #000000;
      text-decoration: none;
    }}
    strong {{
      font-weight: bold;
    }}
    u {{
      text-decoration: underline;
      text-underline-offset: 1.5px;
    }}
    em {{
      font-style: italic;
    }}
    """


def build_html(meta: dict, body_html: str) -> str:
    name = meta.get("name", "Xijie Gong")
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{name} - Curriculum Vitae</title>
<style>
{build_css()}
</style>
</head>
<body>
<div class="cv-header">
{render_header(meta)}
</div>
{body_html}
</body>
</html>
"""


def main():
    text = SRC.read_text(encoding="utf-8")
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, re.S)
    if not m:
        raise SystemExit("front matter not found in " + str(SRC))
    meta = yaml.safe_load(m.group(1))
    body = text[m.end():]

    html = build_html(meta, render_body(body))
    html_path = BUILD / "cv.html"
    html_path.write_text(html, encoding="utf-8")

    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROME)
        page = browser.new_page()
        page.goto(html_path.as_uri())
        page.wait_for_timeout(300)
        page.pdf(
            path=str(OUT_PDF),
            format="A4",
            print_background=True,
            margin={"top": "13mm", "bottom": "13mm", "left": "15mm", "right": "15mm"},
        )
        browser.close()

    if ASSET_PDF.parent.exists():
        shutil.copyfile(OUT_PDF, ASSET_PDF)
        print(f"copied to {ASSET_PDF}")
    v3_pdf = ROOT / "version-3-academic" / "assets" / "cv.pdf"
    if v3_pdf.parent.exists():
        shutil.copyfile(OUT_PDF, v3_pdf)
        print(f"copied to {v3_pdf}")

    print(f"wrote {OUT_PDF} ({OUT_PDF.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
