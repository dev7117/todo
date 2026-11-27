"""Small Markdown subset, escaped by construction; never accept raw HTML.

Supports paragraphs, line breaks, headings, unordered lists, fenced code,
inline code, strong/emphasis and links. This is deliberately not CommonMark.
Only generated tags reach the browser; all user text and attributes are escaped.
"""
from html import escape
import re
from urllib.parse import urlsplit

INLINE = re.compile(r"`([^`\n]+)`|\*\*([^*\n]+)\*\*|\*([^*\n]+)\*|\[([^\]\n]+)\]\(([^\s()]+)\)")


def safe_link(url: str) -> bool:
    if any(ord(char) <= 32 or ord(char) == 127 for char in url):
        return False
    try:
        parsed = urlsplit(url)
        return (parsed.scheme.lower() in {"http", "https"} and bool(parsed.netloc)
                or parsed.scheme.lower() == "mailto" and bool(parsed.path)
                or url.startswith("#"))
    except ValueError:
        return False


def inline(text: str) -> str:
    parts, end = [], 0
    for match in INLINE.finditer(text):
        parts.append(escape(text[end:match.start()]))
        code, strong, emphasis, label, url = match.groups()
        if code is not None:
            parts.append(f"<code>{escape(code)}</code>")
        elif strong is not None:
            parts.append(f"<strong>{escape(strong)}</strong>")
        elif emphasis is not None:
            parts.append(f"<em>{escape(emphasis)}</em>")
        elif safe_link(url):
            parts.append(f'<a href="{escape(url, quote=True)}" rel="noreferrer">{escape(label)}</a>')
        else:
            parts.append(escape(label))
        end = match.end()
    parts.append(escape(text[end:]))
    return "".join(parts)


def render(body: str) -> str:
    output, paragraph, listing, code = [], [], [], []
    fenced = False

    def flush():
        if paragraph:
            output.append("<p>" + "<br>".join(inline(line) for line in paragraph) + "</p>")
            paragraph.clear()
        if listing:
            output.append("<ul>" + "".join(f"<li>{inline(line)}</li>" for line in listing) + "</ul>")
            listing.clear()

    for line in body.splitlines():
        if line.startswith("```"):
            flush()
            if fenced:
                output.append("<pre><code>" + escape("\n".join(code)) + "</code></pre>")
                code.clear()
            fenced = not fenced
        elif fenced:
            code.append(line)
        elif not line.strip():
            flush()
        elif heading := re.match(r"^(#{1,6})\s+(.+)$", line):
            flush()
            level = len(heading[1])
            output.append(f"<h{level}>{inline(heading[2])}</h{level}>")
        elif line.startswith(("- ", "* ")):
            if paragraph:
                flush()
            listing.append(line[2:])
        else:
            if listing:
                flush()
            paragraph.append(line)
    flush()
    if fenced:
        output.append("<pre><code>" + escape("\n".join(code)) + "</code></pre>")
    return "\n".join(output)
