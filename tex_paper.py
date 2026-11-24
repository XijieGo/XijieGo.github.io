#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Convert a LaTeX academic paper (ICML-style .tex + .aux + .bbl + PDF figures)
into a faithful, publication-grade static HTML paper page with build-time KaTeX SSR.
"""

from __future__ import annotations

import html
import json
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pymupdf
from PIL import Image, ImageChops

ROOT = Path(__file__).resolve().parent
KATEX_JS = ROOT / "assets" / "katex" / "katex.min.js"


@dataclass
class BibEntry:
    key: str
    short_author: str
    year: str
    html_body: str
    plain_preview: str


@dataclass
class PaperTocItem:
    level: int
    ident: str
    label: str


@dataclass
class CompiledPaper:
    title: str
    title_html: str
    authors_html: str
    affiliation_html: str
    notes_html: str
    abstract_html: str
    body_html: str
    references_html: str
    toc: list[PaperTocItem]
    bibtex_self: str
    ref_tooltips: dict[str, str]
    figure_assets: list[Path]


# ---------------------------------------------------------------------------
# Balanced-brace helpers
# ---------------------------------------------------------------------------

def read_brace_arg(s: str, pos: int) -> tuple[str, int]:
    """Read a {...} argument starting at pos (skipping leading whitespace)."""
    n = len(s)
    while pos < n and s[pos] in " \t\r\n":
        pos += 1
    if pos >= n or s[pos] != "{":
        return "", pos
    depth = 0
    start = pos + 1
    i = pos
    while i < n:
        ch = s[i]
        if ch == "\\" and i + 1 < n:
            i += 2
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return s[start:i], i + 1
        i += 1
    return s[start:], n


def read_bracket_arg(s: str, pos: int) -> tuple[str | None, int]:
    """Read an optional [...] argument starting at pos."""
    n = len(s)
    while pos < n and s[pos] in " \t":
        pos += 1
    if pos >= n or s[pos] != "[":
        return None, pos
    depth = 0
    start = pos + 1
    i = pos
    while i < n:
        ch = s[i]
        if ch == "\\" and i + 1 < n:
            i += 2
            continue
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                return s[start:i], i + 1
        i += 1
    return s[start:], n


def strip_tex_comments(text: str) -> str:
    out_lines: list[str] = []
    for line in text.splitlines():
        i = 0
        n = len(line)
        cut = n
        while i < n:
            if line[i] == "\\" and i + 1 < n:
                i += 2
                continue
            if line[i] == "%":
                cut = i
                break
            i += 1
        out_lines.append(line[:cut])
    return "\n".join(out_lines)


def strip_outer_braces(s: str) -> str:
    s = s.strip()
    while s.startswith("{") and s.endswith("}"):
        inner, end = read_brace_arg(s, 0)
        if end == len(s):
            s = inner.strip()
        else:
            break
    return s


# ---------------------------------------------------------------------------
# Batch KaTeX SSR via Node.js (zero client-side FOUC / failure)
# ---------------------------------------------------------------------------

class KatexBatchRenderer:
    def __init__(self, macros: dict[str, str]) -> None:
        self.macros = macros
        self.items: list[tuple[str, bool]] = []  # (tex, display_mode)

    def add(self, tex: str, display: bool = False) -> str:
        idx = len(self.items)
        self.items.append((tex.strip(), display))
        return f"@@KATEXSSR{idx}@@"

    def render_all(self, html_text: str) -> str:
        if not self.items:
            return html_text
        payload = json.dumps(
            {
                "katexPath": str(KATEX_JS),
                "macros": self.macros,
                "items": [{"tex": t, "display": d} for t, d in self.items],
            }
        )
        js_code = r"""
const fs = require('fs');
const data = JSON.parse(fs.readFileSync(0, 'utf8'));
const katex = require(data.katexPath);
const out = data.items.map((item, i) => {
  try {
    return katex.renderToString(item.tex, {
      displayMode: item.display,
      macros: Object.assign({}, data.macros),
      throwOnError: true,
      strict: false,
      trust: false
    });
  } catch (err) {
    throw new Error(`KaTeX error on item #${i} (${item.display ? 'display' : 'inline'}): ${item.tex}\n${err.message}`);
  }
});
process.stdout.write(JSON.stringify(out));
"""
        proc = subprocess.run(
            ["node", "-e", js_code],
            input=payload,
            text=True,
            capture_output=True,
            check=False,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"KaTeX SSR failed:\n{proc.stderr}")
        rendered_list: list[str] = json.loads(proc.stdout)
        result = html_text
        for idx, rendered in enumerate(rendered_list):
            result = result.replace(f"@@KATEXSSR{idx}@@", rendered)
        return result


# ---------------------------------------------------------------------------
# Parse .aux labels & .bbl bibliography
# ---------------------------------------------------------------------------

def parse_aux_labels(aux_path: Path) -> dict[str, tuple[str, str]]:
    """Return {label: (number_str, kind)} from main.aux."""
    labels: dict[str, tuple[str, str]] = {}
    if not aux_path.is_file():
        return labels
    raw = aux_path.read_text(encoding="utf-8", errors="ignore")
    pos = 0
    marker = r"\newlabel{"
    while True:
        idx = raw.find(marker, pos)
        if idx == -1:
            break
        key, p = read_brace_arg(raw, idx + len(r"\newlabel"))
        body, p = read_brace_arg(raw, p)
        pos = p
        if key.endswith("@cref") or not key:
            continue
        num, bp = read_brace_arg(body, 0)
        _page, bp = read_brace_arg(body, bp)
        _title, bp = read_brace_arg(body, bp)
        anchor, _ = read_brace_arg(body, bp)
        kind = anchor.split(".")[0] if anchor else ""
        labels[key] = (num.strip(), kind)
    return labels


def clean_bbl_text(tex: str) -> str:
    """Convert .bbl bibliography fragment to clean HTML."""
    s = tex.strip()
    s = re.sub(r"\{\\natexlab\{([^}]*)\}\}", r"\1", s)
    s = re.sub(r"\\natexlab\{([^}]*)\}", r"\1", s)
    s = s.replace(r"{\L}", "Ł").replace(r"\L ", "Ł")
    s = s.replace(r"{\l}", "ł")
    s = s.replace(r"\'{e}", "é").replace(r"\'e", "é").replace(r"\'{a}", "á").replace(r"\'{o}", "ó")
    s = s.replace(r'\"{o}', "ö").replace(r'\"{u}', "ü").replace(r'\"{a}', "ä")
    s = s.replace(r"\&", "&amp;")
    s = s.replace(r"\newblock", " ")
    s = s.replace("~", "\u00a0")
    s = re.sub(r"\\\s+", " ", s)
    s = s.replace("---", "—").replace("--", "–")

    # Convert \emph{...} and \url{...}
    out: list[str] = []
    i = 0
    n = len(s)
    while i < n:
        if s.startswith(r"\emph{", i):
            inner, i = read_brace_arg(s, i + 5)
            out.append(f"<em>{clean_bbl_text(inner)}</em>")
        elif s.startswith(r"\url{", i):
            url, i = read_brace_arg(s, i + 4)
            esc_u = html.escape(url.strip())
            out.append(f'<a class="tex-url" href="{esc_u}" target="_blank" rel="noopener">{esc_u}</a>')
        elif s[i] in "{}":
            i += 1
        else:
            ch = s[i]
            if ch == "<":
                out.append("&lt;")
            elif ch == ">":
                out.append("&gt;")
            elif ch == "&" and not s[i:].startswith("&amp;"):
                out.append("&amp;")
            else:
                out.append(ch)
            i += 1
    res = "".join(out)
    res = re.sub(r"\s+", " ", res).strip()
    return res


def parse_bbl(bbl_path: Path) -> dict[str, BibEntry]:
    entries: dict[str, BibEntry] = {}
    if not bbl_path.is_file():
        return entries
    raw = bbl_path.read_text(encoding="utf-8")
    parts = re.split(r"\\bibitem(?=\[|\{)", raw)
    for part in parts[1:]:
        opt, p = read_bracket_arg(part, 0)
        key, p = read_brace_arg(part, p)
        body = part[p:]
        body = re.sub(r"\\end\{thebibliography\}[\s\S]*$", "", body).strip()

        short_author = key
        year = ""
        if opt:
            opt_clean = re.sub(r"\{\\natexlab\{([^}]*)\}\}", r"\1", opt)
            opt_clean = re.sub(r"\\natexlab\{([^}]*)\}", r"\1", opt_clean)
            opt_clean = opt_clean.replace("~", " ").replace("{", "").replace("}", "")
            m = re.match(r"^(.*?)\((\d{4}[a-z]?)\)", opt_clean)
            if m:
                short_author = m.group(1).strip()
                year = m.group(2).strip()
            else:
                short_author = opt_clean.strip()

        html_body = clean_bbl_text(body)
        plain = html.unescape(re.sub(r"<[^>]+>", "", html_body))
        entries[key.strip()] = BibEntry(
            key=key.strip(),
            short_author=short_author,
            year=year,
            html_body=html_body,
            plain_preview=plain,
        )
    return entries


# ---------------------------------------------------------------------------
# PDF Figure renderer (vector PDF -> high-DPI trimmed PNG)
# ---------------------------------------------------------------------------

def convert_pdf_figure(pdf_path: Path, out_png: Path, narrow: bool = False) -> Path:
    out_png.parent.mkdir(parents=True, exist_ok=True)
    doc = pymupdf.open(pdf_path)
    page = doc[0]
    target_w = 1600 if narrow else 2400
    zoom = target_w / page.rect.width
    pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False)
    img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
    bg = Image.new("RGB", img.size, (255, 255, 255))
    diff = ImageChops.difference(img, bg)
    bbox = diff.getbbox()
    if bbox:
        pad = 14
        img = img.crop(
            (
                max(0, bbox[0] - pad),
                max(0, bbox[1] - pad),
                min(img.width, bbox[2] + pad),
                min(img.height, bbox[3] + pad),
            )
        )
    img.save(out_png, optimize=True)
    return out_png


# ---------------------------------------------------------------------------
# Main LaTeX -> HTML Converter
# ---------------------------------------------------------------------------

class LatexPaperConverter:
    def __init__(
        self,
        paper_dir: Path,
        out_asset_dir: Path,
        aux_labels: dict[str, tuple[str, str]],
        bib_entries: dict[str, BibEntry],
        pdf_url: str = "/loopbehind.pdf",
        macros_katex: dict[str, str] | None = None,
        text_macros: dict[str, str] | None = None,
    ) -> None:
        self.paper_dir = paper_dir
        self.out_asset_dir = out_asset_dir
        self.aux_labels = aux_labels
        self.bib = bib_entries
        self.pdf_url = pdf_url
        self.katex = KatexBatchRenderer(macros_katex or {})
        self.text_macros = text_macros or {}
        self.cited_keys: set[str] = set()
        self.generated_figures: list[Path] = []
        self.toc: list[PaperTocItem] = []
        self.sec_counter: int = 0
        self.subsec_counter: int = 0

    # ---- Cross-references (\cref, \Cref) ----
    def format_cref(self, keys_str: str, capitalize: bool) -> str:
        keys = [k.strip() for k in keys_str.split(",") if k.strip()]
        if not keys:
            return ""
        kind_map = {
            "sec": ("Section", "Sections"),
            "app": ("Appendix", "Appendices"),
            "fig": ("Figure", "Figures"),
            "tab": ("Table", "Tables"),
            "eq": ("Equation", "Equations"),
            "alg": ("Algorithm", "Algorithms"),
        }
        prefix = keys[0].split(":")[0] if ":" in keys[0] else ""
        sing, plur = kind_map.get(prefix, ("Section", "Sections"))
        word = plur if len(keys) > 1 else sing
        if not capitalize and prefix == "eq":
            word = "Eqs." if len(keys) > 1 else "Eq."

        links: list[str] = []
        for k in keys:
            num, _ = self.aux_labels.get(k, ("?", ""))
            disp = f"({num})" if prefix == "eq" else num
            if prefix == "app":
                links.append(
                    f'<a class="tex-xref" href="{html.escape(self.pdf_url)}" target="_blank" rel="noopener" title="See Appendix {html.escape(num)} in the full PDF">{html.escape(disp)}</a>'
                )
            else:
                anchor = self.label_to_id(k)
                links.append(f'<a class="tex-xref" href="#{html.escape(anchor)}">{html.escape(disp)}</a>')

        if len(links) == 1:
            return f"{word}\u00a0{links[0]}"
        if len(links) == 2:
            return f"{word}\u00a0{links[0]} and {links[1]}"
        return f"{word}\u00a0" + ", ".join(links[:-1]) + f", and {links[-1]}"

    @staticmethod
    def label_to_id(label: str) -> str:
        return re.sub(r"[^a-zA-Z0-9_-]+", "-", label).strip("-")

    # ---- Citations (\citep, \citet) ----
    def format_cite(self, keys_str: str, textual: bool) -> str:
        keys = [k.strip() for k in keys_str.split(",") if k.strip()]
        rendered_parts: list[str] = []
        for k in keys:
            self.cited_keys.add(k)
            entry = self.bib.get(k)
            if not entry:
                rendered_parts.append(html.escape(k))
                continue
            ref_id = f"ref-{self.label_to_id(k)}"
            if textual:
                rendered_parts.append(
                    f'<a class="tex-cite" href="#{ref_id}" data-ref="{html.escape(k)}">{html.escape(entry.short_author)} ({html.escape(entry.year)})</a>'
                )
            else:
                rendered_parts.append(
                    f'<a class="tex-cite" href="#{ref_id}" data-ref="{html.escape(k)}">{html.escape(entry.short_author)}, {html.escape(entry.year)}</a>'
                )
        if textual:
            return ", ".join(rendered_parts)
        return "(" + "; ".join(rendered_parts) + ")"

    # ---- Inline TeX -> HTML ----
    def convert_inline(self, tex: str) -> str:
        s = tex
        # Normalize typography outside math first via character-level scanner
        out: list[str] = []
        i = 0
        n = len(s)

        while i < n:
            ch = s[i]

            # 1. Inline math $...$
            if ch == "$":
                # Check for $$...$$ just in case
                if i + 1 < n and s[i + 1] == "$":
                    end = s.find("$$", i + 2)
                    if end != -1:
                        out.append(self.katex.add(s[i + 2 : end], display=True))
                        i = end + 2
                        continue
                j = i + 1
                while j < n:
                    if s[j] == "\\" and j + 1 < n:
                        j += 2
                        continue
                    if s[j] == "$":
                        break
                    j += 1
                math_src = s[i + 1 : j]
                tok = self.katex.add(math_src, display=False)
                # Glue trailing $\times$ to immediately preceding number (e.g. 2.56$\times$)
                if math_src.strip() == r"\times" and out:
                    # Collect trailing number characters from out
                    num_chars: list[str] = []
                    while out and len(out[-1]) == 1 and out[-1] in "0123456789.–-":
                        num_chars.append(out.pop())
                    if num_chars:
                        num_str = "".join(reversed(num_chars))
                        out.append(f'<span class="tex-nowrap">{num_str}{tok}</span>')
                        i = j + 1
                        continue
                out.append(tok)
                i = j + 1
                continue

            # 2. Backslash commands
            if ch == "\\":
                # Single-char escapes
                if i + 1 < n and s[i + 1] in "%&_#${}":
                    esc_map = {
                        "%": "%",
                        "&": "&amp;",
                        "_": "_",
                        "#": "#",
                        "$": "$",
                        "{": "{",
                        "}": "}",
                    }
                    out.append(esc_map[s[i + 1]])
                    i += 2
                    continue
                if i + 1 < n and s[i + 1] in ",;:! ":
                    sp_map = {
                        ",": "\u2009",
                        ";": "\u2005",
                        ":": "\u2005",
                        "!": "",
                        " ": " ",
                    }
                    out.append(sp_map[s[i + 1]])
                    i += 2
                    continue

                m_cmd = re.match(r"\\([a-zA-Z]+)\*?", s[i:])
                if m_cmd:
                    cmd = m_cmd.group(1)
                    after = i + len(m_cmd.group(0))

                    # Consume trailing empty group {} for zero-arg text macros
                    if cmd in self.text_macros:
                        if after + 1 < n and s[after : after + 2] == "{}":
                            after += 2
                        out.append(self.text_macros[cmd])
                        i = after
                        continue

                    # Math-producing macros used in text mode (\Lk{1}, \Lkv{1}{2}, \KV, \Pb, \Rb, \Cb)
                    if cmd == "Lk":
                        a1, after = read_brace_arg(s, after)
                        out.append(self.katex.add(rf"\Lk{{{a1}}}", display=False))
                        i = after
                        continue
                    if cmd == "Lkv":
                        a1, after = read_brace_arg(s, after)
                        a2, after = read_brace_arg(s, after)
                        out.append(self.katex.add(rf"\Lkv{{{a1}}}{{{a2}}}", display=False))
                        i = after
                        continue
                    if cmd in ("KV", "Pb", "Rb", "Cb"):
                        if after + 1 < n and s[after : after + 2] == "{}":
                            after += 2
                        out.append(self.katex.add(rf"\{cmd}", display=False))
                        i = after
                        continue

                    if cmd == "textbf":
                        arg, after = read_brace_arg(s, after)
                        out.append(f"<strong>{self.convert_inline(arg)}</strong>")
                        i = after
                        continue
                    if cmd in ("emph", "textit"):
                        arg, after = read_brace_arg(s, after)
                        out.append(f"<em>{self.convert_inline(arg)}</em>")
                        i = after
                        continue
                    if cmd == "texttt":
                        arg, after = read_brace_arg(s, after)
                        out.append(f'<code class="tex-url">{html.escape(arg)}</code>')
                        i = after
                        continue
                    if cmd == "textsuperscript":
                        arg, after = read_brace_arg(s, after)
                        out.append(f"<sup>{self.convert_inline(arg)}</sup>")
                        i = after
                        continue
                    if cmd == "url":
                        arg, after = read_brace_arg(s, after)
                        esc_u = html.escape(arg.strip())
                        out.append(f'<a class="tex-url" href="{esc_u}" target="_blank" rel="noopener">{esc_u}</a>')
                        i = after
                        continue
                    if cmd == "citep":
                        _opt, after = read_bracket_arg(s, after)
                        arg, after = read_brace_arg(s, after)
                        out.append(self.format_cite(arg, textual=False))
                        i = after
                        continue
                    if cmd == "citet":
                        _opt, after = read_bracket_arg(s, after)
                        arg, after = read_brace_arg(s, after)
                        out.append(self.format_cite(arg, textual=True))
                        i = after
                        continue
                    if cmd in ("cref", "Cref"):
                        arg, after = read_brace_arg(s, after)
                        out.append(self.format_cref(arg, capitalize=(cmd == "Cref")))
                        i = after
                        continue
                    if cmd == "ding":
                        arg, after = read_brace_arg(s, after)
                        ding_map = {"172": "①", "173": "②", "174": "③", "51": "✓", "55": "✗"}
                        out.append(ding_map.get(arg.strip(), ""))
                        i = after
                        continue
                    if cmd == "cmark":
                        out.append('<span class="tex-cmark" aria-label="Yes">✓</span>')
                        i = after
                        continue
                    if cmd == "xmark":
                        out.append('<span class="tex-xmark" aria-label="No">✗</span>')
                        i = after
                        continue
                    if cmd == "gain":
                        arg, after = read_brace_arg(s, after)
                        out.append(f'<span class="tex-gain">{self.convert_inline(arg)}</span>')
                        i = after
                        continue
                    if cmd == "loss":
                        arg, after = read_brace_arg(s, after)
                        out.append(f'<span class="tex-loss">{self.convert_inline(arg)}</span>')
                        i = after
                        continue
                    if cmd == "textcolor":
                        color_arg, after = read_brace_arg(s, after)
                        txt_arg, after = read_brace_arg(s, after)
                        cls = "tex-arrow-green" if "green" in color_arg else ""
                        inner = self.convert_inline(txt_arg)
                        out.append(f'<span class="{cls}">{inner}</span>' if cls else inner)
                        i = after
                        continue
                    if cmd in ("vspace", "hspace", "setlength", "renewcommand"):
                        # Consume arguments and skip layout tweaks
                        if cmd in ("setlength", "renewcommand"):
                            _, after = read_brace_arg(s, after)
                            _, after = read_brace_arg(s, after)
                        else:
                            _, after = read_brace_arg(s, after)
                        i = after
                        continue
                    if cmd in ("footnotesize", "small", "normalsize", "scriptsize", "centering", "par", "smallskip", "medskip", "noindent"):
                        i = after
                        continue

                    # Unknown command: skip command name safely
                    i = after
                    continue

            # 3. Non-breaking space & dashes & quotes
            if ch == "~":
                out.append("\u00a0")
                i += 1
                continue
            if s.startswith("---", i):
                out.append("—")
                i += 3
                continue
            if s.startswith("--", i):
                out.append("–")
                i += 2
                continue
            if s.startswith("``", i):
                out.append("“")
                i += 2
                continue
            if s.startswith("''", i):
                out.append("”")
                i += 2
                continue
            if ch in "{}":
                i += 1
                continue

            # Normal character
            if ch == "<":
                out.append("&lt;")
            elif ch == ">":
                out.append("&gt;")
            elif ch == "&":
                out.append("&amp;")
            else:
                out.append(ch)
            i += 1

        res = "".join(out)
        res = re.sub(r"[ \t]+", " ", res)
        return res

    # ---- Equations (\begin{equation}, \begin{align}) ----
    def convert_equation_env(self, env_name: str, inner: str) -> str:
        label_match = re.search(r"\\label\{([^}]+)\}", inner)
        label = label_match.group(1).strip() if label_match else ""
        cleaned = re.sub(r"\\label\{[^}]+\}", "", inner)
        cleaned = re.sub(r"\\nonumber\b", "", cleaned).strip()

        if env_name == "align":
            tex = f"\\begin{{aligned}}\n{cleaned}\n\\end{{aligned}}"
        else:
            tex = cleaned

        eq_num_html = ""
        id_attr = ""
        if label:
            num, _ = self.aux_labels.get(label, ("", ""))
            id_attr = f' id="{html.escape(self.label_to_id(label))}"'
            if num:
                eq_num_html = f'<div class="tex-eq-num">({html.escape(num)})</div>'

        math_tok = self.katex.add(tex, display=True)
        return (
            f'<div class="tex-eq-row"{id_attr}>'
            f'<div class="tex-eq-math">{math_tok}</div>'
            f"{eq_num_html}"
            f"</div>"
        )

    # ---- Figures (\begin{figure}, \begin{figure*}) ----
    def convert_figure_env(self, env_name: str, inner: str) -> tuple[str, str]:
        label_m = re.search(r"\\label\{([^}]+)\}", inner)
        label = label_m.group(1).strip() if label_m else ""
        num, _ = self.aux_labels.get(label, ("", ""))

        inc_m = re.search(r"\\includegraphics(?:\[[^\]]*\])?\{([^}]+)\}", inner)
        img_rel = inc_m.group(1).strip() if inc_m else ""
        narrow = env_name == "figure"

        img_html = ""
        if img_rel:
            src_pdf = self.paper_dir / img_rel
            stem = src_pdf.stem
            out_png = self.out_asset_dir / f"{stem}.png"
            if src_pdf.is_file():
                convert_pdf_figure(src_pdf, out_png, narrow=narrow)
                self.generated_figures.append(out_png)
            img_html = f'<img src="{html.escape(stem)}.png" alt="Figure {html.escape(num)}">'

        cap_idx = inner.find(r"\caption")
        cap_html = ""
        if cap_idx != -1:
            cap_raw, _ = read_brace_arg(inner, cap_idx + len(r"\caption"))
            cap_inner = self.convert_inline(cap_raw)
            cap_html = f'<figcaption><span class="tex-caption-label">Figure {html.escape(num)}.</span> {cap_inner}</figcaption>'

        cls = "tex-figure tex-figure-narrow" if narrow else "tex-figure tex-wide-breakout"
        id_attr = f' id="{html.escape(self.label_to_id(label))}"' if label else ""
        block = (
            f'<figure class="{cls}"{id_attr}>'
            f'<div class="tex-figure-img-wrap">{img_html}</div>'
            f"{cap_html}"
            f"</figure>"
        )
        return label, block

    # ---- Algorithm (\begin{algorithm*}) ----
    def convert_algorithm_env(self, inner: str) -> tuple[str, str]:
        label_m = re.search(r"\\label\{([^}]+)\}", inner)
        label = label_m.group(1).strip() if label_m else ""
        num, _ = self.aux_labels.get(label, ("1", ""))

        cap_idx = inner.find(r"\caption")
        cap_html = ""
        if cap_idx != -1:
            cap_raw, _ = read_brace_arg(inner, cap_idx + len(r"\caption"))
            cap_html = self.convert_inline(cap_raw)

        alg_m = re.search(r"\\begin\{algorithmic\}(?:\[[^\]]*\])?([\s\S]*?)\\end\{algorithmic\}", inner)
        body_lines_html: list[str] = []
        if alg_m:
            raw_lines = [ln.strip() for ln in alg_m.group(1).splitlines() if ln.strip()]
            line_no = 0
            indent = 0
            for ln in raw_lines:
                comment_html = ""
                c_idx = ln.find(r"\COMMENT")
                if c_idx != -1:
                    c_arg, _ = read_brace_arg(ln, c_idx + len(r"\COMMENT"))
                    comment_html = f'<span class="tex-algo-comment">▷ {self.convert_inline(c_arg)}</span>'
                    ln = ln[:c_idx].strip()

                if ln.startswith(r"\REQUIRE"):
                    req_txt = ln[len(r"\REQUIRE") :].strip()
                    body_lines_html.append(
                        f'<div class="tex-algo-req"><strong>Require:</strong> &nbsp;{self.convert_inline(req_txt)}</div>'
                    )
                    continue

                if ln.startswith(r"\STATE"):
                    line_no += 1
                    stmt = self.convert_inline(ln[len(r"\STATE") :].strip())
                    ind_cls = f" tex-algo-indent-{indent}" if indent > 0 else ""
                    body_lines_html.append(
                        f'<div class="tex-algo-line{ind_cls}">'
                        f'<span class="tex-algo-num">{line_no}:</span>'
                        f'<span class="tex-algo-stmt"><span class="tex-algo-code">{stmt}</span>{comment_html}</span>'
                        f"</div>"
                    )
                elif ln.startswith(r"\FOR"):
                    line_no += 1
                    cond, _ = read_brace_arg(ln, len(r"\FOR"))
                    ind_cls = f" tex-algo-indent-{indent}" if indent > 0 else ""
                    body_lines_html.append(
                        f'<div class="tex-algo-line{ind_cls}">'
                        f'<span class="tex-algo-num">{line_no}:</span>'
                        f'<span class="tex-algo-stmt"><span class="tex-algo-code"><strong>for</strong> {self.convert_inline(cond)} <strong>do</strong></span>{comment_html}</span>'
                        f"</div>"
                    )
                    indent += 1
                elif ln.startswith(r"\ENDFOR"):
                    indent = max(0, indent - 1)
                    line_no += 1
                    ind_cls = f" tex-algo-indent-{indent}" if indent > 0 else ""
                    body_lines_html.append(
                        f'<div class="tex-algo-line{ind_cls}">'
                        f'<span class="tex-algo-num">{line_no}:</span>'
                        f'<span class="tex-algo-stmt"><span class="tex-algo-code"><strong>end for</strong></span>{comment_html}</span>'
                        f"</div>"
                    )

        id_attr = f' id="{html.escape(self.label_to_id(label))}"' if label else ""
        block = (
            f'<div class="tex-algo"{id_attr}>'
            f'<div class="tex-algo-header"><strong>Algorithm {html.escape(num)}</strong> &nbsp;{cap_html}</div>'
            f'<div class="tex-algo-body">' + "\n".join(body_lines_html) + "</div>"
            f"</div>"
        )
        return label, block

    # ---- Tables (\begin{table}, \begin{table*} + \begin{tabular}) ----
    def convert_table_env(self, env_name: str, inner: str) -> tuple[str, str]:
        label_m = re.search(r"\\label\{([^}]+)\}", inner)
        label = label_m.group(1).strip() if label_m else ""
        num, _ = self.aux_labels.get(label, ("", ""))

        cap_idx = inner.find(r"\caption")
        cap_html = ""
        if cap_idx != -1:
            cap_raw, _ = read_brace_arg(inner, cap_idx + len(r"\caption"))
            cap_html = f'<div class="tex-table-caption"><span class="tex-caption-label">Table {html.escape(num)}.</span> {self.convert_inline(cap_raw)}</div>'

        # Extract optional \parbox footnote after \end{tabular}
        footnote_html = ""
        end_tab = inner.find(r"\end{tabular}")
        if end_tab != -1:
            after_tab = inner[end_tab + len(r"\end{tabular}") :]
            pb_idx = after_tab.find(r"\parbox")
            if pb_idx != -1:
                _, p = read_brace_arg(after_tab, pb_idx + len(r"\parbox"))
                fn_raw, _ = read_brace_arg(after_tab, p)
                footnote_html = f'<div class="tex-table-footnote">{self.convert_inline(fn_raw)}</div>'

        tab_start = inner.find(r"\begin{tabular}")
        table_html = ""
        col_count = 0
        if tab_start != -1 and end_tab != -1:
            col_spec_raw, body_start = read_brace_arg(inner, tab_start + len(r"\begin{tabular}"))
            # Strip @{...} column-separator directives before reading l/c/r alignments
            col_spec_clean = re.sub(r"@\{[^}]*\}", "", col_spec_raw)
            col_aligns = [c for c in col_spec_clean if c in "lcr"]
            col_count = len(col_aligns)
            tab_body = inner[body_start:end_tab]
            table_html = self._parse_tabular(tab_body, col_aligns)

        narrow = env_name == "table" and col_count <= 5
        wide = env_name == "table*" and col_count >= 10
        if narrow:
            wrap_cls = "tex-table-float tex-table-narrow"
        elif wide:
            wrap_cls = "tex-table-float tex-wide-breakout"
        else:
            wrap_cls = "tex-table-float"
        id_attr = f' id="{html.escape(self.label_to_id(label))}"' if label else ""
        block = (
            f'<div class="{wrap_cls}"{id_attr}>'
            f"{cap_html}"
            f'<div class="tex-table-scroll">{table_html}</div>'
            f"{footnote_html}"
            f"</div>"
        )
        return label, block

    def _split_tabular_cells(self, row: str) -> list[str]:
        cells: list[str] = []
        buf: list[str] = []
        depth = 0
        i = 0
        n = len(row)
        while i < n:
            ch = row[i]
            if ch == "\\" and i + 1 < n:
                buf.append(ch)
                buf.append(row[i + 1])
                i += 2
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth = max(0, depth - 1)
            if ch == "&" and depth == 0:
                cells.append("".join(buf).strip())
                buf = []
            else:
                buf.append(ch)
            i += 1
        cells.append("".join(buf).strip())
        return cells

    def _parse_tabular(self, raw: str, col_aligns: list[str]) -> str:
        # Remove \toprule and \bottomrule
        s = re.sub(r"\\toprule\b", "", raw)
        s = re.sub(r"\\bottomrule\b", "", s).strip()

        # Split rows by \\ (outside braces)
        raw_rows: list[str] = []
        buf: list[str] = []
        depth = 0
        i = 0
        n = len(s)
        while i < n:
            if s[i] == "{":
                depth += 1
                buf.append(s[i])
                i += 1
            elif s[i] == "}":
                depth = max(0, depth - 1)
                buf.append(s[i])
                i += 1
            elif s.startswith(r"\\", i) and depth == 0:
                raw_rows.append("".join(buf).strip())
                buf = []
                i += 2
            else:
                buf.append(s[i])
                i += 1
        tail = "".join(buf).strip()
        if tail:
            raw_rows.append(tail)

        # Parse each row's directives (\midrule, \cmidrule, \addlinespace, \oursrow, \tabgroup)
        parsed_rows: list[dict[str, Any]] = []
        pending_midrule_before = False
        pending_addlinespace = False

        for r in raw_rows:
            r = r.strip()
            if not r:
                continue

            # Extract leading \cmidrule directives applying to PREVIOUS row
            cmids: list[tuple[int, int]] = []
            while True:
                r = r.lstrip()
                m_cmid = re.match(r"\\cmidrule(?:\([^)]*\))?\{(\d+)-(\d+)\}", r)
                if m_cmid:
                    cmids.append((int(m_cmid.group(1)), int(m_cmid.group(2))))
                    r = r[m_cmid.end() :]
                    continue
                if r.startswith(r"\midrule"):
                    pending_midrule_before = True
                    r = r[len(r"\midrule") :]
                    continue
                m_add = re.match(r"\\addlinespace(?:\[[^\]]*\])?", r)
                if m_add:
                    pending_addlinespace = True
                    r = r[m_add.end() :]
                    continue
                break

            if cmids and parsed_rows:
                parsed_rows[-1]["cmids"].extend(cmids)

            r = r.strip()
            if not r:
                continue

            row_classes: list[str] = []
            if pending_midrule_before:
                row_classes.append("tr-midrule-before")
                pending_midrule_before = False
            if pending_addlinespace:
                row_classes.append("tr-addlinespace")
                pending_addlinespace = False

            if r.startswith(r"\oursrow"):
                row_classes.append("ours-row")
                r = r[len(r"\oursrow") :].strip()

            if r.startswith(r"\tabgroup"):
                c1, p = read_brace_arg(r, len(r"\tabgroup"))
                c2, _ = read_brace_arg(r, p)
                row_classes.append("tabgroup-row")
                parsed_rows.append(
                    {
                        "classes": row_classes,
                        "cells": [{"colspan": int(c1), "align": "l", "html": self.convert_inline(c2)}],
                        "cmids": [],
                        "is_group": True,
                    }
                )
                continue

            raw_cells = self._split_tabular_cells(r)
            cells_out: list[dict[str, Any]] = []
            col_idx = 0
            for cell in raw_cells:
                if cell.startswith(r"\multicolumn"):
                    span_s, p = read_brace_arg(cell, len(r"\multicolumn"))
                    al_s, p = read_brace_arg(cell, p)
                    content_s, _ = read_brace_arg(cell, p)
                    span = int(span_s)
                    al_ch = next((c for c in al_s if c in "lcr"), "l")
                    cells_out.append(
                        {
                            "colspan": span,
                            "start_col": col_idx + 1,
                            "end_col": col_idx + span,
                            "align": al_ch,
                            "html": self.convert_inline(content_s),
                        }
                    )
                    col_idx += span
                else:
                    al_ch = col_aligns[col_idx] if col_idx < len(col_aligns) else "l"
                    cells_out.append(
                        {
                            "colspan": 1,
                            "start_col": col_idx + 1,
                            "end_col": col_idx + 1,
                            "align": al_ch,
                            "html": self.convert_inline(cell),
                        }
                    )
                    col_idx += 1

            parsed_rows.append(
                {
                    "classes": row_classes,
                    "cells": cells_out,
                    "cmids": [],
                    "is_group": len(cells_out) == 1 and cells_out[0]["colspan"] >= len(col_aligns),
                }
            )

        # Split header rows (up to first tr-midrule-before) vs body rows
        header_end = 1
        for idx, row in enumerate(parsed_rows):
            if idx > 0 and "tr-midrule-before" in row["classes"]:
                header_end = idx
                row["classes"].remove("tr-midrule-before")
                break
        if header_end > 0 and parsed_rows:
            parsed_rows[header_end - 1]["classes"].append("tr-midrule-after")

        dense = len(col_aligns) >= 10
        wrap_txt = len(col_aligns) <= 4
        cls_list = ["tex-booktabs"]
        if dense:
            cls_list.append("tex-table-dense")
        if wrap_txt:
            cls_list.append("tex-table-wrap-text")

        def render_tr(row: dict[str, Any], is_header: bool) -> str:
            tag = "th" if is_header else "td"
            rcls = list(row["classes"])
            if row.get("is_group") and "tabgroup-row" not in rcls:
                rcls.append("tabgroup-row")
            cls_attr = f' class="{" ".join(rcls)}"' if rcls else ""
            cmid_ranges = row.get("cmids", [])
            tds: list[str] = []
            for cell in row["cells"]:
                c_cls = [f'col-{cell["align"]}']
                if is_header and cmid_ranges:
                    sc, ec = cell.get("start_col", 0), cell.get("end_col", 0)
                    if any(sc == a and ec == b for a, b in cmid_ranges):
                        c_cls.append("cmid-cell")
                span_attr = f' colspan="{cell["colspan"]}"' if cell["colspan"] > 1 else ""
                tds.append(f'<{tag} class="{" ".join(c_cls)}"{span_attr}>{cell["html"]}</{tag}>')
            return f"<tr{cls_attr}>" + "".join(tds) + "</tr>"

        thead_rows = [render_tr(r, is_header=True) for r in parsed_rows[:header_end]]
        tbody_rows = [render_tr(r, is_header=False) for r in parsed_rows[header_end:]]
        return (
            f'<table class="{" ".join(cls_list)}">'
            f"<thead>{''.join(thead_rows)}</thead>"
            f"<tbody>{''.join(tbody_rows)}</tbody>"
            f"</table>"
        )

    # ---- Parse section body into HTML blocks + extracted floats ----
    def convert_section_tex(
        self,
        raw_tex: str,
        floats_store: dict[str, str],
        floating_labels: set[str],
    ) -> str:
        text = strip_tex_comments(raw_tex)

        # Remove layout-only preamble tweaks inside sections
        text = re.sub(r"\\renewcommand\{\\(?:dbltopfraction|textfraction|arraystretch)\}\{[^}]*\}", "", text)
        text = re.sub(r"\\setlength\{\\(?:dbltextfloatsep|dblfloatsep|tabcolsep)\}\{[^}]*\}", "", text)

        # Extract environments: figure*, figure, table*, table, algorithm*, align, equation
        env_tokens: list[str] = []
        env_re = re.compile(
            r"\\begin\{(figure\*?|table\*?|algorithm\*?|align|equation)\}(?:\[[^\]]*\])?([\s\S]*?)\\end\{\1\}"
        )

        def repl_env(m: re.Match[str]) -> str:
            env = m.group(1)
            inner = m.group(2)
            if env in ("align", "equation"):
                rendered = self.convert_equation_env(env, inner)
                tok = f"\n\n@@TEXBLOCK{len(env_tokens)}@@\n\n"
                env_tokens.append(rendered)
                return tok
            if env in ("figure", "figure*"):
                lbl, rendered = self.convert_figure_env(env, inner)
            elif env in ("table", "table*"):
                lbl, rendered = self.convert_table_env(env, inner)
            elif env in ("algorithm", "algorithm*"):
                lbl, rendered = self.convert_algorithm_env(inner)
            else:
                return ""

            if lbl and lbl in floating_labels:
                floats_store[lbl] = rendered
                return "\n\n"
            tok = f"\n\n@@TEXBLOCK{len(env_tokens)}@@\n\n"
            env_tokens.append(rendered)
            return tok

        text = env_re.sub(repl_env, text)

        # Replace \section{...}, \subsection{...}, \section*{...} with block tokens
        sec_re = re.compile(r"\\(section|subsection)(\*)?\{([^}]+)\}(?:\s*\\label\{([^}]+)\})?")

        def repl_sec(m: re.Match[str]) -> str:
            kind = m.group(1)
            starred = bool(m.group(2))
            title_raw = m.group(3).strip()
            label = (m.group(4) or "").strip()
            title_html = self.convert_inline(title_raw)
            if starred:
                sec_id = re.sub(r"[^a-z0-9]+", "-", title_raw.lower()).strip("-")
                num_html = ""
                toc_label = title_raw
            else:
                if kind == "section":
                    self.sec_counter += 1
                    self.subsec_counter = 0
                    fallback_num = str(self.sec_counter)
                else:
                    self.subsec_counter += 1
                    fallback_num = f"{self.sec_counter}.{self.subsec_counter}"
                aux_num = self.aux_labels.get(label, ("", ""))[0] if label else ""
                num = aux_num or fallback_num
                sec_id = self.label_to_id(label) if label else re.sub(r"[^a-z0-9]+", "-", title_raw.lower()).strip("-")
                num_html = f'<span class="sec-num">{html.escape(num)}.</span>' if num else ""
                toc_label = f"{num}. {title_raw}" if num else title_raw

            tag = "h2" if kind == "section" else "h3"
            cls = "tex-section" if kind == "section" else "tex-subsection"
            self.toc.append(PaperTocItem(level=2 if kind == "section" else 3, ident=sec_id, label=toc_label))
            rendered = (
                f'<{tag} class="{cls}" id="{html.escape(sec_id)}">'
                f"{num_html}{title_html}"
                f'<a class="heading-anchor" href="#{html.escape(sec_id)}" aria-label="Link to section">#</a>'
                f"</{tag}>"
            )
            tok = f"\n\n@@TEXBLOCK{len(env_tokens)}@@\n\n"
            env_tokens.append(rendered)
            return tok

        text = sec_re.sub(repl_sec, text)

        # Split remaining text into paragraphs
        blocks: list[str] = []
        for para in re.split(r"\n\s*\n", text):
            chunk = para.strip()
            if not chunk:
                continue
            m_blk = re.fullmatch(r"@@TEXBLOCK(\d+)@@", chunk)
            if m_blk:
                blocks.append(env_tokens[int(m_blk.group(1))])
                continue
            # Also handle paragraphs that contain an inline @@TEXBLOCKk@@ (e.g. equation inside paragraph)
            parts = re.split(r"(@@TEXBLOCK\d+@@)", chunk)
            for p_part in parts:
                p_part = p_part.strip()
                if not p_part:
                    continue
                m_sub = re.fullmatch(r"@@TEXBLOCK(\d+)@@", p_part)
                if m_sub:
                    blocks.append(env_tokens[int(m_sub.group(1))])
                else:
                    # Skip standalone \vspace{...}
                    if re.fullmatch(r"\\vspace\{[^}]*\}", p_part):
                        continue
                    inline_html = self.convert_inline(p_part)
                    if inline_html.strip():
                        blocks.append(f"<p>{inline_html}</p>")

        return "\n".join(blocks)


def compile_latex_paper(
    paper_dir: Path,
    out_asset_dir: Path,
    pdf_url: str = "/loopbehind.pdf",
    code_url: str = "https://github.com/XijieGo/LoopBehind",
    float_placements: dict[str, str] | None = None,
) -> CompiledPaper:
    """Compile a LaTeX paper (main.tex + sections/ + build/main.aux + build/main.bbl)."""
    main_tex = strip_tex_comments((paper_dir / "main.tex").read_text(encoding="utf-8"))
    aux_labels = parse_aux_labels(paper_dir / "build" / "main.aux")
    bib_entries = parse_bbl(paper_dir / "build" / "main.bbl")

    # KaTeX math macros (matches sections/macros.tex)
    katex_macros = {
        r"\Lk": r"\mathrm{L}_{#1}",
        r"\Lkv": r"\mathrm{L}_{#1}[\mathrm{KV}_{#2}]",
        r"\Pb": r"\mathcal{P}",
        r"\Rb": r"\mathcal{R}",
        r"\Cb": r"\mathcal{C}",
        r"\KV": r"\mathrm{KV}",
    }
    text_macros = {
        "wb": "LB",
    }

    conv = LatexPaperConverter(
        paper_dir=paper_dir,
        out_asset_dir=out_asset_dir,
        aux_labels=aux_labels,
        bib_entries=bib_entries,
        pdf_url=pdf_url,
        macros_katex=katex_macros,
        text_macros=text_macros,
    )

    # Extract title (match \icmltitle{ explicitly so \icmltitlerunning is skipped)
    t_idx = main_tex.find(r"\icmltitle{")
    raw_title, _ = read_brace_arg(main_tex, t_idx + len(r"\icmltitle")) if t_idx != -1 else ("", 0)
    title_clean = re.sub(r"\s*\\\\\s*", " ", raw_title).strip()
    title_html = re.sub(r"\s*\\\\\s*", "<br>", html.escape(raw_title.strip()))

    # Extract authors, affiliation, correspondence, notice
    authors_list: list[tuple[str, str]] = []
    for m_a in re.finditer(r"\\icmlauthor\{([^}]+)\}\{([^}]+)\}", main_tex):
        authors_list.append((m_a.group(1).strip(), m_a.group(2).strip()))

    author_spans: list[str] = []
    for name, syms in authors_list:
        sym_parts = [s.strip() for s in syms.split(",")]
        sups: list[str] = []
        if "intern" in sym_parts:
            sups.append("*")
        if "corr" in sym_parts:
            sups.append("†")
        sup_html = f"<sup>{''.join(sups)}</sup>" if sups else ""
        author_spans.append(f"<span>{html.escape(name)}{sup_html}</span>")
    authors_html = ", ".join(author_spans)

    affil_m = re.search(r"\\icmlaffiliation\{[^}]+\}\{([^}]+)\}", main_tex)
    affiliation_html = html.escape(affil_m.group(1).strip()) if affil_m else ""

    corr_m = re.search(r"\\icmlcorrespondingauthor\{([^}]+)\}\{([^}]+)\}", main_tex)
    notice_idx = main_tex.find(r"\printAffiliationsAndNotice")
    notice_txt = ""
    if notice_idx != -1:
        n_raw, _ = read_brace_arg(main_tex, notice_idx + len(r"\printAffiliationsAndNotice"))
        notice_txt = conv.convert_inline(n_raw).strip()

    notes_parts: list[str] = []
    if notice_txt:
        notes_parts.append(notice_txt)
    if corr_m:
        cname, cemail = corr_m.group(1).strip(), corr_m.group(2).strip()
        notes_parts.append(
            f'<sup>†</sup>Correspondence to: {html.escape(cname)} &lt;<a href="mailto:{html.escape(cemail)}">{html.escape(cemail)}</a>&gt;.'
        )
    notes_html = " &nbsp;&middot;&nbsp; ".join(notes_parts)

    # Abstract
    abs_raw = strip_tex_comments((paper_dir / "sections" / "0-abstract.tex").read_text(encoding="utf-8")).strip()
    conv.toc.append(PaperTocItem(level=2, ident="abstract", label="Abstract"))
    abstract_html = conv.convert_inline(abs_raw)

    # Ordered main body sections from main.tex (before \bibliography / \appendix)
    body_part = main_tex.split(r"\begin{abstract}")[1]
    body_part = body_part.split(r"\bibliography")[0]

    # Collect section inputs + Impact Statement
    section_inputs = re.findall(r"\\input\{(sections/[1-9][^}]+)\}", body_part)

    # Floats that LaTeX places out-of-order due to two-column [t] floats;
    # relocate them cleanly to their natural single-column anchor.
    relocate = float_placements or {
        "fig:domains": "after_heading:sec-macro",
        "fig:method": "after_para_contains:Native decoding spends an extra loop as depth",
        "alg:wb": "before_para_contains:details this schedule.",
        "tab:main": "after_heading:sec-wb-results",
        "fig:speed": "after_para_contains:To verify this analysis empirically",
        "tab:huginn": "after_para_contains:We compare",
        "tab:related": "before_para_contains:Parallel loops and efficient decoding.",
    }
    floats_store: dict[str, str] = {}
    floating_labels = set(relocate.keys())

    body_chunks: list[str] = []
    for sec_rel in section_inputs:
        sec_path = paper_dir / (sec_rel if sec_rel.endswith(".tex") else f"{sec_rel}.tex")
        sec_raw = sec_path.read_text(encoding="utf-8")
        body_chunks.append(conv.convert_section_tex(sec_raw, floats_store, floating_labels))

    # Impact Statement from main.tex
    imp_m = re.search(r"(\\section\*\{Impact Statement\}[\s\S]*)$", body_part)
    if imp_m:
        body_chunks.append(conv.convert_section_tex(imp_m.group(1), floats_store, floating_labels))

    body_html = "\n".join(body_chunks)

    # Insert relocated floats at their clean single-column positions
    for lbl, rule in relocate.items():
        if lbl not in floats_store:
            continue
        f_html = floats_store[lbl]
        if rule.startswith("after_heading:"):
            target_id = rule.split(":", 1)[1]
            pat = re.compile(rf'(<h[23][^>]*id="{re.escape(target_id)}"[^>]*>[\s\S]*?</h[23]>)')
            if pat.search(body_html):
                body_html = pat.sub(lambda m: m.group(1) + "\n" + f_html, body_html, count=1)
                continue
        elif rule.startswith("after_para_contains:"):
            needle = rule.split(":", 1)[1]
            pat = re.compile(rf"(<p>[^<]*(?:<(?!/?p>)[^<]*)*{re.escape(needle)}[\s\S]*?</p>)")
            if pat.search(body_html):
                body_html = pat.sub(lambda m: m.group(1) + "\n" + f_html, body_html, count=1)
                continue
        elif rule.startswith("before_para_contains:"):
            needle = rule.split(":", 1)[1]
            pat = re.compile(rf"(<p>(?:(?!</p>)[\s\S])*{re.escape(needle)}(?:(?!</p>)[\s\S])*</p>)")
            if pat.search(body_html):
                body_html = pat.sub(lambda m: f_html + "\n" + m.group(1), body_html, count=1)
                continue
        body_html += "\n" + f_html

    # Build References section (Husky'Log compact 2-column numbered grid)
    conv.toc.append(PaperTocItem(level=2, ident="references", label="References"))
    ref_items: list[str] = []
    ref_tooltips: dict[str, str] = {}
    ref_idx = 0
    for key, entry in bib_entries.items():
        if key not in conv.cited_keys:
            continue
        ref_idx += 1
        ref_id = f"ref-{conv.label_to_id(key)}"
        ref_items.append(
            f'<li class="reference-item" id="{html.escape(ref_id)}">'
            f'<span class="reference-number">[{ref_idx}]</span>'
            f'<div class="reference-content">{entry.html_body}</div>'
            f"</li>"
        )
        ref_tooltips[key] = entry.plain_preview

    references_html = (
        '<div class="references-divider"></div>'
        '<section class="tex-references" id="references">'
        '<h2 class="references-title">References</h2>'
        '<ol class="references-grid">' + "\n".join(ref_items) + "</ol>"
        "</section>"
    )

    # Batch-render EVERY math expression via KaTeX SSR!
    combined = "\n@@SPLIT@@\n".join([abstract_html, body_html, references_html])
    combined = conv.katex.render_all(combined)
    abstract_html, body_html, references_html = combined.split("\n@@SPLIT@@\n")

    bibtex_self = (
        "@misc{gong2026loopbehind,\n"
        f"  title  = {{{title_clean}}},\n"
        "  author = {Gong, Xijie and Wang, Tonghan},\n"
        "  year   = {2026},\n"
        "  url    = {https://xijiegong.com/loopbehind.pdf}\n"
        "}"
    )

    return CompiledPaper(
        title=title_clean,
        title_html=title_html,
        authors_html=authors_html,
        affiliation_html=affiliation_html,
        notes_html=notes_html,
        abstract_html=abstract_html,
        body_html=body_html,
        references_html=references_html,
        toc=conv.toc,
        bibtex_self=bibtex_self,
        ref_tooltips=ref_tooltips,
        figure_assets=conv.generated_figures,
    )
