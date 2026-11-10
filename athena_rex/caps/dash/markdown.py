"""Just enough Markdown for an item's body. Everything is escaped first, so raw HTML stays text; only http, https and mailto links link."""
import html
import re

_LINK = re.compile(r"\[([^\]\n]+)\]\((https?://[^\s)]+|mailto:[^\s)]+)\)")
_BARE = re.compile(r"(?<![\"'=>])\b(https?://[^\s<)]+)")


def _inline(text: str) -> str:
    text = html.escape(text, quote=False)
    text = re.sub(r"`([^`\n]+)`", r"<code>\1</code>", text)
    text = re.sub(r"\*\*([^*\n]+)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"<em>\1</em>", text)
    text = _LINK.sub(lambda m: f'<a href="{html.escape(m.group(2))}" rel="noopener noreferrer">{m.group(1)}</a>', text)
    return _BARE.sub(lambda m: f'<a href="{m.group(1)}" rel="noopener noreferrer">{m.group(1)}</a>', text)


def _table(rows) -> str:
    out = ["<table>"]
    for i, row in enumerate(rows):
        cells = [c.strip() for c in row.strip().strip("|").split("|")]
        if i == 1 and all(re.fullmatch(r":?-+:?", c) for c in cells):
            continue
        tag = "th" if i == 0 else "td"
        out.append("<tr>" + "".join(f"<{tag}>{_inline(c)}</{tag}>" for c in cells) + "</tr>")
    return "".join(out) + "</table>"


def render(text: str) -> str:
    out, para, lines = [], [], (text or "").splitlines()
    i = 0

    def flush():
        if para:
            out.append("<p>" + _inline(" ".join(para)) + "</p>")
            para.clear()

    while i < len(lines):
        line = lines[i]
        if line.startswith("```"):
            flush()
            block = []
            i += 1
            while i < len(lines) and not lines[i].startswith("```"):
                block.append(lines[i])
                i += 1
            out.append("<pre><code>" + html.escape("\n".join(block), quote=False) + "</code></pre>")
        elif re.match(r"#{1,6} ", line):
            flush()
            level = min(len(line) - len(line.lstrip("#")) + 2, 6)
            out.append(f"<h{level}>{_inline(line.lstrip('#').strip())}</h{level}>")
        elif re.match(r"\s*[-*] ", line) or re.match(r"\s*\d+\. ", line):
            flush()
            tag = "ol" if re.match(r"\s*\d", line) else "ul"
            block = []
            while i < len(lines) and (re.match(r"\s*[-*] ", lines[i]) or re.match(r"\s*\d+\. ", lines[i])):
                block.append(re.sub(r"^\s*(?:[-*]|\d+\.) ", "", lines[i]))
                i += 1
            out.append(f"<{tag}>" + "".join(f"<li>{_inline(b)}</li>" for b in block) + f"</{tag}>")
            continue
        elif line.lstrip().startswith("|"):
            flush()
            block = []
            while i < len(lines) and lines[i].lstrip().startswith("|"):
                block.append(lines[i])
                i += 1
            out.append(_table(block))
            continue
        elif not line.strip():
            flush()
        else:
            para.append(line.strip())
        i += 1
    flush()
    return "".join(out)
