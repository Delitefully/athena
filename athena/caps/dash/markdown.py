"""An item's body as HTML, byte for byte as the old Go dash rendered it (dash/markdown.go): goldmark v1.8.6 with GFM tables
(alignment as an align attribute, the page's CSP refusing a style one), Linkify and Strikethrough, its default renderer
(raw HTML omitted, dangerous URLs dropped), and safeLinks on top: a link whose scheme is not http, https, mailto or
relative is unwrapped to its text, such an autolink to its label, and every image to its alt text.

This is a port of the parts of goldmark that config reaches, kept close to its structure (a block pass over the source,
then an inline pass over each block's lines, then the AST transformers and the renderer), quirks included, so the two
agree on what an LLM writes in a brief. Standard library only."""
import re
import threading
import unicodedata
from html.entities import html5 as _ENTITIES
from urllib.parse import quote as _quote

_SPACES = frozenset("\t\n\r ")  # util.IsSpace
_TRIM = " \t\n\x0b\x0c\r"  # util.TrimLeftSpace / TrimRightSpace
_PUNCT = frozenset("!\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~")
_HEX = frozenset("0123456789abcdefABCDEF")
_DIGITS = frozenset("0123456789")
_ALNUM = frozenset("0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ")
_LETTERS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ")
_URL_SAFE = frozenset("!#$&'()*+,-./0123456789:;=?@ABCDEFGHIJKLMNOPQRSTUVWXYZ_abcdefghijklmnopqrstuvwxyz~")
_EMAIL = frozenset("!#$%&'*+-./0123456789=?ABCDEFGHIJKLMNOPQRSTUVWXYZ^_`abcdefghijklmnopqrstuvwxyz{|}~")
_SCHEME = _ALNUM | frozenset("+-.")
_GO_SPACE = ("\t\n\x0b\x0c\r \x85\xa0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a"
             "\u2028\u2029\u202f\u205f\u3000")  # unicode.IsSpace
_ESCAPES = {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "\x00": "\ufffd"}
_ESCAPE_RE = re.compile("[&<>\"\x00]")

_CONTINUE, _CLOSE, _HAS_CHILDREN, _NO_CHILDREN, _REQUIRE_PARAGRAPH = 2, 4, 8, 16, 32
_PARA_CONTINUATION, _NEW_BLOCKS, _NO_BLOCKS = 1, 2, 3
_HARD, _SOFT, _VISIBLE = 1, 2, 4
_BULLET, _ORDERED = 1, 2


# --- characters and strings, as goldmark's util reads them ---------------------------------------------------------------

def _is_blank(s):
    return all(c in _SPACES for c in s)


def _trim_left_space_length(s):
    i = 0
    while i < len(s) and s[i] in _SPACES:
        i += 1
    return i


def _trim_right_space_length(s):
    i = len(s) - 1
    while i >= 0 and s[i] in _SPACES:
        i -= 1
    return len(s) - 1 - i


def _tab_width(pos):
    return 4 - pos % 4


def _indent_width(s, cur):
    width = pos = 0
    for c in s:
        if c == " ":
            width += 1
        elif c == "\t":
            width += _tab_width(cur + width)
        else:
            break
        pos += 1
    return width, pos


def _indent_position(s, cur, width, paddingv=0):
    if width == 0:
        return 0, paddingv
    w = i = 0
    p = paddingv
    while i < len(s):
        if p > 0:
            p -= 1
            w += 1
            i += 1
            continue
        if s[i] == "\t" and w < width:
            w += _tab_width(cur + w)
        elif s[i] == " " and w < width:
            w += 1
        else:
            break
        i += 1
    if w >= width:
        return i - paddingv, w - width
    return -1, -1


def _first_non_space(s):
    for i, c in enumerate(s):
        if c in " \t":
            continue
        return -1 if c == "\n" else i
    return -1


def _is_punct_rune(c):
    return unicodedata.category(c)[0] in "PS"


def _is_space_rune(c):
    return c in _GO_SPACE


def _to_link_reference(v):
    v = v.strip(_TRIM).casefold()
    return re.sub("[\t\n\r ]+", " ", v)


def _byte_len(s):
    """Go measures a string in UTF-8 bytes."""
    return len(s.encode("utf-8"))


def _escape_html(s):
    return _ESCAPE_RE.sub(lambda m: _ESCAPES[m.group()], s)


def _valid_rune(v):
    return "\ufffd" if v == 0 or v > 0x10FFFF or 0xD800 <= v <= 0xDFFF else chr(v)


def _read_while(s, i, chars):
    j = i
    while j < len(s) and s[j] in chars:
        j += 1
    return j


def _write(out, s):
    """html.Writer.Write: a backslash escape and an entity resolve, then everything is escaped."""
    escaped = False
    n = i = 0
    limit = len(s)
    while i < limit:
        c = s[i]
        if escaped and c in _PUNCT:
            out.append(_escape_html(s[n:i - 1]))
            n = i
            escaped = False
            i += 1
            continue
        if c == "\x00":
            out.append(_escape_html(s[n:i]) + "\ufffd")
            n = i + 1
            escaped = False
            i += 1
            continue
        if c == "&":
            nxt = i + 1
            if nxt < limit and s[nxt] == "#":
                nn = nxt + 1
                if nn < limit and s[nn] in "xX":
                    j = _read_while(s, nn + 1, _HEX)
                    if j > nn + 1 and j < limit and s[j] == ";" and j - nn - 1 < 7:
                        out.append(_escape_html(s[n:i]) + _escape_html(_valid_rune(int(s[nn + 1:j], 16))))
                        n = i = j + 1
                        continue
                elif nn < limit and s[nn] in _DIGITS:
                    j = _read_while(s, nn, _DIGITS)
                    if j < limit and j - nn < 8 and s[j] == ";":
                        out.append(_escape_html(s[n:i]) + _escape_html(_valid_rune(int(s[nn:j]))))
                        n = i = j + 1
                        continue
            else:
                j = _read_while(s, nxt, _ALNUM)
                if j > nxt and j < limit and s[j] == ";":
                    entity = _ENTITIES.get(s[nxt:j + 1])
                    if entity is not None:
                        out.append(_escape_html(s[n:i]) + _escape_html(entity))
                        n = i = j + 1
                        continue
        escaped = c == "\\"
        i += 1
    out.append(_escape_html(s[n:]))


def _unescape_punctuations(s):
    return re.sub(r"\\([!-/:-@\[-`{-~])", r"\1", s)


def _go_parse_uint0(digits):
    """strconv.ParseUint(s, 0, 32) on digits: a leading 0 reads octal, and a syntax error is 0."""
    if len(digits) > 1 and digits[0] == "0":
        return 0 if set(digits) & {"8", "9"} else int(digits, 8)
    return int(digits)


def _resolve_references(s):
    """util.ResolveNumericReferences then util.ResolveEntityNames, as URLEscape resolves a destination."""
    def numeric(m):
        v = int(m.group(1), 16) if m.group(1) is not None else _go_parse_uint0(m.group(2))
        return _valid_rune(v)

    s = re.sub(r"&#[xX]([0-9a-fA-F]+);|&#([0-9]{1,7});", numeric, s)
    return re.sub(r"&([0-9A-Za-z]+);", lambda m: _ENTITIES.get(m.group(1) + ";", m.group()), s)


def _url_escape(v, resolve):
    if resolve:
        v = _resolve_references(_unescape_punctuations(v))
    out = []
    n = i = 0
    limit = len(v)
    while i < limit:
        c = v[i]
        if c in _URL_SAFE:
            i += 1
            continue
        if c == "%" and i + 2 < limit and v[i + 1] in _HEX and v[i + 2] in _HEX:
            i += 3
            continue
        out.append(v[n:i])
        out.append("%20" if c == " " else _quote(c.encode("utf-8", "replace"), safe=""))
        i += 1
        n = i
    out.append(v[n:])
    return "".join(out)


def _is_dangerous_url(url):
    low = url.lower()
    if low.startswith("data:image/") and len(url) >= 11:
        return not low[11:].startswith(("png;", "gif;", "jpeg;", "webp;"))
    return low.startswith(("javascript:", "vbscript:", "file:", "data:"))


def _find_url_index(b):
    if not b or b[0] not in _LETTERS:
        return -1
    i = 1
    while i < len(b) and b[i] in _SCHEME:
        i += 1
    if i == 1 or i > 33 or i >= len(b) or b[i] != ":":
        return -1
    i += 1
    while i < len(b) and b[i] > " " and b[i] not in "<>":
        i += 1
    return i


_EMAIL_DOMAIN = re.compile(r"[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?(?:\.[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?)*")


_EMAIL_LOCAL = re.compile("[" + re.escape("".join(sorted(_EMAIL))) + "]*")


def _find_email_index(b):
    i = _EMAIL_LOCAL.match(b).end()
    if i == 0 or i >= len(b) or b[i] != "@":
        return -1
    i += 1
    if i >= len(b):
        return -1
    m = _EMAIL_DOMAIN.match(b, i)
    return m.end() if m else -1


# --- the URL allowlist ------------------------------------------------------------------------------------------------------

_HOST_OK = _ALNUM | frozenset("-_.~!$&'()*+,;=:[]<>\"")
_USERINFO_OK = _ALNUM | frozenset("-._:~!$&'()*+,;=%@")


def _unescape_ok(s, host=False):
    i = 0
    while i < len(s):
        if s[i] == "%":
            if i + 2 >= len(s) or s[i + 1] not in _HEX or s[i + 2] not in _HEX:
                return False
            if host and int(s[i + 1], 16) < 8 and s[i:i + 3] != "%25":
                return False
            i += 3
            continue
        if host and s[i] < "\x80" and s[i] not in _HOST_OK:
            return False
        i += 1
    return True


def _valid_port(p):
    return p == "" or (p[0] == ":" and all(c in _DIGITS for c in p[1:]))


def _parse_authority(authority):
    i = authority.rfind("@")
    host = authority[i + 1:]
    if host.startswith("["):
        j = host.rfind("]")
        if j < 0 or not _valid_port(host[j + 1:]):
            return False
    else:
        j = host.rfind(":")
        if j != -1 and not _valid_port(host[j:]):
            return False
    if not _unescape_ok(host, host=True):
        return False
    userinfo = authority[:i] if i >= 0 else ""
    return all(c in _USERINFO_OK for c in userinfo) and _unescape_ok(userinfo)


def _url_scheme(raw):
    """The scheme Go's url.Parse finds in raw, or None where it returns an error."""
    u, _, frag = raw.partition("#")
    if re.search("[\x00-\x1f\x7f]", u):
        return None
    scheme, rest = "", u
    for i, c in enumerate(u):
        if c in _LETTERS:
            continue
        if c in _SCHEME:
            if i == 0:
                break
            continue
        if c == ":":
            if i == 0:
                return None
            scheme, rest = u[:i], u[i + 1:]
        break
    if u != "*":
        rest = rest[:-1] if rest.endswith("?") and rest.count("?") == 1 else rest.partition("?")[0]
        if rest.startswith("/") or not scheme:
            if not rest.startswith("/") and ":" in rest.partition("/")[0]:
                return None
            if (scheme or not rest.startswith("///")) and rest.startswith("//"):
                authority, slash, path = rest[2:].partition("/")
                if not _parse_authority(authority):
                    return None
                rest = slash + path
            if not _unescape_ok(rest):
                return None
    if frag and not _unescape_ok(frag):
        return None
    return scheme


def allowed_url(raw: str) -> bool:
    """An http, https or mailto URL, or a relative one. Any other scheme, in any case, is refused, and so is what does not parse
    as a URL (a control character, say), as Go's url.Parse has it."""
    scheme = _url_scheme(raw.strip(_GO_SPACE))
    return scheme is not None and scheme.lower() in ("", "http", "https", "mailto")


# --- segments, nodes and readers (goldmark's text and ast packages) ---------------------------------------------------------

class _Seg:
    __slots__ = ("start", "stop", "padding", "nl")

    def __init__(self, start, stop, padding=0, nl=False):
        self.start, self.stop, self.padding, self.nl = start, stop, padding, nl

    def value(self, src):
        v = src[self.start:self.stop]
        if self.padding:
            v = " " * self.padding + v
        if self.nl and v and v[-1] != "\n":
            v += "\n"
        return v

    def length(self):
        return self.stop - self.start + self.padding

    def between(self, other):
        return _Seg(self.start, other.start, self.padding - other.padding)

    def is_empty(self):
        return self.start >= self.stop and self.padding == 0

    def with_start(self, v):
        return _Seg(v, self.stop, self.padding)

    def with_stop(self, v):
        return _Seg(self.start, v, self.padding)

    def trim_right_space(self, src):
        v = src[self.start:self.stop]
        n = _trim_right_space_length(v)
        if n == len(v):
            return _Seg(self.start, self.start)
        return _Seg(self.start, self.stop - n, self.padding)

    def trim_left_space(self, src):
        return _Seg(self.start + _trim_left_space_length(src[self.start:self.stop]), self.stop)

    def trim_left_space_width(self, width, src):
        padding = self.padding
        while width > 0 and padding:
            padding -= 1
            width -= 1
        if width == 0:
            return _Seg(self.start, self.stop, padding)
        start = self.start
        for c in src[self.start:self.stop]:
            if start >= self.stop - 1 or width <= 0:
                break
            if c == " ":
                width -= 1
            elif c == "\t":
                width -= 4
            else:
                break
            start += 1
        if width < 0:
            padding = -width
        return _Seg(start, self.stop, padding)


class _Node:
    def __init__(self, kind, **attrs):
        self.kind = kind
        self.parent = self.first = self.last = self.prev = self.next = None
        self.lines = []
        self.blank_prev = False
        self.__dict__.update(attrs)

    def children(self):
        c = self.first
        while c is not None:
            yield c
            c = c.next

    def count(self):
        return sum(1 for _ in self.children())

    def remove_child(self, v):
        if v.parent is not self:
            return
        if v.prev is not None:
            v.prev.next = v.next
        else:
            self.first = v.next
        if v.next is not None:
            v.next.prev = v.prev
        else:
            self.last = v.prev
        v.parent = v.prev = v.next = None

    def append(self, v):
        if v.parent is not None:
            v.parent.remove_child(v)
        if self.first is None:
            self.first = v
            v.prev = v.next = None
        else:
            self.last.next = v
            v.prev = self.last
        v.parent = self
        self.last = v

    def insert_before(self, v1, ins):
        if v1 is None:
            self.append(ins)
            return
        if ins.parent is not None:
            ins.parent.remove_child(ins)
        if v1.parent is self:
            if v1.prev is not None:
                v1.prev.next = ins
                ins.prev = v1.prev
            else:
                self.first = ins
                ins.prev = None
            ins.next = v1
            v1.prev = ins
            ins.parent = self

    def insert_after(self, v1, ins):
        self.insert_before(v1.next, ins)

    def replace_child(self, v1, ins):
        self.insert_before(v1, ins)
        self.remove_child(v1)


def _text(seg, raw=False):
    return _Node("text", seg=seg, raw=raw, soft=False, hard=False)


def _merge_or_append_text(parent, seg):
    last = parent.last
    if last is not None and last.kind == "text" and last.seg.stop == seg.start and not last.soft:
        last.seg = last.seg.with_stop(seg.stop)
    else:
        parent.append(_text(seg))


def _merge_or_replace_text(parent, n, seg):
    prev = n.prev
    if prev is not None and prev.kind == "text" and prev.seg.stop == seg.start and not prev.soft:
        prev.seg = prev.seg.with_stop(seg.stop)
        parent.remove_child(n)
    else:
        parent.replace_child(n, _text(seg))


class _ReaderBase:
    def position(self):
        return self.line, self.pos

    def skip_spaces(self):
        chars = 0
        while True:
            line, seg = self.peek_line()
            if line is None:
                return seg, chars, False
            for i, c in enumerate(line):
                if c in _SPACES:
                    chars += 1
                    self.advance(1)
                    continue
                return seg.with_start(seg.start + i + 1), chars, True

    def skip_blank_lines(self):
        lines = 0
        while True:
            line, seg = self.peek_line()
            if line is None:
                return seg, lines, False
            if not _is_blank(line):
                return seg, lines, True
            lines += 1
            self.advance_line()

    def find_closure(self, opener, closer):
        """text.FindClosure with the link options: no nesting, across lines, the reader left where it stopped."""
        ret = []
        while True:
            bs, seg = self.peek_line()
            if bs is None:
                return None
            i = 0
            while i < len(bs):
                c = bs[i]
                if c == "\\" and i < len(bs) - 1 and bs[i + 1] in _PUNCT:
                    i += 2
                    continue
                if c == closer:
                    ret.append(seg.with_stop(seg.start + i - seg.padding))
                    self.advance(i + 1)
                    return ret
                if c == opener:
                    return None
                i += 1
            self.advance_line()
            ret.append(seg)

    def line_offset(self):
        if self._lo < 0:
            head, start = self.head, self.pos.start
            tab = self.src.find("\t", head, start) if start > head else -1
            if tab < 0:
                v = max(start - head, 0)
            else:
                v = tab - head
                for i in range(tab, start):
                    v += _tab_width(v) if self.src[i] == "\t" else 1
            self._lo = v - self.pos.padding
        return self._lo

    def set_padding(self, v):
        self._lo = -1
        self.pos = _Seg(self.pos.start, self.pos.stop, v, self.pos.nl)

    def advance_and_set_padding(self, n, padding):
        self.advance(n)
        if padding > self.pos.padding:
            self.set_padding(padding)


class _Reader(_ReaderBase):
    """text.Reader over the whole source, its peeked-line cache included."""

    def __init__(self, src):
        self.src, self.n = src, len(src)
        self.line, self.head, self._lo = -1, 0, -1
        self.pos = _Seg(0, 0)
        self.peeked = None
        self.advance_line()

    def peek_line(self):
        p = self.pos
        if 0 <= p.start < self.n:
            if self.peeked is None:
                self.peeked = p.value(self.src)
            return self.peeked, p
        return None, p

    def advance(self, n):
        self._lo = -1
        p = self.pos
        if n < len(self.peeked or "") and p.padding == 0:
            self.pos = _Seg(p.start + n, p.stop, 0, p.nl)
            self.peeked = None
            return
        self.peeked = None
        while n > 0 and self.pos.start < self.n:
            p = self.pos
            if p.padding:
                self.pos = _Seg(p.start, p.stop, p.padding - 1, p.nl)
            elif self.src[p.start] == "\n":
                self.advance_line()
            else:
                self.pos = _Seg(p.start + 1, p.stop, 0, p.nl)
            n -= 1

    def advance_to_eol(self):
        if self.pos.start >= self.n:
            return
        self._lo = -1
        i = self.src.find("\n", self.pos.start)
        self.peeked = None
        self.pos = _Seg(i if i != -1 else self.n, self.pos.stop, 0, self.pos.nl)

    def advance_line(self):
        self._lo = -1
        self.peeked = None
        start = self.pos.stop
        self.head = start
        if start < 0 or start >= self.n:
            self.pos = _Seg(start, self.pos.stop, self.pos.padding, self.pos.nl)
            return
        i = self.src.find("\n", start)
        self.pos = _Seg(start, i + 1 if i != -1 else self.n)
        self.line += 1

    def set_position(self, line, pos):
        self._lo = -1
        self.line, self.pos = line, pos


class _BlockReader(_ReaderBase):
    """text.BlockReader over a block's lines."""

    def __init__(self, src, segs):
        self.src = src
        self.segs, self.nsegs = segs, len(segs)
        self.line, self.head, self._lo = -1, 0, -1
        self.last = segs[-1].stop if segs else 0
        self.pos = _Seg(-1, -1)
        self.advance_line()

    def value(self, seg):
        line = self.nsegs - 1
        while line >= 0 and seg.start < self.segs[line].start:
            line -= 1
        out = []
        i = seg.start
        while line < self.nsegs:
            s = self.segs[line]
            if i < 0:
                i = s.start
            if s.padding:
                out.append(" " * s.padding)
            end = min(seg.stop, s.stop)
            if i < end:
                out.append(self.src[i:end])
            i = -1
            if s.stop > seg.stop:
                break
            line += 1
        return "".join(out)

    def rest(self):
        """What a regexp reads from here: this line on, through the block's last."""
        line, seg = self.peek_line()
        if line is None:
            return ""
        return line + "".join(s.value(self.src) for s in self.segs[self.line + 1:])

    def preceding_char(self):
        if self.pos.padding:
            return " "
        if not self.nsegs:
            return "\n"
        if self.line == 0 and self.pos.start <= self.segs[0].start:
            return "\n"
        i = self.pos.start - 1
        if i < 0 or i >= len(self.src):
            return "\n"
        return self.src[i]

    def peek(self):
        p = self.pos
        if self.line < self.nsegs and 0 <= p.start < self.last:
            return " " if p.padding else self.src[p.start]
        return None

    def peek_line(self):
        p = self.pos
        if self.line < self.nsegs and 0 <= p.start < self.last:
            return p.value(self.src), p
        return None, p

    def advance(self, n):
        self._lo = -1
        p = self.pos
        if n < p.stop - p.start and p.padding == 0:
            self.pos = _Seg(p.start + n, p.stop, 0, p.nl)
            return
        while n > 0:
            p = self.pos
            if p.padding:
                self.pos = _Seg(p.start, p.stop, p.padding - 1, p.nl)
            elif p.start >= p.stop - 1 and p.stop < self.last:
                self.advance_line()
            else:
                self.pos = _Seg(p.start + 1, p.stop, 0, p.nl)
            n -= 1

    def advance_to_eol(self):
        self._lo = -1
        p = self.pos
        start = p.stop - 1 if self.src[p.stop - 1] == "\n" else p.stop
        self.pos = _Seg(start, p.stop, 0, p.nl)

    def advance_line(self):
        self.set_position(self.line + 1, None)
        self.head = self.pos.start

    def set_position(self, line, pos):
        self._lo = -1
        self.line = line
        if pos is None or pos.start == -1:
            if line < self.nsegs:
                self.pos = self.segs[line]
                self.head = self.pos.start
        else:
            self.pos = pos
            if line < self.nsegs:
                self.head = self.segs[line].start

    def match(self, rx):
        line, pos = self.position()
        m = rx.match(self.rest())
        self.set_position(line, pos)
        if m:
            self.advance(m.end())
        return m


class _Ctx:
    def __init__(self, src):
        self.src = src
        self.store = {}
        self.refs = {}
        self.opened = []
        self.block_offset = -1
        self.first_delim = self.last_delim = None

    def last_node(self):
        return self.opened[-1][0] if self.opened else None

    def add_reference(self, label, dest, title):
        self.refs.setdefault(_to_link_reference(label), (dest, title))


# --- block parsers ------------------------------------------------------------------------------------------------------

class _BlockParser:
    triggers = ""
    interrupts = True
    indented = False

    def cont(self, node, reader, pc):
        return _CLOSE

    def close(self, node, reader, pc):
        pass


def _setext_bar(line):
    start = len(line) - len(line.lstrip(" "))
    if start > 3:
        return None
    end = len(line)
    level1 = len(line[start:]) - len(line[start:].lstrip("="))
    c, level2 = "=", 0
    if level1 == 0:
        level2 = len(line[start:]) - len(line[start:].lstrip("-"))
        c = "-"
    if line[end - 1] in _SPACES:
        end -= _trim_right_space_length(line[start:end])
    if (level1 > 0 and start + level1 == end) or (level2 > 0 and start + level2 == end):
        return c
    return None


class _Setext(_BlockParser):
    triggers = "-="

    def open(self, parent, reader, pc):
        last = pc.last_node()
        if last is None or last.kind != "paragraph" or last.parent is not parent:
            return None, _NO_CHILDREN
        line, seg = reader.peek_line()
        c = _setext_bar(line)
        if c is None:
            return None, _NO_CHILDREN
        node = _Node("heading", level=1 if c == "=" else 2)
        node.lines = [seg]
        pc.store["paragraph"] = last
        return node, _NO_CHILDREN | _REQUIRE_PARAGRAPH

    def close(self, node, reader, pc):
        seg = node.lines[0]
        node.lines = []
        tmp = pc.store.pop("paragraph", None)
        if tmp is None:
            return
        if not tmp.lines:
            nxt = node.next
            seg = seg.trim_left_space(reader.src)
            if nxt is None or nxt.kind != "paragraph":
                para = _Node("paragraph", lines=[seg])
                node.parent.insert_after(node, para)
            else:
                nxt.lines.insert(0, seg)
            node.parent.remove_child(node)
        else:
            node.lines = tmp.lines
            node.blank_prev = tmp.blank_prev
            if tmp.parent is not None:
                tmp.parent.remove_child(tmp)


def _is_thematic_break(line, offset):
    w, pos = _indent_width(line, offset)
    if w > 3:
        return False
    mark, count = None, 0
    for c in line[pos:]:
        if c in _SPACES:
            continue
        if mark is None:
            mark, count = c, 1
            if c in "*-_":
                continue
            return False
        if c != mark:
            return False
        count += 1
    return count > 2


class _ThematicBreak(_BlockParser):
    triggers = "-*_"

    def open(self, parent, reader, pc):
        line, _ = reader.peek_line()
        if _is_thematic_break(line, reader.line_offset()):
            reader.advance_to_eol()
            return _Node("hr"), _NO_CHILDREN
        return None, _NO_CHILDREN


def _parse_list_item(line):
    i, l = 0, len(line)
    while i < l and line[i] == " ":
        i += 1
    m = [0, i, i, 0, 0, 0]
    if i > 3:
        return m, 0
    if i < l and line[i] in "-*+":
        i += 1
        m[3] = i
        typ = _BULLET
    elif i < l:
        i = _read_while(line, i, _DIGITS)
        m[3] = i
        if m[3] == m[2] or m[3] - m[2] > 9:
            return m, 0
        if i < l and line[i] in ".)":
            i += 1
            m[3] = i
        else:
            return m, 0
        typ = _ORDERED
    else:
        return m, 0
    if i < l and line[i] != "\n" and _indent_width(line[i:], 0)[0] == 0:
        return m, 0
    if i >= l:
        m[4] = m[5] = -1
        return m, typ
    m[4], m[5] = i, l
    if line[l - 1] == "\n" and line[i] != "\n":
        m[5] -= 1
    return m, typ


def _last_offset(node):
    return node.last.offset if node.last is not None else 0


class _List(_BlockParser):
    triggers = "-+*0123456789"

    def open(self, parent, reader, pc):
        last = pc.last_node()
        if (last is not None and last.kind == "list") or pc.store.get("skip-list"):
            pc.store["skip-list"] = None
            return None, _NO_CHILDREN
        line, _ = reader.peek_line()
        m, typ = _parse_list_item(line)
        if not typ:
            return None, _NO_CHILDREN
        start = int(line[m[2]:m[3] - 1]) if typ == _ORDERED else -1
        if last is not None and last.kind == "paragraph" and last.parent is parent:
            if typ == _ORDERED and start != 1:
                return None, _NO_CHILDREN
            if m[4] < 0 or _is_blank(line[m[4]:m[5]]):
                return None, _NO_CHILDREN
        node = _Node("list", marker=line[m[3] - 1], tight=True, start=max(start, 0))
        pc.store["empty-item"] = None
        return node, _HAS_CHILDREN

    def cont(self, node, reader, pc):
        line, _ = reader.peek_line()
        if _is_blank(line):
            if node.last.first is None:
                pc.store["empty-item"] = True
            return _CONTINUE | _HAS_CHILDREN
        offset = _last_offset(node)
        last_is_empty = node.last.first is None
        indent, _ = _indent_width(line, reader.line_offset())
        if indent < offset or last_is_empty:
            if indent < 4:
                m, typ = _parse_list_item(line)
                if typ and m[1] - offset < 4:
                    marker = line[m[3] - 1]
                    if marker != node.marker or (typ == _ORDERED) != (node.marker in ".)"):
                        return _CLOSE
                    if _is_thematic_break(line[m[3] - 1:], 0):
                        last = pc.last_node()
                        heading = last is not None and last.kind == "paragraph" and _setext_bar(line[m[3] - 1:]) == "-"
                        if not heading:
                            return _CLOSE
                    return _CONTINUE | _HAS_CHILDREN
            if not last_is_empty:
                return _CLOSE
        if last_is_empty and indent < offset:
            return _CLOSE
        if pc.store.get("empty-item") is not None:
            return _CLOSE
        return _CONTINUE | _HAS_CHILDREN

    def close(self, node, reader, pc):
        c = node.first
        while c is not None and node.tight:
            if c.first is not None and c.first is not c.last:
                c1 = c.first.next
                while c1 is not None:
                    if c1.blank_prev:
                        node.tight = False
                        break
                    c1 = c1.next
            if c is not node.first and c.blank_prev:
                node.tight = False
            c = c.next
        if node.tight:
            for item in node.children():
                gc = item.first
                while gc is not None:
                    nxt = gc.next
                    if gc.kind == "paragraph":
                        item.replace_child(gc, _Node("text_block", lines=gc.lines))
                    gc = nxt


class _ListItem(_BlockParser):
    triggers = "-+*0123456789"

    def open(self, parent, reader, pc):
        if parent.kind != "list":
            return None, _NO_CHILDREN
        offset = _last_offset(parent)
        line, _ = reader.peek_line()
        m, typ = _parse_list_item(line)
        if not typ or m[1] - offset > 3:
            return None, _NO_CHILDREN
        pc.store["empty-item"] = None
        if m[4] < 0 or _is_blank(line[m[4]:]):
            item_offset = 1
        else:
            item_offset = _indent_width(line[m[4]:], m[4])[0]
            if item_offset > 4:
                item_offset = 1
        node = _Node("list_item", offset=m[3] + item_offset)
        if m[4] < 0 or _is_blank(line[m[4]:m[5]]):
            return node, _NO_CHILDREN
        pos, padding = _indent_position(line[m[4]:], m[4], item_offset)
        reader.advance_and_set_padding(m[3] + pos, padding)
        return node, _HAS_CHILDREN

    def cont(self, node, reader, pc):
        line, _ = reader.peek_line()
        if _is_blank(line):
            reader.advance_to_eol()
            return _CONTINUE | _HAS_CHILDREN
        offset = _last_offset(node.parent)
        is_empty = node.first is None and pc.store.get("empty-item") is not None
        indent, _ = _indent_width(line, reader.line_offset())
        if (is_empty or indent < offset) and indent < 4:
            if _parse_list_item(line)[1]:
                pc.store["skip-list"] = True
                return _CLOSE
            if not is_empty:
                return _CLOSE
        pos, padding = _indent_position(line, reader.line_offset(), offset)
        reader.advance_and_set_padding(pos, padding)
        return _CONTINUE | _HAS_CHILDREN


def _preserve_leading_tab(seg, reader, indent):
    offset = reader.line_offset() + indent
    sl, ss = reader.position()
    reader.set_position(sl, _Seg(ss.start - 1, ss.stop))
    if offset == reader.line_offset():
        seg = _Seg(seg.start - 1, seg.stop, 0, seg.nl)
    reader.set_position(sl, ss)
    return seg


class _CodeBlock(_BlockParser):
    interrupts = False
    indented = True

    def _line(self, node, reader, pos, padding):
        reader.advance_and_set_padding(pos, padding)
        _, seg = reader.peek_line()
        if seg.padding:
            seg = _preserve_leading_tab(seg, reader, 0)
        node.lines.append(_Seg(seg.start, seg.stop, seg.padding, True))
        reader.advance_to_eol()

    def open(self, parent, reader, pc):
        line, _ = reader.peek_line()
        pos, padding = _indent_position(line, reader.line_offset(), 4)
        if pos < 0 or _is_blank(line):
            return None, _NO_CHILDREN
        node = _Node("code_block", raw=True)
        self._line(node, reader, pos, padding)
        return node, _NO_CHILDREN

    def cont(self, node, reader, pc):
        line, seg = reader.peek_line()
        if _is_blank(line):
            node.lines.append(seg.trim_left_space_width(4, reader.src))
            return _CONTINUE | _NO_CHILDREN
        pos, padding = _indent_position(line, reader.line_offset(), 4)
        if pos < 0:
            return _CLOSE
        self._line(node, reader, pos, padding)
        return _CONTINUE | _NO_CHILDREN

    def close(self, node, reader, pc):
        while node.lines and _is_blank(node.lines[-1].value(reader.src)):
            node.lines.pop()


class _ATXHeading(_BlockParser):
    triggers = "#"

    def open(self, parent, reader, pc):
        line, seg = reader.peek_line()
        pos = pc.block_offset
        if pos < 0:
            return None, _NO_CHILDREN
        i = pos
        while i < len(line) and line[i] == "#":
            i += 1
        level = i - pos
        if i == pos or level > 6:
            return None, _NO_CHILDREN
        if i == len(line):
            return _Node("heading", level=level), _NO_CHILDREN
        n = _trim_left_space_length(line[i:])
        if n == 0:
            return None, _NO_CHILDREN
        start = min(i + n, len(line) - 1)
        node = _Node("heading", level=level)
        hl = _Seg(seg.start + start - seg.padding, seg.start + len(line) - seg.padding).trim_right_space(reader.src)
        if hl.length() == 0:
            reader.advance_to_eol()
            return node, _NO_CHILDREN
        text = hl.value(reader.src)
        stop = len(text)
        if stop:
            i = stop - 1
            while text[i] == "#" and i > 0:
                i -= 1
            if i == 0 and text[0] == "#":
                reader.advance_to_eol()
                return node, _NO_CHILDREN
            if i != stop - 1 and text[i] in _SPACES:
                stop = i - _trim_right_space_length(text[:i])
        node.lines.append(_Seg(hl.start, hl.start + stop, hl.padding))
        reader.advance_to_eol()
        return node, _NO_CHILDREN


class _FencedCode(_BlockParser):
    triggers = "~`"

    def open(self, parent, reader, pc):
        line, seg = reader.peek_line()
        pos = pc.block_offset
        if pos < 0 or line[pos] not in "`~":
            return None, _NO_CHILDREN
        char = line[pos]
        i = pos
        while i < len(line) and line[i] == char:
            i += 1
        length = i - pos
        if length < 3:
            return None, _NO_CHILDREN
        info = None
        rest = line[i:]
        if _byte_len(rest) > 1:
            left, right = _trim_left_space_length(rest), _trim_right_space_length(rest)
            if left < len(rest) - right:
                info_start, info_stop = seg.start - seg.padding + i + left, seg.stop - right
                if char == "`" and "`" in rest[left:len(rest) - right]:
                    return None, _NO_CHILDREN
                if info_start != info_stop:
                    info = _Seg(info_start, info_stop)
        node = _Node("fenced", raw=True, info=info)
        pc.store["fence"] = (char, pos, length, node)
        return node, _NO_CHILDREN

    def cont(self, node, reader, pc):
        line, seg = reader.peek_line()
        char, indent, length, _ = pc.store["fence"]
        w, pos = _indent_width(line, reader.line_offset())
        if w < 4:
            i = pos
            while i < len(line) and line[i] == char:
                i += 1
            if i - pos >= length and _is_blank(line[i:]):
                reader.advance_to_eol()
                return _CLOSE
        pos, padding = _indent_position(line, reader.line_offset(), indent, seg.padding)
        if pos < 0:
            pos = max(0, _first_non_space(line)) - seg.padding
            padding = 0
        s = _Seg(seg.start + pos, seg.stop, padding)
        if padding:
            s = _preserve_leading_tab(s, reader, indent)
        node.lines.append(_Seg(s.start, s.stop, s.padding, True))
        reader.advance_to_eol()
        return _CONTINUE | _NO_CHILDREN

    def close(self, node, reader, pc):
        fence = pc.store.get("fence")
        if fence and fence[3] is node:
            pc.store["fence"] = None


def _blockquote_prefix(reader):
    line, _ = reader.peek_line()
    w, pos = _indent_width(line, reader.line_offset())
    if w > 3 or pos >= len(line) or line[pos] != ">":
        return False
    pos += 1
    if pos >= len(line) or line[pos] == "\n":
        reader.advance(pos)
        return True
    reader.advance(pos)
    if line[pos] in " \t":
        padding = _tab_width(reader.line_offset()) - 1 if line[pos] == "\t" else 0
        reader.advance_and_set_padding(1, padding)
    return True


class _Blockquote(_BlockParser):
    triggers = ">"

    def open(self, parent, reader, pc):
        if _blockquote_prefix(reader):
            return _Node("blockquote"), _HAS_CHILDREN
        return None, _NO_CHILDREN

    def cont(self, node, reader, pc):
        return _CONTINUE | _HAS_CHILDREN if _blockquote_prefix(reader) else _CLOSE


_BLOCK_TAGS = frozenset(
    "address article aside base basefont blockquote body caption center col colgroup dd details dialog dir div dl dt "
    "fieldset figcaption figure footer form frame frameset h1 h2 h3 h4 h5 h6 head header hr html iframe legend li link "
    "main menu menuitem meta nav noframes ol optgroup option p param search section summary table tbody td tfoot th "
    "thead title tr track ul".split())
_ATTRIBUTE = (r"(?:[\r\n \t]+[a-zA-Z_:][a-zA-Z0-9:._-]*(?:[\r\n \t]*=[\r\n \t]*"
              r"(?:[^\"'=<>`\x00-\x20]+|'[^']*'|\"[^\"]*\"))?)")
_HTML1_OPEN = re.compile(r"(?i)[ ]{0,3}<(script|pre|style|textarea)(?:[\t\n\f\r ].*|>.*|/>.*|)(?:\r\n|\n)?\Z")
_HTML1_CLOSE = re.compile(r"(?i)</(?:script|pre|style|textarea)>")
_HTML2_OPEN = re.compile(r"[ ]{0,3}<!--")
_HTML3_OPEN = re.compile(r"[ ]{0,3}<\?")
_HTML4_OPEN = re.compile(r"[ ]{0,3}<![A-Z]+.*(?:\r\n|\n)?\Z")
_HTML5_OPEN = re.compile(r"[ ]{0,3}<!\[CDATA\[")
_HTML6 = re.compile(r"[ ]{0,3}<(?:/[ ]*)?([a-zA-Z]+[a-zA-Z0-9\-]*)(?:[ ].*|>.*|/>.*|)(?:\r\n|\n)?\Z")
_HTML7 = re.compile(r"[ ]{0,3}<(/[ ]*)?([a-zA-Z]+[a-zA-Z0-9\-]*)(" + _ATTRIBUTE + r"*)[ ]*(?:>|/>)[ ]*(?:\r\n|\n)?\Z")
_HTML_CLOSURES = {2: "-->", 3: "?>", 4: ">", 5: "]]>"}


class _HTMLBlock(_BlockParser):
    triggers = "<"

    def open(self, parent, reader, pc):
        line, seg = reader.peek_line()
        last = pc.last_node()
        htype = None
        if _HTML1_OPEN.match(line):
            htype = 1
        elif _HTML2_OPEN.match(line):
            htype = 2
        elif _HTML3_OPEN.match(line):
            htype = 3
        elif _HTML4_OPEN.match(line):
            htype = 4
        elif _HTML5_OPEN.match(line):
            htype = 5
        else:
            m = _HTML7.match(line)
            if m:
                closing, has_attr, tag = m.group(1) == "/", m.group(3) != "", m.group(2).lower()
                if tag in _BLOCK_TAGS:
                    htype = 6
                elif tag not in ("script", "style", "pre") and not (last is not None and last.kind == "paragraph") \
                        and not (closing and has_attr):
                    htype = 7
        if htype is None:
            m = _HTML6.match(line)
            if m and m.group(1).lower() in _BLOCK_TAGS:
                htype = 6
        if htype is None:
            return None, _NO_CHILDREN
        reader.advance_to_eol()
        return _Node("html_block", raw=True, htype=htype, closure=None, lines=[seg]), _NO_CHILDREN

    def cont(self, node, reader, pc):
        line, seg = reader.peek_line()
        if node.htype == 1 or node.htype in _HTML_CLOSURES:
            found = (lambda s: _HTML1_CLOSE.search(s)) if node.htype == 1 else (lambda s: _HTML_CLOSURES[node.htype] in s)
            if len(node.lines) == 1 and found(node.lines[0].value(reader.src)):
                return _CLOSE
            if found(line):
                node.closure = seg
                reader.advance_to_eol()
                return _CLOSE
        elif _is_blank(line):
            return _CLOSE
        node.lines.append(seg)
        reader.advance_to_eol()
        return _CONTINUE | _NO_CHILDREN


class _Paragraph(_BlockParser):
    interrupts = False

    def open(self, parent, reader, pc):
        line, seg = reader.peek_line()
        if _is_blank(line):
            return None, _NO_CHILDREN
        node = _Node("paragraph", lines=[seg])
        reader.advance_to_eol()
        return node, _NO_CHILDREN

    def cont(self, node, reader, pc):
        line, seg = reader.peek_line()
        if _is_blank(line):
            return _CLOSE
        node.lines.append(seg)
        reader.advance_to_eol()
        return _CONTINUE | _NO_CHILDREN

    def close(self, node, reader, pc):
        lines = node.lines
        if lines:
            lines[:] = [s.trim_left_space(reader.src) for s in lines]
            lines[-1] = lines[-1].trim_right_space(reader.src)
        else:
            node.parent.remove_child(node)


_BLOCK_ORDER = [_Setext(), _ThematicBreak(), _List(), _ListItem(), _CodeBlock(), _ATXHeading(), _FencedCode(),
                _Blockquote(), _HTMLBlock(), _Paragraph()]
_FREE = [p for p in _BLOCK_ORDER if not p.triggers]
_BLOCK_PARSERS = {}
for _p in _BLOCK_ORDER:
    for _c in _p.triggers:
        _BLOCK_PARSERS.setdefault(_c, []).append(_p)
for _c in _BLOCK_PARSERS:
    _BLOCK_PARSERS[_c] += _FREE


# --- paragraph transformers: link reference definitions, then tables ---------------------------------------------------------

def _parse_link_destination(block):
    block.skip_spaces()
    line, _ = block.peek_line()
    line = line or ""
    if block.peek() == "<":
        i = 1
        while i < len(line):
            c = line[i]
            if c == "\\" and i < len(line) - 1 and line[i + 1] in _PUNCT:
                i += 2
                continue
            if c == ">":
                block.advance(i + 1)
                return line[1:i], True
            i += 1
        return None, False
    opened = i = 0
    while i < len(line):
        c = line[i]
        if c == "\\" and i < len(line) - 1 and line[i + 1] in _PUNCT:
            i += 2
            continue
        if c == "(":
            opened += 1
        elif c == ")":
            opened -= 1
            if opened < 0:
                break
        elif c in _SPACES:
            break
        i += 1
    block.advance(i)
    return line[:i], i != 0


def _parse_link_reference_definition(block, pc):
    fail = None, -1, -1
    block.skip_spaces()
    line, _ = block.peek_line()
    if line is None:
        return fail
    start_line, _ = block.position()
    width, pos = _indent_width(line, 0)
    if width > 3:
        return fail
    if width != 0:
        pos += 1
    if pos >= len(line) or line[pos] != "[":
        return fail
    block.advance(pos + 1)
    segs = block.find_closure("[", "]")
    if segs is None:
        return fail
    label = "".join(block.value(s) for s in segs)
    if _is_blank(label) or block.peek() != ":":
        return fail
    block.advance(1)
    block.skip_spaces()
    dest, ok = _parse_link_destination(block)
    if not ok:
        return fail
    line, _ = block.peek_line()
    is_new_line = line is None or _is_blank(line)
    end_line, _ = block.position()
    _, spaces, _ = block.skip_spaces()
    opener = block.peek()
    if opener not in ('"', "'", "("):
        if not is_new_line:
            return fail
        pc.add_reference(label, dest, None)
        return _Node("ref_def"), start_line, end_line + 1
    if spaces == 0:
        return fail
    block.advance(1)
    segs = block.find_closure(opener, ")" if opener == "(" else opener)
    if segs is None:
        if not is_new_line:
            return fail
        pc.add_reference(label, dest, None)
        block.advance_line()
        return _Node("ref_def"), start_line, end_line + 1
    title = "".join(block.value(s) for s in segs)
    line, _ = block.peek_line()
    if line is not None and not _is_blank(line):
        if not is_new_line:
            return fail
        pc.add_reference(label, dest, title)
        return _Node("ref_def"), start_line, end_line
    end_line, _ = block.position()
    pc.add_reference(label, dest, title)
    return _Node("ref_def"), start_line, end_line + 1


def _link_references(node, pc):
    lines = node.lines
    block = _BlockReader(pc.src, lines)
    removes = []
    while True:
        ref, start, end = _parse_link_reference_definition(block, pc)
        if start < 0:
            break
        if start == 0:
            ref.blank_prev = node.blank_prev
        node.parent.insert_before(node, ref)
        removes.append((start, end + 1 if start == end else end))
    offset = 0
    for start, end in removes:
        if not lines:
            break
        lines[start - offset:] = lines[end - offset:]
        offset = end
    if not lines:
        node.parent.remove_child(node)


_ALIGNS = [(re.compile(r"[\t\n\f\r ]*:-+[\t\n\f\r ]*\Z"), "left"), (re.compile(r"[\t\n\f\r ]*-+:[\t\n\f\r ]*\Z"), "right"),
           (re.compile(r"[\t\n\f\r ]*:-+:[\t\n\f\r ]*\Z"), "center"), (re.compile(r"[\t\n\f\r ]*-+[\t\n\f\r ]*\Z"), None)]


def _table_alignments(line):
    if _indent_width(line, 0)[0] > 3 or any(c not in _SPACES and c not in "-|:" for c in line) or set(line) == {"-"}:
        return None
    cols = line.split("|")
    if _is_blank(cols[0]):
        cols = cols[1:]
    if cols and _is_blank(cols[-1]):
        cols = cols[:-1]
    aligns = []
    for col in cols:
        for rx, align in _ALIGNS:
            if rx.match(col):
                aligns.append(align)
                break
        else:
            return None
    return aligns or None


def _table_row(seg, aligns, header, pc):
    src = pc.src
    seg = seg.trim_left_space(src).trim_right_space(src)
    line = seg.value(src)
    pos, limit = 0, len(line)
    row = _Node("table_row")
    if line and line[0] == "|":
        pos += 1
    if line and line[-1] == "|":
        limit -= 1
    i = 0
    while pos < limit:
        align = None
        if i >= len(aligns):
            if not header:
                return row
        else:
            align = aligns[i]
        cell = _Node("table_cell", align=align)
        backtick, closure, escapes = False, pos, []
        while closure < limit:
            if line[closure] == "`":
                backtick = True
            if line[closure] == "|":
                if closure == 0 or line[closure - 1] != "\\":
                    break
                if backtick:
                    escapes.append(seg.start + closure - 1)
            closure += 1
        if escapes:
            pc.store.setdefault("escaped-pipes", []).append((cell, escapes))
        cell.lines = [_Seg(seg.start + pos, seg.start + closure).trim_left_space(src).trim_right_space(src)]
        row.append(cell)
        pos = closure + 1
        i += 1
    while i < len(aligns):
        row.append(_Node("table_cell", align=None))
        i += 1
    return row


def _tables(node, pc):
    lines = node.lines
    for i in range(1, len(lines)):
        aligns = _table_alignments(lines[i].value(pc.src))
        if aligns is None:
            continue
        header = _table_row(lines[i - 1], aligns, True, pc)
        if len(aligns) != header.count():
            return
        table = _Node("table")
        thead = _Node("table_header")
        for cell in list(header.children()):
            thead.append(cell)
        table.append(thead)
        for line in lines[i + 1:]:
            table.append(_table_row(line, aligns, False, pc))
        del lines[i - 1:]
        node.parent.insert_after(node, table)
        if not lines:
            node.parent.remove_child(node)
        else:
            last = lines[i - 2]
            lines[i - 2] = _Seg(last.start, last.stop - 1, last.padding, last.nl)
        return


def _transform_paragraph(node, pc):
    for transform in (_link_references, _tables):
        transform(node, pc)
        if node.parent is None:
            return True
    return False


# --- the block pass (parser.parseBlocks) ------------------------------------------------------------------------------------

def _close_blocks(frm, to, reader, pc):
    blocks = pc.opened
    for i in range(frm, to - 1, -1):
        node, bp = blocks[i]
        if node.kind == "paragraph" and node.parent is not None:
            _transform_paragraph(node, pc)
        if node.parent is not None:
            bp.close(node, reader, pc)
    pc.opened = blocks[:to] if frm == len(blocks) - 1 else blocks[:to] + blocks[frm + 1:]


def _open_blocks(parent, blank_line, reader, pc):
    result = _NO_BLOCKS
    last_block = pc.opened[-1] if pc.opened else None
    continuable = last_block is not None and last_block[0].kind == "paragraph"
    while True:
        line, _ = reader.peek_line()
        w, pos = _indent_width(line or "", reader.line_offset())
        pc.block_offset = -1 if w >= len(line or "") else pos
        if line is None or line[0] == "\n":
            break
        bps = _BLOCK_PARSERS.get(line[pos], _FREE) if pos < len(line) else _FREE
        retry = False
        for bp in bps:
            if continuable and result == _NO_BLOCKS and not bp.interrupts:
                continue
            if w > 3 and not bp.indented:
                continue
            last_block = pc.opened[-1] if pc.opened else None
            last = last_block[0] if last_block else None
            node, state = bp.open(parent, reader, pc)
            if node is None:
                continue
            if state & _REQUIRE_PARAGRAPH and last is parent.last:
                last_block[1].close(last, reader, pc)
                pc.opened.pop()
                if _transform_paragraph(last, pc):
                    continuable = False
                    retry = True
                    break
            node.blank_prev = blank_line
            if last is not None and last.parent is None:
                _close_blocks(len(pc.opened) - 1, len(pc.opened) - 1, reader, pc)
            parent.append(node)
            result = _NEW_BLOCKS
            pc.opened.append((node, bp))
            if state & _HAS_CHILDREN:
                parent = node
                retry = True
            break
        if not retry:
            break
    if result == _NO_BLOCKS and continuable:
        if last_block[1].cont(last_block[0], reader, pc) & _CONTINUE:
            result = _PARA_CONTINUATION
    return result


def _is_blank_line(line_num, level, stats):
    if not stats:
        return True
    for i in range(len(stats) - 1 - level, -1, -1):
        num, lvl, blank = stats[i]
        if num == line_num and lvl <= level:
            return blank
        if num < line_num:
            break
    return False


def _parse_blocks(root, reader, pc):
    while True:
        if not reader.skip_blank_lines()[2]:
            return
        if _open_blocks(root, True, reader, pc) != _NEW_BLOCKS:
            return
        reader.advance_line()
        stats = []
        while pc.opened:
            opened = list(pc.opened)
            last_index = len(opened) - 1
            for i, (node, bp) in enumerate(opened):
                line, _ = reader.peek_line()
                if line is None:
                    _close_blocks(last_index, 0, reader, pc)
                    reader.advance_line()
                    return
                line_num = reader.line
                stats.append((line_num, i, _is_blank(line)))
                if node.kind != "paragraph":
                    state = bp.cont(node, reader, pc)
                    if state & _CONTINUE:
                        if state & _HAS_CHILDREN and i == last_index:
                            _open_blocks(node, _is_blank_line(line_num - 1, i + 1, stats), reader, pc)
                            break
                        continue
                blank = _is_blank_line(line_num - 1, i, stats)
                last_node = opened[last_index][0]
                if _open_blocks(root if i == 0 else opened[i - 1][0], blank, reader, pc) != _PARA_CONTINUATION:
                    if last_index < len(pc.opened) and pc.opened[last_index][0] is not last_node:
                        last_index -= 1
                    _close_blocks(last_index, i, reader, pc)
                break
            reader.advance_line()


# --- inline parsers --------------------------------------------------------------------------------------------------------

def _scan_delimiter(line, before, chars):
    c = line[0]
    if c not in chars:
        return None
    j = 1
    while j < len(line) and line[j] == c:
        j += 1
    after = " " if j == len(line) else line[j]
    bp, bw, ap, aw = _is_punct_rune(before), _is_space_rune(before), _is_punct_rune(after), _is_space_rune(after)
    left = not aw and (not ap or bw or bp)
    right = not bw and (not bp or aw or ap)
    if c == "_":
        can_open, can_close = left and (not right or bp), right and (not left or ap)
    else:
        can_open, can_close = left, right
    return _Node("delimiter", can_open=can_open, can_close=can_close, length=j, orig=j, char=c, prev_d=None, next_d=None)


def _push_delimiter(pc, d):
    if pc.first_delim is None:
        pc.first_delim = pc.last_delim = d
    else:
        pc.last_delim.next_d = d
        d.prev_d = pc.last_delim
        pc.last_delim = d


def _remove_delimiter(pc, d):
    if d.prev_d is None:
        pc.first_delim = d.next_d
    else:
        d.prev_d.next_d = d.next_d
        if d.next_d is not None:
            d.next_d.prev_d = d.prev_d
    if d.next_d is None:
        pc.last_delim = d.prev_d
    if pc.first_delim is not None:
        pc.first_delim.prev_d = None
    if pc.last_delim is not None:
        pc.last_delim.next_d = None
    d.next_d = d.prev_d = None
    if d.length:
        _merge_or_replace_text(d.parent, d, d.seg)
    else:
        d.parent.remove_child(d)


def _clear_delimiters(bottom, pc):
    c = pc.last_delim
    while c is not None and c is not bottom:
        prev = c.prev
        if c.kind == "delimiter":
            _remove_delimiter(pc, c)
        c = prev


def _process_delimiters(bottom, pc):
    last = pc.last_delim
    if last is None:
        return
    closer = None
    if bottom is None:
        closer = pc.first_delim
    elif bottom is not last:
        c = last.prev
        while c is not None and c is not bottom:
            if c.kind == "delimiter":
                closer = c
            c = c.prev
    if closer is None:
        _clear_delimiters(bottom, pc)
        return
    while closer is not None:
        if not closer.can_close:
            closer = closer.next_d
            continue
        consume, found, maybe = 0, False, False
        opener = closer.prev_d
        while opener is not None and opener is not bottom:
            if opener.can_open and opener.char == closer.char:
                maybe = True
                if (opener.can_close or closer.can_open) and (opener.orig + closer.orig) % 3 == 0 and closer.orig % 3:
                    consume = 0
                else:
                    consume = 2 if opener.length >= 2 and closer.length >= 2 else 1
                if consume:
                    found = True
                    break
            opener = opener.prev_d
        if not found:
            nxt = closer.next_d
            if not maybe and not closer.can_open:
                _remove_delimiter(pc, closer)
            closer = nxt
            continue
        for d in (opener, closer):
            d.length -= consume
            d.seg = d.seg.with_stop(d.seg.start + d.length)
        node = _Node("strikethrough") if opener.char == "~" else _Node("emphasis", level=consume)
        parent = opener.parent
        child = opener.next
        while child is not None and child is not closer:
            nxt = child.next
            node.append(child)
            child = nxt
        parent.insert_after(opener, node)
        c = opener.next_d
        while c is not None and c is not closer:
            nxt = c.next_d
            _remove_delimiter(pc, c)
            c = nxt
        if opener.length == 0:
            _remove_delimiter(pc, opener)
        if closer.length == 0:
            nxt = closer.next_d
            _remove_delimiter(pc, closer)
            closer = nxt
    _clear_delimiters(bottom, pc)


def _parse_emphasis(parent, block, pc):
    before = block.preceding_char()
    line, seg = block.peek_line()
    node = _scan_delimiter(line, before, "*_")
    if node is None:
        return None
    node.seg = seg.with_stop(seg.start + node.orig)
    block.advance(node.orig)
    _push_delimiter(pc, node)
    return node


def _parse_strikethrough(parent, block, pc):
    before = block.preceding_char()
    line, seg = block.peek_line()
    node = _scan_delimiter(line, before, "~")
    if node is None or node.orig > 2 or before == "~":
        return None
    node.seg = seg.with_stop(seg.start + node.orig)
    block.advance(node.orig)
    _push_delimiter(pc, node)
    return node


def _parse_code_span(parent, block, pc):
    line, start_seg = block.peek_line()
    opener = len(line) - len(line.lstrip("`"))
    block.advance(opener)
    saved = block.position()
    node = _Node("code_span")
    while True:
        line, seg = block.peek_line()
        if line is None:
            block.set_position(*saved)
            return _text(start_seg.with_stop(start_seg.start + opener))
        i = 0
        closed = False
        while i < len(line):
            if line[i] == "`":
                run = i
                while i < len(line) and line[i] == "`":
                    i += 1
                if i - run == opener:
                    seg = seg.with_stop(seg.start + i - opener)
                    if not seg.is_empty():
                        node.append(_text(seg, raw=True))
                    block.advance(i)
                    closed = True
                    break
            i += 1
        if closed:
            break
        node.append(_text(seg, raw=True))
        block.advance_line()
    src = block.src
    if not all(_is_blank(c.seg.value(src)) for c in node.children()):
        first, last = node.first.seg, node.last.seg
        if not first.is_empty() and src[first.start] in " \n" and not last.is_empty() and src[last.stop - 1] in " \n":
            node.first.seg = first.with_start(first.start + 1)
            last = node.last.seg
            node.last.seg = last.with_stop(last.stop - 1)
    return node


def _parse_autolink(parent, block, pc):
    line, seg = block.peek_line()
    typ, stop = "email", _find_email_index(line[1:])
    if stop < 0:
        typ, stop = "url", _find_url_index(line[1:])
    if stop < 0:
        return None
    stop += 1
    if stop >= len(line) or line[stop] != ">":
        return None
    block.advance(stop + 1)
    return _Node("autolink", typ=typ, value=_Seg(seg.start + 1, seg.start + stop), protocol=None)


_TAGNAME = r"([A-Za-z][A-Za-z0-9-]*)"
_SPACE_OR_NEWLINE = r"(?:[ \t]|(?:\r\n|\n){0,1})"
_OPEN_TAG = re.compile("<" + _TAGNAME + _ATTRIBUTE + "*" + _SPACE_OR_NEWLINE + "*/?>")
_CLOSE_TAG = re.compile("</" + _TAGNAME + _SPACE_OR_NEWLINE + "*>")


def _raw_html_until(block, closer, offset=0):
    saved = block.position()
    while True:
        line, _ = block.peek_line()
        if line is None:
            break
        index = line.find(closer, offset)
        if index > -1:
            block.advance(index + len(closer))
            return _Node("raw_html")
        offset = 0
        block.advance_line()
    block.set_position(*saved)
    return None


def _parse_raw_html(parent, block, pc):
    line, _ = block.peek_line()
    if len(line) > 1 and line[1] in _ALNUM or len(line) > 2 and line[1] == "/" and line[2] in _ALNUM:
        return _Node("raw_html") if block.match(_CLOSE_TAG if line[1] == "/" else _OPEN_TAG) else None
    if line.startswith("<!--"):
        for empty in ("<!-->", "<!--->"):
            if line.startswith(empty):
                block.advance(len(empty))
                return _Node("raw_html")
        return _raw_html_until(block, "-->", 4)
    if line.startswith("<?"):
        return _raw_html_until(block, "?>")
    if len(line) > 2 and line[1] == "!" and "A" <= line[2] <= "Z":
        return _raw_html_until(block, ">")
    if line.startswith("<![CDATA["):
        return _raw_html_until(block, "]]>")
    return None


def _push_link_bottom(pc):
    pc.store.setdefault("bottoms", []).append(pc.last_delim)


def _pop_link_bottom(pc):
    bottoms = pc.store.get("bottoms")
    return bottoms.pop() if bottoms else None


def _push_label(pc, v):
    head = pc.store.get("labels")
    if head is None:
        v.first = v.last_s = v
        pc.store["labels"] = v
    else:
        tail = head.last_s
        head.last_s = v
        tail.next_s = v
        v.prev_s = tail


def _remove_label(pc, d):
    head = pc.store.get("labels")
    if head is None:
        return
    if d.prev_s is None:
        head = d.next_s
        if head is not None:
            head.first, head.last_s, head.prev_s = d, d.last_s, None
        pc.store["labels"] = head
    else:
        d.prev_s.next_s = d.next_s
        if d.next_s is not None:
            d.next_s.prev_s = d.prev_s
    if head is not None and d.next_s is None:
        head.last_s = d.prev_s
    d.next_s = d.prev_s = d.first = d.last_s = None


def _label_length(v, src):
    if v is None or v.last_s is None or v.first is None:
        return 0
    return _byte_len(src[v.first.seg.start:v.last_s.seg.stop])


def _contains_link(n):
    while n is not None:
        if n.kind == "link" or _contains_link(n.first):
            return True
        n = n.next
    return False


def _process_link_label(parent, link, last, pc):
    _process_delimiters(_pop_link_bottom(pc), pc)
    c = last.next
    while c is not None:
        nxt = c.next
        parent.remove_child(c)
        link.append(c)
        c = nxt


def _parse_link_title(block):
    block.skip_spaces()
    opener = block.peek()
    if opener not in ('"', "'", "("):
        return None, False
    block.advance(1)
    segs = block.find_closure(opener, ")" if opener == "(" else opener)
    if segs is None:
        return None, False
    return "".join(block.value(s) for s in segs), True


def _parse_inline_link(parent, last, block, pc):
    block.advance(1)
    block.skip_spaces()
    dest = title = None
    if block.peek() == ")":
        block.advance(1)
    else:
        dest, ok = _parse_link_destination(block)
        if not ok:
            return None
        block.skip_spaces()
        if block.peek() == ")":
            block.advance(1)
        else:
            title, ok = _parse_link_title(block)
            if not ok:
                return None
            block.skip_spaces()
            if block.peek() != ")":
                return None
            block.advance(1)
    link = _Node("link", dest=dest, title=title)
    _process_link_label(parent, link, last, pc)
    return link


def _parse_reference_link(parent, last, block, pc):
    _, orgpos = block.position()
    block.advance(1)
    segs = block.find_closure("[", "]")
    if segs is None:
        return None, False
    ref = "".join(block.value(s) for s in segs)
    if _is_blank(ref):
        ref = block.value(_Seg(last.seg.stop, orgpos.start - 1))
    if _byte_len(ref) > 999:
        return None, True
    found = pc.refs.get(_to_link_reference(ref))
    if found is None:
        return None, True
    link = _Node("link", dest=found[0], title=found[1])
    _process_link_label(parent, link, last, pc)
    return link, True


def _parse_link(parent, block, pc):
    line, seg = block.peek_line()
    if line[0] in "![":
        if line[0] == "!":
            if len(line) < 2 or line[1] != "[":
                return None
            block.advance(1)
        _push_link_bottom(pc)
        image = line[0] == "!"
        pos = seg.start + 1 if image else seg.start
        state = _Node("label", seg=_Seg(pos - 1 if image else pos, pos + 1), image=image, prev_s=None, next_s=None,
                      first=None, last_s=None)
        _push_label(pc, state)
        block.advance(1)
        return state
    head = pc.store.get("labels")
    if head is None:
        return None
    last = head.last_s
    if last is None:
        _pop_link_bottom(pc)
        return None
    block.advance(1)
    _remove_label(pc, last)

    def give_up():
        _merge_or_replace_text(last.parent, last, last.seg)
        _pop_link_bottom(pc)

    if _label_length(head, pc.src) > 998 or not last.image and _contains_link(last):
        return give_up()
    c = block.peek()
    saved = block.position()
    link = None
    if c == "(":
        link = _parse_inline_link(parent, last, block, pc)
    elif c == "[":
        link, has_value = _parse_reference_link(parent, last, block, pc)
        if link is None and has_value:
            return give_up()
    if link is None:
        block.set_position(*saved)
        ref = block.value(_Seg(last.seg.stop, seg.start))
        found = pc.refs.get(_to_link_reference(ref)) if _byte_len(ref) <= 999 else None
        if found is None:
            return give_up()
        link = _Node("link", dest=found[0], title=found[1])
        _process_link_label(parent, link, last, pc)
    last.parent.remove_child(last)
    if not last.image:
        return link
    image = _Node("image", dest=link.dest, title=link.title)
    for child in list(link.children()):
        image.append(child)
    return image


_WWW = re.compile(r"www\.[-a-zA-Z0-9@:%._+~#=]{1,256}\.[a-z]+(?:[/#?][-a-zA-Z0-9@:%_+.~#!?&/=();,'\">^{}\[\]`]*)?")
_URL = re.compile(r"(?:http|https|ftp)://[-a-zA-Z0-9@:%._+~#=]{1,256}\.[a-z]+(?::[0-9]+)?"
                  r"(?:[/#?][-a-zA-Z0-9@:%_+.~#$!?&/=();,'\">^{}\[\]`]*)?")


def _parse_linkify(parent, block, pc):
    if pc.store.get("labels") is not None:
        return None
    line, seg = block.peek_line()
    consumes, start = 0, seg.start
    if line[0] in " *_~(":
        consumes, start, line = 1, start + 1, line[1:]
    m, protocol, typ = None, None, "url"
    if line.startswith(("http:", "https:", "ftp:")):
        found = _URL.match(line)
        m = [0, found.end()] if found else None
    if m is None and line.startswith("www."):
        found = _WWW.match(line)
        m = [0, found.end()] if found else None
        protocol = "http"
    if m is None:
        if line and line[0] in _PUNCT:
            return None
        typ = "email"
        stop = _find_email_index(line)
        if stop < 0:
            return None
        m = [0, stop, line.find("@"), stop - 1]
        if "." not in line[m[2]:m[3]]:
            return None
        if line[m[1] - 1] == ".":
            m[1] -= 1
        if m[1] < len(line) and line[m[1]] in "-_":
            return None
    if consumes:
        _merge_or_append_text(parent, seg.with_stop(seg.start + 1))
    i = m[1] - 1
    while i > 0 and line[i] in "?!.,:*_~":
        i -= 1
    i += 1
    if line[i - 1] == ")":
        i -= max(0, line[:i].count(")") - line[:i].count("("))
    elif line[i - 1] == ";":
        j = i - 2
        while j >= m[0] and line[j] in _ALNUM:
            j -= 1
        if j != i - 2 and line[j] == "&":
            i = j
    block.advance(consumes + i)
    return _Node("autolink", typ=typ, value=_Seg(start, start + i), protocol=protocol)


_INLINE_PARSERS = {"`": [_parse_code_span], "!": [_parse_link], "[": [_parse_link], "]": [_parse_link],
                   "<": [_parse_autolink, _parse_raw_html], "*": [_parse_emphasis, _parse_linkify],
                   "_": [_parse_emphasis, _parse_linkify], "~": [_parse_strikethrough, _parse_linkify],
                   " ": [_parse_linkify], "(": [_parse_linkify]}


def _close_inline_block(parent, block, pc):
    """The link parser's CloseBlock: an unclosed bracket is text."""
    pc.store["bottoms"] = []
    s = pc.store.get("labels")
    while s is not None:
        nxt = s.next_s
        _remove_label(pc, s)
        s.parent.replace_child(s, _text(s.seg))
        s = nxt


def _parse_inlines(parent, pc):
    """parser.parseBlock: a block's lines into inline nodes."""
    escaped = False
    src = pc.src
    block = _BlockReader(src, parent.lines)
    while True:
        line, _ = block.peek_line()
        if line is None:
            break
        n = len(line)
        flags = 0
        nl = line[-1] == "\n"
        if nl and ((n >= 3 and line[n - 2] == "\\" and line[n - 3] != "\\") or (n == 2 and line[0] == "\\")):
            n -= 2
            flags = _HARD | _VISIBLE
        elif nl and ((n >= 4 and line[n - 3] == "\\" and line[n - 2] == "\r" and line[n - 4] != "\\")
                     or (n == 3 and line[0] == "\\" and line[1] == "\r")):
            n -= 3
            flags = _HARD | _VISIBLE
        elif nl and n >= 3 and line[n - 3] == " " and line[n - 2] == " ":
            n -= 3
            flags = _HARD
        elif nl and n >= 4 and line[n - 4] == " " and line[n - 3] == " " and line[n - 2] == "\r":
            n -= 4
            flags = _HARD
        elif nl:
            flags = _SOFT
        line_no, start = block.position()
        advance = 0
        restart = False
        for i in range(n):
            c = line[i]
            if c == "\n":
                break
            space, punct = c in " \t", c in _PUNCT
            if (punct and not escaped) or space or i == 0:
                parsers = _INLINE_PARSERS.get(" " if space or (i == 0 and not punct) else c)
                if parsers:
                    block.advance(advance)
                    advance = 0
                    saved = block.position()
                    if i != 0:
                        _merge_or_append_text(parent, start.between(saved[1]))
                        start = saved[1]
                    node = None
                    for parse in parsers:
                        node = parse(parent, block, pc)
                        if node is not None:
                            break
                        block.set_position(*saved)
                    if node is not None:
                        parent.append(node)
                        restart = True
                        break
            if escaped:
                escaped = False
            elif c == "\\":
                escaped = True
            advance += 1
        if restart:
            continue
        if advance:
            block.advance(advance)
        cur_line, cur = block.position()
        if cur_line != line_no:
            continue
        diff = start.between(cur)
        text = _text(diff if flags & (_HARD | _VISIBLE) == _HARD | _VISIBLE else diff.trim_right_space(src))
        text.soft, text.hard = bool(flags & _SOFT), bool(flags & _HARD)
        parent.append(text)
        block.advance_line()
    _process_delimiters(None, pc)
    _close_inline_block(parent, block, pc)


# --- AST transformers and the renderer ---------------------------------------------------------------------------------------

def _walk(root):
    """(node, entering) in document order, without recursion: a body can nest deeper than Python's stack."""
    stack = [(root, True)]
    while stack:
        node, entering = stack.pop()
        yield node, entering
        if entering:
            stack.append((node, False))
            stack.extend((c, True) for c in reversed(list(node.children())))


def _autolink_url(node, src):
    label = node.value.value(src)
    return node.protocol + "://" + label if node.protocol else label


def _safe_links(doc, src):
    drop = []
    for node, entering in _walk(doc):
        if not entering:
            continue
        if node.kind == "link" and not allowed_url(_url_escape(node.dest or "", True)):
            drop.append(node)
        elif node.kind == "autolink":
            url = _autolink_url(node, src)
            if node.typ == "email" and not url.lower().startswith("mailto:"):
                url = "mailto:" + url
            if not allowed_url(url):
                drop.append(node)
        elif node.kind == "image":
            drop.append(node)
    for node in drop:
        parent = node.parent
        if parent is None:
            continue
        if node.kind == "autolink":
            parent.replace_child(node, _Node("string", value=node.value.value(src)))
            continue
        c = node.first
        while c is not None:
            nxt = c.next
            parent.insert_before(node, c)
            c = nxt
        parent.remove_child(node)


def _escaped_pipes(pc):
    """The table extension's AST transformer: `\\|` in a cell's code span is a pipe."""
    for cell, positions in pc.store.pop("escaped-pipes", []):
        for span, entering in list(_walk(cell)):
            if not entering or span.kind != "code_span":
                continue
            for c in list(span.children()):
                if c.kind != "text":
                    continue
                n, orig = c, c.seg
                for pos in positions:
                    if orig.start <= pos < orig.stop:
                        n1, n2 = _text(n.seg.with_stop(pos), raw=True), _text(n.seg.with_start(pos + 1), raw=True)
                        span.insert_after(n, n1)
                        span.insert_after(n1, n2)
                        span.remove_child(n)
                        n = n2


def _render(doc, src):
    out = []
    w = out.append
    stack = [(doc, True)]
    while stack:
        n, entering = stack.pop()
        k = n.kind
        skip = False
        if k == "text":
            if entering:
                value = n.seg.value(src)
                if n.raw:
                    w(_escape_html(value))
                else:
                    _write(out, value)
                    if n.hard:
                        w("<br>\n")
                    elif n.soft:
                        w("\n")
        elif k == "string":
            if entering:
                _write(out, n.value)
        elif k == "paragraph":
            w("<p>" if entering else "</p>\n")
        elif k == "text_block":
            if not entering and n.next is not None and n.first is not None:
                w("\n")
        elif k == "heading":
            w(f"<h{n.level}>" if entering else f"</h{n.level}>\n")
        elif k in ("emphasis", "strikethrough"):
            tag = "del" if k == "strikethrough" else "strong" if n.level == 2 else "em"
            w(f"<{tag}>" if entering else f"</{tag}>")
        elif k == "code_span":
            if entering:
                w("<code>")
                for c in n.children():
                    value = c.seg.value(src)
                    w(_escape_html(value[:-1]) + " " if value.endswith("\n") else _escape_html(value))
                skip = True
            else:
                w("</code>")
        elif k == "link":
            if entering:
                dest = _url_escape(n.dest or "", True)
                w('<a href="' + ("" if _is_dangerous_url(dest) else _escape_html(dest)) + '"')
                if n.title is not None:
                    w(' title="')
                    _write(out, n.title)
                    w('"')
                w(">")
            else:
                w("</a>")
        elif k == "autolink":
            if entering:
                url = _url_escape(_autolink_url(n, src), False)
                w('<a href="')
                if n.typ == "email" and not url.lower().startswith("mailto:"):
                    w("mailto:")
                if not _is_dangerous_url(url):
                    w(_escape_html(url))
                w('">' + _escape_html(n.value.value(src)) + "</a>")
        elif k == "raw_html":
            if entering:
                w("<!-- raw HTML omitted -->")
            skip = True
        elif k == "list":
            tag = "ol" if n.marker in ".)" else "ul"
            if entering:
                w(f'<ol start="{n.start}">\n' if tag == "ol" and n.start != 1 else f"<{tag}>\n")
            else:
                w(f"</{tag}>\n")
        elif k == "list_item":
            if entering:
                w("<li>" if n.first is None or n.first.kind == "text_block" else "<li>\n")
            else:
                w("</li>\n")
        elif k == "blockquote":
            w("<blockquote>\n" if entering else "</blockquote>\n")
        elif k == "hr":
            if entering:
                w("<hr>\n")
        elif k in ("code_block", "fenced"):
            if entering:
                w("<pre><code")
                if k == "fenced" and n.info is not None:
                    w(' class="language-')
                    _write(out, n.info.value(src).partition(" ")[0])
                    w('"')
                w(">" + "".join(_escape_html(line.value(src)) for line in n.lines))
            else:
                w("</code></pre>\n")
        elif k == "html_block":
            if entering or n.closure is not None:
                w("<!-- raw HTML omitted -->\n")
        elif k == "ref_def":
            skip = True
        elif k == "table":
            w("<table>\n" if entering else "</table>\n")
        elif k == "table_header":
            if entering:
                w("<thead>\n<tr>\n")
            else:
                w("</tr>\n</thead>\n" + ("<tbody>\n" if n.next is not None else ""))
        elif k == "table_row":
            if entering:
                w("<tr>\n")
            else:
                w("</tr>\n" + ("</tbody>\n" if n.parent.last is n else ""))
        elif k == "table_cell":
            tag = "th" if n.parent.kind == "table_header" else "td"
            if entering:
                w(f'<{tag} align="{n.align}">' if n.align else f"<{tag}>")
            else:
                w(f"</{tag}>\n")
        if entering:
            stack.append((n, False))
            if not skip:
                stack.extend((c, True) for c in reversed(list(n.children())))
    return "".join(out)


def _convert(text):
    text = re.sub("[\ud800-\udfff]", "\ufffd", text)  # what Go's JSON decoding left of a lone surrogate
    pc = _Ctx(text)
    doc = _Node("document")
    _parse_blocks(doc, _Reader(text), pc)
    blocks = [n for n, entering in _walk(doc) if not entering and n.kind not in ("text", "ref_def")]
    for node in blocks:  # children before their parent, as parser.walkBlock
        if not getattr(node, "raw", False) and node.lines:
            _parse_inlines(node, pc)
        else:
            _close_inline_block(node, None, pc)
    _safe_links(doc, text)
    _escaped_pipes(pc)
    return _render(doc, text)


_rendered = {}
_lock = threading.Lock()


def render(text: str) -> str:
    """An item's body as HTML. The last bodies are kept: a page is built on every state change and every minute, and its bodies
    rarely change."""
    text = text or ""
    with _lock:
        html = _rendered.get(text)
        if html is None:
            if len(_rendered) >= 64:
                _rendered.clear()
            html = _rendered[text] = _convert(text)
        return html
