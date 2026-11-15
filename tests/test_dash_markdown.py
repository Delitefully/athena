"""The dash's Markdown, against the old Go dash's (dash/markdown_test.go, one test for each of its), and against goldmark's own
output: every golden below is what the old dash binary served for that body (its goldmark v1.8.6, as dash/markdown.go
configures it), byte for byte. The bodies are as the dash keeps them, the blank lines around them already trimmed."""
import re

from harness import Case

from athena.caps.dash import markdown

# Text may say anything, escaped: what must never appear is a tag, an event handler or a URL in an attribute.
BAD = re.compile(r'(?i)<script|<img|<iframe|\son\w+=|(href|src)="\s*(javascript|vbscript|data|file|ftp)|src=|<a href="[^"]*evil')


class Markdown(Case):
    def test_a_body_cannot_inject_markup_or_script(self):
        """Nothing in a body can put markup, a script or a link to anything but http, https, mailto or a relative URL on the page."""
        vectors = [
            "<script>alert(1)</script>",
            "<img src=x onerror=alert(1)>",
            '<a href="javascript:alert(1)">x</a>',
            "<iframe src=//evil.test></iframe>",
            "[x](javascript:alert(1))",
            "[x](JaVaScRiPt:alert(1))",
            "[x](  javascript:alert(1))",
            "[x](&#106;avascript:alert(1))",
            "[x](java&#x09;script:alert(1))",
            "[x](data:text/html;base64,PHNjcmlwdD4=)",
            "[x](vbscript:msgbox(1))",
            "[x](file:///etc/passwd)",
            "[x][r]\n\n[r]: javascript:alert(1)",
            "<javascript:alert(1)>",
            "<JaVaScRiPt:alert(1)>",
            "![a](javascript:alert(1))",
            "![a](data:image/svg+xml;base64,PHN2Zz4=)",
            "![a](https://evil.test/pixel.png)",
            '[x](https://ok.test "t\\"><script>alert(1)</script>")',
            "| a | b |\n|---|---|\n| <script>x</script> | [y](javascript:alert(1)) |",
            "`<script>`",
            "```html\n<script>alert(1)</script>\n```",
        ]
        for v in vectors:
            with self.subTest(body=v):
                out = markdown.render(v)
                self.assertIsNone(BAD.search(out), out)

    def test_only_http_mailto_and_relative_links(self):
        """The scheme allowlist is checked on top of goldmark's own dangerous-URL check, so it also refuses what goldmark lets by."""
        for raw, want in {
            "https://github.com/acme/web/pull/7": True,
            "http://127.0.0.1:2843/": True,
            "HTTPS://Example.test/x": True,
            "mailto:dev@acme.test": True,
            "/preview/a1/index.html": True,
            "notes.html#top": True,
            "JaVaScRiPt:alert(1)": False,
            "javascript:alert(1)": False,
            "ftp://files.acme.test/x": False,
            "chrome://settings": False,
            "tel:+15555550100": False,
            "\x01javascript:alert(1)": False,
            "data:text/plain,hi": False,
        }.items():
            self.assertEqual(markdown.allowed_url(raw), want, raw)
        # goldmark itself lets ftp: by; the allowlist keeps the text and drops the link.
        out = markdown.render("get [the file](ftp://files.acme.test/x) and [this](JaVaScRiPt:alert(1)) <ftp://files.acme.test/y>")
        self.assertNotIn("<a", out)
        for want in ("the file", "this", "ftp://files.acme.test/y"):
            self.assertIn(want, out)

    def test_a_body_renders_paragraphs_lists_tables_and_links(self):
        # A column's alignment is an attribute: the page's CSP refuses inline styles.
        out = markdown.render("**Rec: B.** Fewer hosts.\n\nSecond paragraph with `code`.\n\n"
                              "- one\n- two\n\n1. first\n2. second\n\n"
                              "| Option | Cost |\n|---|--:|\n| A | 2 d |\n| B | 1 d |\n\n"
                              "See [the PR](https://github.com/acme/web/pull/7), https://acme.test/x and <mailto:dev@acme.test>.")
        for want in (
            "<p><strong>Rec: B.</strong> Fewer hosts.</p>",
            "<p>Second paragraph with <code>code</code>.</p>",
            "<ul>\n<li>one</li>\n<li>two</li>\n</ul>",
            "<ol>\n<li>first</li>",
            "<table>", "<th>Option</th>", '<td align="right">2 d</td>', "<td>B</td>",
            '<a href="https://github.com/acme/web/pull/7">the PR</a>',
            '<a href="https://acme.test/x">https://acme.test/x</a>',
            '<a href="mailto:dev@acme.test">mailto:dev@acme.test</a>',
        ):
            self.assertIn(want, out)

    def test_the_allowlist_sees_entity_encoded_schemes(self):
        """goldmark decodes entities in a destination when it renders the link, so the allowlist checks the decoded form:
        "ftp&#58;//" is ftp://, and a data: image is refused even though goldmark itself would let one by."""
        for v in (
            "[a](ftp&#58;//x.test/a)",
            "[a](vscode&#58;//file/x)",
            "[a](vscode&colon;//file/x)",
            "[a](tel&#x3a;+15555550100)",
            "[a](data&#58;image/png;base64,AAAA)",
            "[a][r]\n\n[r]: vscode&#58;//x",
            "[a][r]\n\n[r]: data&#58;image/png;base64,AAAA",
        ):
            self.assertNotIn("<a", markdown.render(v), v)
        self.assertIn('<a href="https://acme.test/x">a</a>', markdown.render("[a](https&#58;//acme.test/x)"))

    def test_bodies_render_as_the_old_dash_rendered_them(self):
        for body, html in BRIEFS + SPEC:
            with self.subTest(body=body):
                self.assertEqual(markdown.render(body), html)

    def test_a_body_is_rendered_once(self):
        body = "a **cached** body"
        self.assertIs(markdown.render(body), markdown.render(body))


# What the old dash served: briefs as an LLM writes them, the Go tests' vectors, and the corners of each construct.
BRIEFS = [
    ('<script>alert(1)</script>',
     '<!-- raw HTML omitted -->\n'),
    ('<img src=x onerror=alert(1)>',
     '<!-- raw HTML omitted -->\n'),
    ('<a href="javascript:alert(1)">x</a>',
     '<p><!-- raw HTML omitted -->x<!-- raw HTML omitted --></p>\n'),
    ('<iframe src=//evil.test></iframe>',
     '<!-- raw HTML omitted -->\n'),
    ('[x](javascript:alert(1))',
     '<p>x</p>\n'),
    ('[x](JaVaScRiPt:alert(1))',
     '<p>x</p>\n'),
    ('[x](  javascript:alert(1))',
     '<p>x</p>\n'),
    ('[x](&#106;avascript:alert(1))',
     '<p>x</p>\n'),
    ('[x](java&#x09;script:alert(1))',
     '<p>x</p>\n'),
    ('[x](data:text/html;base64,PHNjcmlwdD4=)',
     '<p>x</p>\n'),
    ('[x](vbscript:msgbox(1))',
     '<p>x</p>\n'),
    ('[x](file:///etc/passwd)',
     '<p>x</p>\n'),
    ('[x][r]\n\n[r]: javascript:alert(1)',
     '<p>x</p>\n'),
    ('<javascript:alert(1)>',
     '<p>javascript:alert(1)</p>\n'),
    ('<JaVaScRiPt:alert(1)>',
     '<p>JaVaScRiPt:alert(1)</p>\n'),
    ('![a](javascript:alert(1))',
     '<p>a</p>\n'),
    ('![a](data:image/svg+xml;base64,PHN2Zz4=)',
     '<p>a</p>\n'),
    ('![a](https://evil.test/pixel.png)',
     '<p>a</p>\n'),
    ('[x](https://ok.test "t\\"><script>alert(1)</script>")',
     '<p><a href="https://ok.test" title="t&quot;&gt;&lt;script&gt;alert(1)&lt;/script&gt;">x</a></p>\n'),
    ('| a | b |\n|---|---|\n| <script>x</script> | [y](javascript:alert(1)) |',
     '<table>\n<thead>\n<tr>\n<th>a</th>\n<th>b</th>\n</tr>\n</thead>\n<tbody>\n<tr>\n<td><!-- raw HTML omitted -->x<!-- raw HTML omitted --></td>\n<td>y</td>\n</tr>\n</tbody>\n</table>\n'),
    ('`<script>`',
     '<p><code>&lt;script&gt;</code></p>\n'),
    ('```html\n<script>alert(1)</script>\n```',
     '<pre><code class="language-html">&lt;script&gt;alert(1)&lt;/script&gt;\n</code></pre>\n'),
    ('get [the file](ftp://files.acme.test/x) and [this](JaVaScRiPt:alert(1)) <ftp://files.acme.test/y>',
     '<p>get the file and this ftp://files.acme.test/y</p>\n'),
    ('**Rec: B.** Fewer hosts.\n\nSecond paragraph with `code`.\n\n- one\n- two\n\n1. first\n2. second\n\n| Option | Cost |\n|---|--:|\n| A | 2 d |\n| B | 1 d |\n\nSee [the PR](https://github.com/acme/web/pull/7), https://acme.test/x and <mailto:dev@acme.test>.',
     '<p><strong>Rec: B.</strong> Fewer hosts.</p>\n<p>Second paragraph with <code>code</code>.</p>\n<ul>\n<li>one</li>\n<li>two</li>\n</ul>\n<ol>\n<li>first</li>\n<li>second</li>\n</ol>\n<table>\n<thead>\n<tr>\n<th>Option</th>\n<th align="right">Cost</th>\n</tr>\n</thead>\n<tbody>\n<tr>\n<td>A</td>\n<td align="right">2 d</td>\n</tr>\n<tr>\n<td>B</td>\n<td align="right">1 d</td>\n</tr>\n</tbody>\n</table>\n<p>See <a href="https://github.com/acme/web/pull/7">the PR</a>, <a href="https://acme.test/x">https://acme.test/x</a> and <a href="mailto:dev@acme.test">mailto:dev@acme.test</a>.</p>\n'),
    ('[a](ftp&#58;//x.test/a)',
     '<p>a</p>\n'),
    ('[a](vscode&#58;//file/x)',
     '<p>a</p>\n'),
    ('[a](vscode&colon;//file/x)',
     '<p>a</p>\n'),
    ('[a](tel&#x3a;+15555550100)',
     '<p>a</p>\n'),
    ('[a](data&#58;image/png;base64,AAAA)',
     '<p>a</p>\n'),
    ('[a][r]\n\n[r]: vscode&#58;//x',
     '<p>a</p>\n'),
    ('[a][r]\n\n[r]: data&#58;image/png;base64,AAAA',
     '<p>a</p>\n'),
    ('[a](https&#58;//acme.test/x)',
     '<p><a href="https://acme.test/x">a</a></p>\n'),
    ('|a|b|\n|-|-|\n|1|2|',
     '<table>\n<thead>\n<tr>\n<th>a</th>\n<th>b</th>\n</tr>\n</thead>\n<tbody>\n<tr>\n<td>1</td>\n<td>2</td>\n</tr>\n</tbody>\n</table>\n'),
    ('- x',
     '<ul>\n<li>x</li>\n</ul>\n'),
    ('<b>',
     '<!-- raw HTML omitted -->\n'),
    ('## Summary\n\nThe export rework is **ready for review**. Two things need a decision:\n\n1. Whether to ship behind a flag.\n2. Who reviews the *schema* change.\n\n### Details\n\n- `export/queue.go`: the new worker pool (about 120 lines).\n- `export/schema.sql`: adds `exported_at`.\n  - nullable for now\n  - backfilled by a job\n- Tests: `go test ./export/...` passes.\n\n> Note: the backfill takes ~2 h on prod.\n\n---\n\n**Next:** on your answer athena briefs a worker.',
     '<h2>Summary</h2>\n<p>The export rework is <strong>ready for review</strong>. Two things need a decision:</p>\n<ol>\n<li>Whether to ship behind a flag.</li>\n<li>Who reviews the <em>schema</em> change.</li>\n</ol>\n<h3>Details</h3>\n<ul>\n<li><code>export/queue.go</code>: the new worker pool (about 120 lines).</li>\n<li><code>export/schema.sql</code>: adds <code>exported_at</code>.\n<ul>\n<li>nullable for now</li>\n<li>backfilled by a job</li>\n</ul>\n</li>\n<li>Tests: <code>go test ./export/...</code> passes.</li>\n</ul>\n<blockquote>\n<p>Note: the backfill takes ~2 h on prod.</p>\n</blockquote>\n<hr>\n<p><strong>Next:</strong> on your answer athena briefs a worker.</p>\n'),
    ('**Recommendation: after ABC-101 merges.** Both change `export/queue.go`; starting now means rebasing about 400 lines once ABC-101 lands.\n\n| Option | Starts | Risk | Cost |\n|---|---|---|---|\n| Start now | today | conflicts in `queue.go` | a rebase, about 1 h |\n| After ABC-101 | in about 2 days | none | waits on its review |\n\n- ABC-101 is [acme/web#48](https://github.com/acme/web/pull/48): approved, waiting on the merge.\n- The new export screen is in the preview below.\n\n**Next:** on your answer athena briefs a worker for ABC-111 with that start point.',
     '<p><strong>Recommendation: after ABC-101 merges.</strong> Both change <code>export/queue.go</code>; starting now means rebasing about 400 lines once ABC-101 lands.</p>\n<table>\n<thead>\n<tr>\n<th>Option</th>\n<th>Starts</th>\n<th>Risk</th>\n<th>Cost</th>\n</tr>\n</thead>\n<tbody>\n<tr>\n<td>Start now</td>\n<td>today</td>\n<td>conflicts in <code>queue.go</code></td>\n<td>a rebase, about 1 h</td>\n</tr>\n<tr>\n<td>After ABC-101</td>\n<td>in about 2 days</td>\n<td>none</td>\n<td>waits on its review</td>\n</tr>\n</tbody>\n</table>\n<ul>\n<li>ABC-101 is <a href="https://github.com/acme/web/pull/48">acme/web#48</a>: approved, waiting on the merge.</li>\n<li>The new export screen is in the preview below.</li>\n</ul>\n<p><strong>Next:</strong> on your answer athena briefs a worker for ABC-111 with that start point.</p>\n'),
    ('Result\n======\n\nAll green.\n\nWhat changed\n------------\n\n* one\n* two\n\n    indented code\n    more code\n\nDone.',
     '<h1>Result</h1>\n<p>All green.</p>\n<h2>What changed</h2>\n<ul>\n<li>\n<p>one</p>\n</li>\n<li>\n<p>two</p>\n<p>indented code\nmore code</p>\n</li>\n</ul>\n<p>Done.</p>\n'),
    ('1. Step one\n\n   ```bash\n   make test\n   ```\n\n2. Step two\n3. Step three\n   - sub a\n   - sub b\n\n     with a paragraph\n',
     '<ol>\n<li>\n<p>Step one</p>\n<pre><code class="language-bash">make test\n</code></pre>\n</li>\n<li>\n<p>Step two</p>\n</li>\n<li>\n<p>Step three</p>\n<ul>\n<li>\n<p>sub a</p>\n</li>\n<li>\n<p>sub b</p>\n<p>with a paragraph</p>\n</li>\n</ul>\n</li>\n</ol>\n'),
    ('A line with a hard break  \nnext line\\\nthird line\nfourth',
     '<p>A line with a hard break<br>\nnext line<br>\nthird line\nfourth</p>\n'),
    ('Links: <https://example.com/a?b=c&d=e>, www.example.com/path, http://example.com/x_(y), https://example.com/a.b. And (see https://example.com/z).',
     '<p>Links: <a href="https://example.com/a?b=c&amp;d=e">https://example.com/a?b=c&amp;d=e</a>, <a href="http://www.example.com/path">www.example.com/path</a>, <a href="http://example.com/x_(y)">http://example.com/x_(y)</a>, <a href="https://example.com/a.b">https://example.com/a.b</a>. And (see <a href="https://example.com/z">https://example.com/z</a>).</p>\n'),
    ('Mail me at dev@acme.test, or <dev@acme.test>, or mailto:dev@acme.test.',
     '<p>Mail me at <a href="mailto:dev@acme.test">dev@acme.test</a>, or <a href="mailto:dev@acme.test">dev@acme.test</a>, or mailto:dev@acme.test.</p>\n'),
    ('Entities: &copy; &amp; &lt;tag&gt; &#169; &#xA9; &nosuch; &quot;quoted&quot; and "plain quotes" & \'apostrophes\'.',
     "<p>Entities: © &amp; &lt;tag&gt; © © &amp;nosuch; &quot;quoted&quot; and &quot;plain quotes&quot; &amp; 'apostrophes'.</p>\n"),
    ('Escapes: \\*not em\\*, \\_x\\_, \\# not heading, \\[not link\\](x), a\\\\b, \\` tick.',
     '<p>Escapes: *not em*, _x_, # not heading, [not link](x), a\\b, ` tick.</p>\n'),
    ('~~struck~~ and ~one~ and ~~~three~~~ and a~~b~~c',
     '<p><del>struck</del> and <del>one</del> and ~~~three~~~ and a<del>b</del>c</p>\n'),
    ('| Left | Center | Right | None |\n|:-----|:------:|------:|------|\n| a | b | c | d |\n| e | f | g |\n| h | i | j | k | extra |',
     '<table>\n<thead>\n<tr>\n<th align="left">Left</th>\n<th align="center">Center</th>\n<th align="right">Right</th>\n<th>None</th>\n</tr>\n</thead>\n<tbody>\n<tr>\n<td align="left">a</td>\n<td align="center">b</td>\n<td align="right">c</td>\n<td>d</td>\n</tr>\n<tr>\n<td align="left">e</td>\n<td align="center">f</td>\n<td align="right">g</td>\n<td></td>\n</tr>\n<tr>\n<td align="left">h</td>\n<td align="center">i</td>\n<td align="right">j</td>\n<td>k</td>\n</tr>\n</tbody>\n</table>\n'),
    ('| a | `b \\| c` | d \\| e |\n|---|---|---|\n| `x \\| y` | z | w |',
     '<table>\n<thead>\n<tr>\n<th>a</th>\n<th><code>b | c</code></th>\n<th>d | e</th>\n</tr>\n</thead>\n<tbody>\n<tr>\n<td><code>x | y</code></td>\n<td>z</td>\n<td>w</td>\n</tr>\n</tbody>\n</table>\n'),
    ('Text before\n| a | b |\n| - | - |\n| 1 | 2 |\nText after?',
     '<p>Text before</p>\n<table>\n<thead>\n<tr>\n<th>a</th>\n<th>b</th>\n</tr>\n</thead>\n<tbody>\n<tr>\n<td>1</td>\n<td>2</td>\n</tr>\n<tr>\n<td>Text after?</td>\n<td></td>\n</tr>\n</tbody>\n</table>\n'),
    ('[ref link][Ref] and [Ref][] and [Ref].\n\n[ref]: https://example.com/ref "Ref Title"',
     '<p><a href="https://example.com/ref" title="Ref Title">ref link</a> and <a href="https://example.com/ref" title="Ref Title">Ref</a> and <a href="https://example.com/ref" title="Ref Title">Ref</a>.</p>\n'),
    ('[inline](https://example.com "Title & more") [paren](https://example.com \'single\') [bracket](<https://example.com/a b>)',
     '<p><a href="https://example.com" title="Title &amp; more">inline</a> <a href="https://example.com" title="single">paren</a> <a href="https://example.com/a%20b">bracket</a></p>\n'),
    ('A [link with **bold** and `code`](https://example.com) and ![an *image*](https://example.com/i.png "t").',
     '<p>A <a href="https://example.com">link with <strong>bold</strong> and <code>code</code></a> and an <em>image</em>.</p>\n'),
    ('Nested: [outer [inner](https://a.test) text](https://b.test)',
     '<p>Nested: [outer <a href="https://a.test">inner</a> text](<a href="https://b.test">https://b.test</a>)</p>\n'),
    ('- [ ] task one\n- [x] task done',
     '<ul>\n<li>[ ] task one</li>\n<li>[x] task done</li>\n</ul>\n'),
    ('* loose\n\n* list\n\n* items',
     '<ul>\n<li>\n<p>loose</p>\n</li>\n<li>\n<p>list</p>\n</li>\n<li>\n<p>items</p>\n</li>\n</ul>\n'),
    ('+ a\n+ b\n\n- c\n- d',
     '<ul>\n<li>a</li>\n<li>b</li>\n</ul>\n<ul>\n<li>c</li>\n<li>d</li>\n</ul>\n'),
    ('3. three\n4. four\n\n10. ten\n    continued\n11. eleven',
     '<ol start="3">\n<li>\n<p>three</p>\n</li>\n<li>\n<p>four</p>\n</li>\n<li>\n<p>ten\ncontinued</p>\n</li>\n<li>\n<p>eleven</p>\n</li>\n</ol>\n'),
    ('> quote\n> > nested\n> back\n\nlazy\n> continuation\nhere',
     '<blockquote>\n<p>quote</p>\n<blockquote>\n<p>nested\nback</p>\n</blockquote>\n</blockquote>\n<p>lazy</p>\n<blockquote>\n<p>continuation\nhere</p>\n</blockquote>\n'),
    ('```\nunclosed fence\nstill code',
     '<pre><code>unclosed fence\nstill code\n</code></pre>\n'),
    ("~~~ python extra words\nprint('hi') # <b>\n~~~",
     '<pre><code class="language-python">print(\'hi\') # &lt;b&gt;\n</code></pre>\n'),
    ('```js {.class}\nlet x = 1 < 2 && 3 > 2;\n```',
     '<pre><code class="language-js">let x = 1 &lt; 2 &amp;&amp; 3 &gt; 2;\n</code></pre>\n'),
    ('<div>\n**not md**\n</div>\n\nafter div',
     '<!-- raw HTML omitted -->\n<p>after div</p>\n'),
    ('<!-- a comment -->\nText <!-- inline comment --> more <?php echo 1; ?> and <![CDATA[x]]>.',
     '<!-- raw HTML omitted -->\n<p>Text <!-- raw HTML omitted --> more <!-- raw HTML omitted --> and <!-- raw HTML omitted -->.</p>\n'),
    ('Inline <span class="x">html</span> and </span> close and <br/> void.',
     '<p>Inline <!-- raw HTML omitted -->html<!-- raw HTML omitted --> and <!-- raw HTML omitted --> close and <!-- raw HTML omitted --> void.</p>\n'),
    ('Tabs:\n\n\tcode with tab\n\n-\titem with tab\n\n>\tquote tab',
     '<p>Tabs:</p>\n<pre><code>code with tab\n</code></pre>\n<ul>\n<li>item with tab</li>\n</ul>\n<blockquote>\n<p>quote tab</p>\n</blockquote>\n'),
    ('Line one\r\nLine two\r\n\r\n- item\r\n- item two',
     '<p>Line one\nLine two</p>\n<ul>\n<li>item</li>\n<li>item two</li>\n</ul>\n'),
    ('Unicode: café — “quotes” ‘single’ … →, emoji 🎉, CJK 中文。*强调*。',
     '<p>Unicode: café — “quotes” ‘single’ … →, emoji 🎉, CJK 中文。<em>强调</em>。</p>\n'),
    ('_under_score_ and snake_case_name and __bold__ and ___both___',
     '<p><em>under_score</em> and snake_case_name and <strong>bold</strong> and <em><strong>both</strong></em></p>\n'),
    ('***strong em*** and **bold *nested em* bold** and *em **nested strong** em*',
     '<p><em><strong>strong em</strong></em> and <strong>bold <em>nested em</em> bold</strong> and <em>em <strong>nested strong</strong> em</em></p>\n'),
    ('Trailing punctuation: https://example.com/path?q=1&r=2; then https://example.com/a&amp; then www.example.com.',
     '<p>Trailing punctuation: <a href="https://example.com/path?q=1&amp;r=2;">https://example.com/path?q=1&amp;r=2;</a> then <a href="https://example.com/a">https://example.com/a</a>&amp; then <a href="http://www.example.com">www.example.com</a>.</p>\n'),
    ('https://example.com/foo_(bar)_baz) and https://en.wikipedia.org/wiki/Foo_(bar)',
     '<p><a href="https://example.com/foo_(bar)_baz">https://example.com/foo_(bar)_baz</a>) and <a href="https://en.wikipedia.org/wiki/Foo_(bar)">https://en.wikipedia.org/wiki/Foo_(bar)</a></p>\n'),
    ('Port: http://localhost:8080/x and http://127.0.0.1:2843/ and http://acme.test:99/y',
     '<p>Port: http://localhost:8080/x and http://127.0.0.1:2843/ and <a href="http://acme.test:99/y">http://acme.test:99/y</a></p>\n'),
    ('Weird schemes: <tel:+15555550100> <chrome://settings> <HTTPS://EXAMPLE.TEST/X> <irc://x.test>',
     '<p>Weird schemes: tel:+15555550100 chrome://settings <a href="HTTPS://EXAMPLE.TEST/X">HTTPS://EXAMPLE.TEST/X</a> irc://x.test</p>\n'),
    ('[rel](./notes.md) [abs](/x/y) [frag](#top) [q](?a=1) [proto-rel](//evil.test/x) [mail](mailto:a@b.test)',
     '<p><a href="./notes.md">rel</a> <a href="/x/y">abs</a> <a href="#top">frag</a> <a href="?a=1">q</a> <a href="//evil.test/x">proto-rel</a> <a href="mailto:a@b.test">mail</a></p>\n'),
    ('[enc](https://example.com/a%20b) [space](<a b>) [unicode](https://example.com/café) [quote](https://example.com/"x)',
     '<p><a href="https://example.com/a%20b">enc</a> <a href="a%20b">space</a> <a href="https://example.com/caf%C3%A9">unicode</a> <a href="https://example.com/%22x">quote</a></p>\n'),
    ('Heading with trailing hashes\n# Title ###\n## Sub ## more ##\n###### six\n####### seven',
     '<p>Heading with trailing hashes</p>\n<h1>Title</h1>\n<h2>Sub ## more</h2>\n<h6>six</h6>\n<p>####### seven</p>\n'),
    ('Setext after list\n- item\n---\nPara\n***\nend',
     '<p>Setext after list</p>\n<ul>\n<li>item</li>\n</ul>\n<hr>\n<p>Para</p>\n<hr>\n<p>end</p>\n'),
    ('Empty items:\n\n-\n- \n-   \n\n1.\n2. x',
     '<p>Empty items:</p>\n<ul>\n<li></li>\n<li></li>\n<li></li>\n</ul>\n<ol>\n<li></li>\n<li>x</li>\n</ol>\n'),
    ('a  \n  \n b',
     '<p>a</p>\n<p>b</p>\n'),
    ('`` code with ` backtick `` and ``` unclosed',
     '<p><code>code with ` backtick</code> and ``` unclosed</p>\n'),
    ('Hard break at end of paragraph  ',
     '<p>Hard break at end of paragraph</p>\n'),
    ('* * *\n- - -\n_ _ _',
     '<hr>\n<hr>\n<hr>\n'),
    ('Image only: ![alt text](https://x.test/a.png)',
     '<p>Image only: alt text</p>\n'),
    ('Image in link: [![badge](https://x.test/b.svg)](https://x.test/ci)',
     '<p>Image in link: <a href="https://x.test/ci">badge</a></p>\n'),
    ('Autolink in link: [see <https://a.test>](https://b.test)',
     '<p>Autolink in link: <a href="https://b.test">see <a href="https://a.test">https://a.test</a></a></p>\n'),
    ('Email-ish: foo@bar, foo@bar.c, foo.bar@baz.example.org., a_b@c-d.ef-',
     '<p>Email-ish: foo@bar, <a href="mailto:foo@bar.c">foo@bar.c</a>, <a href="mailto:foo.bar@baz.example.org">foo.bar@baz.example.org</a>., a_b@c-d.ef-</p>\n'),
    ('www.example.com and www.ex_ample.com and www.example and wwwexample.com',
     '<p><a href="http://www.example.com">www.example.com</a> and <a href="http://www.ex_ample.com">www.ex_ample.com</a> and www.example and wwwexample.com</p>\n'),
    ('(www.example.com) *www.example.com* _https://x.test_ ~https://y.test~',
     '<p>(<a href="http://www.example.com">www.example.com</a>) <em><a href="http://www.example.com">www.example.com</a></em> <em><a href="https://x.test">https://x.test</a></em> <del><a href="https://y.test">https://y.test</a></del></p>\n'),
    ('Numbers: 1) one\n2) two\n\n1. a\n1. b\n1. c',
     '<p>Numbers: 1) one\n2) two</p>\n<ol>\n<li>a</li>\n<li>b</li>\n<li>c</li>\n</ol>\n'),
    ('Paragraph\n    not code (lazy)\n\n    code now',
     '<p>Paragraph\nnot code (lazy)</p>\n<pre><code>code now\n</code></pre>\n'),
    ('| only header |\n|---|',
     '<table>\n<thead>\n<tr>\n<th>only header</th>\n</tr>\n</thead>\n</table>\n'),
    ('|a|\n|-|\n|b|\n\n|x|y|\n|-|\n|1|2|',
     '<table>\n<thead>\n<tr>\n<th>a</th>\n</tr>\n</thead>\n<tbody>\n<tr>\n<td>b</td>\n</tr>\n</tbody>\n</table>\n<p>|x|y|\n|-|\n|1|2|</p>\n'),
    ('Table in list:\n\n- | a | b |\n  |---|---|\n  | 1 | 2 |',
     '<p>Table in list:</p>\n<ul>\n<li>\n<table>\n<thead>\n<tr>\n<th>a</th>\n<th>b</th>\n</tr>\n</thead>\n<tbody>\n<tr>\n<td>1</td>\n<td>2</td>\n</tr>\n</tbody>\n</table>\n</li>\n</ul>\n'),
    ('Long **bold\nacross lines** and *em\nacross*.',
     '<p>Long <strong>bold\nacross lines</strong> and <em>em\nacross</em>.</p>\n'),
    ("Ref with angle: [x]\n\n[x]: <https://a.test/b c> 'title'",
     '<p>Ref with angle: <a href="https://a.test/b%20c" title="title">x</a></p>\n'),
    ('[foo]: /url1\n[foo]: /url2\n\n[foo]',
     '<p><a href="/url1">foo</a></p>\n'),
    ('Backslash at end\\',
     '<p>Backslash at end\\</p>\n'),
    ('Dangling [bracket and ![image start and ]close',
     '<p>Dangling [bracket and ![image start and ]close</p>\n'),
    ('Entity in code: `&amp;` and in fence:\n```\n&amp; &lt;\n```',
     '<p>Entity in code: <code>&amp;amp;</code> and in fence:</p>\n<pre><code>&amp;amp; &amp;lt;\n</code></pre>\n'),
    ('<https://example.com/a"b> <http://x.test/<y>',
     '<p><a href="https://example.com/a%22b">https://example.com/a&quot;b</a> &lt;http://x.test/<!-- raw HTML omitted --></p>\n'),
    ('Here is a link to https://github.com/acme/web/pull/48#issuecomment-123 and PR #48.',
     '<p>Here is a link to <a href="https://github.com/acme/web/pull/48#issuecomment-123">https://github.com/acme/web/pull/48#issuecomment-123</a> and PR #48.</p>\n'),
    ('* item one\n* item two\n\n  second paragraph of item two\n* item three',
     '<ul>\n<li>\n<p>item one</p>\n</li>\n<li>\n<p>item two</p>\n<p>second paragraph of item two</p>\n</li>\n<li>\n<p>item three</p>\n</li>\n</ul>\n'),
    ('1. one\n   * nested bullet\n     1. deep ordered\n2. two',
     '<ol>\n<li>one\n<ul>\n<li>nested bullet\n<ol>\n<li>deep ordered</li>\n</ol>\n</li>\n</ul>\n</li>\n<li>two</li>\n</ol>\n'),
]

# Examples from the CommonMark spec goldmark ships (two of each section) and from its table, linkify and strikethrough tests.
SPEC = [
    ('\tfoo\tbaz\t\tbim',  # spec 1 (Tabs)
     '<pre><code>foo\tbaz\t\tbim\n</code></pre>\n'),
    ('  \tfoo\tbaz\t\tbim',  # spec 2 (Tabs)
     '<pre><code>foo\tbaz\t\tbim\n</code></pre>\n'),
    ('\\!\\"\\#\\$\\%\\&\\\'\\(\\)\\*\\+\\,\\-\\.\\/\\:\\;\\<\\=\\>\\?\\@\\[\\\\\\]\\^\\_\\`\\{\\|\\}\\~',  # spec 12 (Backslash escapes)
     "<p>!&quot;#$%&amp;'()*+,-./:;&lt;=&gt;?@[\\]^_`{|}~</p>\n"),
    ('\\\t\\A\\a\\ \\3\\φ\\«',  # spec 13 (Backslash escapes)
     '<p>\\\t\\A\\a\\ \\3\\φ\\«</p>\n'),
    ('&nbsp; &amp; &copy; &AElig; &Dcaron;\n&frac34; &HilbertSpace; &DifferentialD;\n&ClockwiseContourIntegral; &ngE;',  # spec 25 (Entity and numeric character references)
     '<p>\xa0 &amp; © Æ Ď\n¾ ℋ ⅆ\n∲ ≧̸</p>\n'),
    ('&#35; &#1234; &#992; &#0;',  # spec 26 (Entity and numeric character references)
     '<p># Ӓ Ϡ �</p>\n'),
    ('- `one\n- two`',  # spec 42 (Precedence)
     '<ul>\n<li>`one</li>\n<li>two`</li>\n</ul>\n'),
    ('***\n---\n___',  # spec 43 (Thematic breaks)
     '<hr>\n<hr>\n<hr>\n'),
    ('+++',  # spec 44 (Thematic breaks)
     '<p>+++</p>\n'),
    ('# foo\n## foo\n### foo\n#### foo\n##### foo\n###### foo',  # spec 62 (ATX headings)
     '<h1>foo</h1>\n<h2>foo</h2>\n<h3>foo</h3>\n<h4>foo</h4>\n<h5>foo</h5>\n<h6>foo</h6>\n'),
    ('####### foo',  # spec 63 (ATX headings)
     '<p>####### foo</p>\n'),
    ('Foo *bar*\n=========\n\nFoo *bar*\n---------',  # spec 80 (Setext headings)
     '<h1>Foo <em>bar</em></h1>\n<h2>Foo <em>bar</em></h2>\n'),
    ('Foo *bar\nbaz*\n====',  # spec 81 (Setext headings)
     '<h1>Foo <em>bar\nbaz</em></h1>\n'),
    ('    a simple\n      indented code block',  # spec 107 (Indented code blocks)
     '<pre><code>a simple\n  indented code block\n</code></pre>\n'),
    ('  - foo\n\n    bar',  # spec 108 (Indented code blocks)
     '<ul>\n<li>\n<p>foo</p>\n<p>bar</p>\n</li>\n</ul>\n'),
    ('```\n<\n >\n```',  # spec 119 (Fenced code blocks)
     '<pre><code>&lt;\n &gt;\n</code></pre>\n'),
    ('~~~\n<\n >\n~~~',  # spec 120 (Fenced code blocks)
     '<pre><code>&lt;\n &gt;\n</code></pre>\n'),
    ('<table><tr><td>\n<pre>\n**Hello**,\n\n_world_.\n</pre>\n</td></tr></table>',  # spec 148 (HTML blocks)
     '<!-- raw HTML omitted -->\n<p><em>world</em>.\n<!-- raw HTML omitted --></p>\n<!-- raw HTML omitted -->\n'),
    ('<table>\n  <tr>\n    <td>\n           hi\n    </td>\n  </tr>\n</table>\n\nokay.',  # spec 149 (HTML blocks)
     '<!-- raw HTML omitted -->\n<p>okay.</p>\n'),
    ('[foo]: /url "title"\n\n[foo]',  # spec 192 (Link reference definitions)
     '<p><a href="/url" title="title">foo</a></p>\n'),
    ("   [foo]: \n      /url  \n           'the title'  \n\n[foo]",  # spec 193 (Link reference definitions)
     '<p><a href="/url" title="the title">foo</a></p>\n'),
    ('aaa\n\nbbb',  # spec 219 (Paragraphs)
     '<p>aaa</p>\n<p>bbb</p>\n'),
    ('aaa\nbbb\n\nccc\nddd',  # spec 220 (Paragraphs)
     '<p>aaa\nbbb</p>\n<p>ccc\nddd</p>\n'),
    ('aaa\n  \n\n# aaa',  # spec 227 (Blank lines)
     '<p>aaa</p>\n<h1>aaa</h1>\n'),
    ('> # Foo\n> bar\n> baz',  # spec 228 (Block quotes)
     '<blockquote>\n<h1>Foo</h1>\n<p>bar\nbaz</p>\n</blockquote>\n'),
    ('># Foo\n>bar\n> baz',  # spec 229 (Block quotes)
     '<blockquote>\n<h1>Foo</h1>\n<p>bar\nbaz</p>\n</blockquote>\n'),
    ('A paragraph\nwith two lines.\n\n    indented code\n\n> A block quote.',  # spec 253 (List items)
     '<p>A paragraph\nwith two lines.</p>\n<pre><code>indented code\n</code></pre>\n<blockquote>\n<p>A block quote.</p>\n</blockquote>\n'),
    ('1.  A paragraph\n    with two lines.\n\n        indented code\n\n    > A block quote.',  # spec 254 (List items)
     '<ol>\n<li>\n<p>A paragraph\nwith two lines.</p>\n<pre><code>indented code\n</code></pre>\n<blockquote>\n<p>A block quote.</p>\n</blockquote>\n</li>\n</ol>\n'),
    ('- foo\n- bar\n+ baz',  # spec 301 (Lists)
     '<ul>\n<li>foo</li>\n<li>bar</li>\n</ul>\n<ul>\n<li>baz</li>\n</ul>\n'),
    ('1. foo\n2. bar\n3) baz',  # spec 302 (Lists)
     '<ol>\n<li>foo</li>\n<li>bar</li>\n</ol>\n<ol start="3">\n<li>baz</li>\n</ol>\n'),
    ('`hi`lo`',  # spec 327 (Inlines)
     '<p><code>hi</code>lo`</p>\n'),
    ('`foo`',  # spec 328 (Code spans)
     '<p><code>foo</code></p>\n'),
    ('`` foo ` bar ``',  # spec 329 (Code spans)
     '<p><code>foo ` bar</code></p>\n'),
    ('*foo bar*',  # spec 350 (Emphasis and strong emphasis)
     '<p><em>foo bar</em></p>\n'),
    ('a * foo bar*',  # spec 351 (Emphasis and strong emphasis)
     '<p>a * foo bar*</p>\n'),
    ('[link](/uri "title")',  # spec 482 (Links)
     '<p><a href="/uri" title="title">link</a></p>\n'),
    ('[link](/uri)',  # spec 483 (Links)
     '<p><a href="/uri">link</a></p>\n'),
    ('![foo](/url "title")',  # spec 572 (Images)
     '<p>foo</p>\n'),
    ('![foo *bar*]\n\n[foo *bar*]: train.jpg "train & tracks"',  # spec 573 (Images)
     '<p>foo <em>bar</em></p>\n'),
    ('<http://foo.bar.baz>',  # spec 594 (Autolinks)
     '<p><a href="http://foo.bar.baz">http://foo.bar.baz</a></p>\n'),
    ('<https://foo.bar.baz/test?q=hello&id=22&boolean>',  # spec 595 (Autolinks)
     '<p><a href="https://foo.bar.baz/test?q=hello&amp;id=22&amp;boolean">https://foo.bar.baz/test?q=hello&amp;id=22&amp;boolean</a></p>\n'),
    ('<a><bab><c2c>',  # spec 613 (Raw HTML)
     '<p><!-- raw HTML omitted --><!-- raw HTML omitted --><!-- raw HTML omitted --></p>\n'),
    ('<a/><b2/>',  # spec 614 (Raw HTML)
     '<p><!-- raw HTML omitted --><!-- raw HTML omitted --></p>\n'),
    ('foo  \nbaz',  # spec 633 (Hard line breaks)
     '<p>foo<br>\nbaz</p>\n'),
    ('foo\\\nbaz',  # spec 634 (Hard line breaks)
     '<p>foo<br>\nbaz</p>\n'),
    ('foo\nbaz',  # spec 648 (Soft line breaks)
     '<p>foo\nbaz</p>\n'),
    ('foo \n baz',  # spec 649 (Soft line breaks)
     '<p>foo\nbaz</p>\n'),
    ("hello $.;'there",  # spec 650 (Textual content)
     "<p>hello $.;'there</p>\n"),
    ('Foo χρῆν',  # spec 651 (Textual content)
     '<p>Foo χρῆν</p>\n'),
    ('| foo | bar |\n| --- | --- |\n| baz | bim |',  # table0
     '<table>\n<thead>\n<tr>\n<th>foo</th>\n<th>bar</th>\n</tr>\n</thead>\n<tbody>\n<tr>\n<td>baz</td>\n<td>bim</td>\n</tr>\n</tbody>\n</table>\n'),
    ('| abc | defghi |\n:-: | -----------:\nbar | baz',  # table1
     '<table>\n<thead>\n<tr>\n<th align="center">abc</th>\n<th align="right">defghi</th>\n</tr>\n</thead>\n<tbody>\n<tr>\n<td align="center">bar</td>\n<td align="right">baz</td>\n</tr>\n</tbody>\n</table>\n'),
    ('| f\\|oo  |\n| ------ |\n| b `\\|` az |\n| b **\\|** im |',  # table2
     '<table>\n<thead>\n<tr>\n<th>f|oo</th>\n</tr>\n</thead>\n<tbody>\n<tr>\n<td>b <code>|</code> az</td>\n</tr>\n<tr>\n<td>b <strong>|</strong> im</td>\n</tr>\n</tbody>\n</table>\n'),
    ('| abc | def |\n| --- | --- |\n| bar | baz |\n> bar',  # table3
     '<table>\n<thead>\n<tr>\n<th>abc</th>\n<th>def</th>\n</tr>\n</thead>\n<tbody>\n<tr>\n<td>bar</td>\n<td>baz</td>\n</tr>\n</tbody>\n</table>\n<blockquote>\n<p>bar</p>\n</blockquote>\n'),
    ('| abc | def |\n| --- | --- |\n| bar | baz |\nbar\n\nbar',  # table4
     '<table>\n<thead>\n<tr>\n<th>abc</th>\n<th>def</th>\n</tr>\n</thead>\n<tbody>\n<tr>\n<td>bar</td>\n<td>baz</td>\n</tr>\n<tr>\n<td>bar</td>\n<td></td>\n</tr>\n</tbody>\n</table>\n<p>bar</p>\n'),
    ('| abc | def |\n| --- |\n| bar |',  # table5
     '<p>| abc | def |\n| --- |\n| bar |</p>\n'),
    ('www.commonmark.org',  # linkify0
     '<p><a href="http://www.commonmark.org">www.commonmark.org</a></p>\n'),
    ('Visit www.commonmark.org/help for more information.',  # linkify1
     '<p>Visit <a href="http://www.commonmark.org/help">www.commonmark.org/help</a> for more information.</p>\n'),
    ('www.google.com/search?q=Markup+(business)\n\nwww.google.com/search?q=Markup+(business)))\n\n(www.google.com/search?q=Markup+(business))\n\n(www.google.com/search?q=Markup+(business)',  # linkify2
     '<p><a href="http://www.google.com/search?q=Markup+(business)">www.google.com/search?q=Markup+(business)</a></p>\n<p><a href="http://www.google.com/search?q=Markup+(business)">www.google.com/search?q=Markup+(business)</a>))</p>\n<p>(<a href="http://www.google.com/search?q=Markup+(business)">www.google.com/search?q=Markup+(business)</a>)</p>\n<p>(<a href="http://www.google.com/search?q=Markup+(business)">www.google.com/search?q=Markup+(business)</a></p>\n'),
    ('www.google.com/search?q=(business))+ok',  # linkify3
     '<p><a href="http://www.google.com/search?q=(business))+ok">www.google.com/search?q=(business))+ok</a></p>\n'),
    ('www.google.com/search?q=commonmark&hl=en\n\nwww.google.com/search?q=commonmark&hl;',  # linkify4
     '<p><a href="http://www.google.com/search?q=commonmark&amp;hl=en">www.google.com/search?q=commonmark&amp;hl=en</a></p>\n<p><a href="http://www.google.com/search?q=commonmark">www.google.com/search?q=commonmark</a>&amp;hl;</p>\n'),
    ('www.commonmark.org/he<lp',  # linkify5
     '<p><a href="http://www.commonmark.org/he">www.commonmark.org/he</a>&lt;lp</p>\n'),
    ('~~Hi~~ Hello, world!',  # strikethrough0
     '<p><del>Hi</del> Hello, world!</p>\n'),
    ('This ~~has a\n\nnew paragraph~~.',  # strikethrough1
     '<p>This ~~has a</p>\n<p>new paragraph~~.</p>\n'),
    ('~Hi~ Hello, world!',  # strikethrough2
     '<p><del>Hi</del> Hello, world!</p>\n'),
    ('This will ~~~not~~~ strike.',  # strikethrough3
     '<p>This will ~~~not~~~ strike.</p>\n'),
]
