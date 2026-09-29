#!/usr/bin/env python3
"""Check local documentation links, heading fragments, fences and SVG assets.

Offline, standard library only. Remote URLs and Mermaid syntax are not fetched
or validated. Run from any directory: python3 scripts/check_docs.py
"""
from __future__ import annotations

import html
import re
import sys
import unicodedata
from pathlib import Path
from urllib.parse import unquote, urlsplit
from xml.etree import ElementTree

ROOT = Path(__file__).resolve().parents[1]


def prose(text: str) -> tuple[str, bool]:
    """Omit fenced code while retaining line numbers for diagnostic messages."""
    marker = ""
    output = []
    for line in text.splitlines():
        fence = re.match(r"^\s{0,3}(`{3,}|~{3,})", line)
        if fence:
            token = fence[1]
            if not marker:
                marker = token
            elif token[0] == marker[0] and len(token) >= len(marker):
                marker = ""
            output.append("\ufffc")
        else:
            output.append("\ufffc" if marker else line)
    return "\n".join(output), bool(marker)


def anchors(text: str) -> set[str]:
    """GitHub-style heading slugs for the headings used in these guides."""
    body, _ = prose(text)
    result = set(re.findall(r'<a\s+[^>]*id=["\']([^"\']+)', body))
    seen: dict[str, int] = {}
    for title in re.findall(r"^#{1,6}\s+(.+?)\s*#*\s*$", body, re.MULTILINE):
        title = re.sub(r"<[^>]+>", "", html.unescape(title)).lower()
        slug = "".join(c for c in title if c in " -_" or unicodedata.category(c)[0] in "LNM").replace(" ", "-")
        count = seen.get(slug, 0)
        seen[slug] = count + 1
        result.add(f"{slug}-{count}" if count else slug)
    return result


def check_page(path: Path, root: Path) -> list[str]:
    text = path.read_text(encoding="utf-8")
    body, unclosed = prose(text)
    label = str(path.relative_to(root))
    errors = []
    if unclosed:
        errors.append(f"{label}: unclosed code fence")
    if len(re.findall(r"^# \S", body, re.MULTILINE)) != 1:
        errors.append(f"{label}: use exactly one H1 title")
    # Nonblank placeholders exclude intentional whitespace in code examples.
    if re.search(r"\n[ \t]*\n[ \t]*\n", body):
        errors.append(f"{label}: repeated blank lines; use one between blocks")
    links = list(re.finditer(r'\]\(([^\s)]+)(?:\s+"[^"]*")?\)', body))
    links += list(re.finditer(r'(?:href|src)=["\']([^"\']+)', body))
    links += list(re.finditer(r'^\s*\[[^\]]+\]:\s*(\S+)', body, re.MULTILINE))
    for match in links:
        url = html.unescape(match[1].strip("<>"))
        parsed = urlsplit(url)
        if parsed.scheme or parsed.netloc:
            continue
        target = (path.parent / unquote(parsed.path)).resolve() if parsed.path else path
        location = f"{label}:{body[:match.start()].count(chr(10)) + 1}"
        if not target.is_relative_to(root.resolve()):
            errors.append(f"{location}: link escapes repository: {url}")
        elif not target.exists():
            errors.append(f"{location}: missing local target: {url}")
        elif parsed.fragment and target.suffix.lower() == ".md":
            if unquote(parsed.fragment) not in anchors(target.read_text(encoding="utf-8")):
                errors.append(f"{location}: missing heading: {url}")
    return errors


def check(root: Path) -> list[str]:
    pages = [root / "README.md", *sorted((root / "docs").rglob("*.md"))]
    errors = [error for page in pages for error in check_page(page, root)]
    for svg in (root / "docs").rglob("*.svg"):
        try:
            document = ElementTree.parse(svg).getroot()
            tags = {element.tag.rsplit("}", 1)[-1] for element in document.iter()}
            if not {"title", "desc"}.issubset(tags):
                errors.append(f"{svg.relative_to(root)}: add an accessible title and description")
            if tags & {"script", "foreignObject"}:
                errors.append(f"{svg.relative_to(root)}: SVG must be static and self-contained")
        except ElementTree.ParseError as exc:
            errors.append(f"{svg.relative_to(root)}: invalid SVG: {exc}")
    return errors


if __name__ == "__main__":
    failures = check(ROOT)
    if failures:
        print("Documentation checks failed:\n" + "\n".join(f"- {error}" for error in failures))
        sys.exit(1)
    count = 1 + len(list((ROOT / "docs").rglob("*.md")))
    print(f"Documentation checks passed: {count} Markdown pages; local targets, anchors, spacing, fences and SVG assets.")
