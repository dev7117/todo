"""Exercise the actual server renderer, including the browser-facing HTML contract."""
from html.parser import HTMLParser

import pytest

from todo_v2.markdown import render


class Tags(HTMLParser):
    def __init__(self, text):
        super().__init__()
        self.tags = []
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))


def test_xss_is_escaped_and_javascript_links_removed():
    payload = '<script>alert(1)</script>\n<img src=x onerror=alert(2)>\n[click](javascript:alert%281%29)'
    html = render(payload)
    assert '<script' not in html and '<img' not in html and 'javascript:' not in html
    assert '&lt;script&gt;' in html and '&lt;img' in html
    assert all(tag in {'p', 'br'} and not attrs for tag, attrs in Tags(html).tags)


@pytest.mark.parametrize('url', [
    'JaVaScRiPt:alert%281%29', 'data:text/html,hello', 'vbscript:msgbox',
    'javascript&#58;alert', '//example.org', 'java\tscript:alert',
    'https://[invalid', 'https://example.org/"onclick="alert',
])
def test_hostile_links_cannot_inject_attributes(url):
    html = render(f'[link]({url})')
    for tag, attrs in Tags(html).tags:
        assert set(attrs) <= {'href', 'rel'}
        if tag == 'a':
            assert attrs['href'].startswith('https://')


def test_supported_markdown_and_safe_links():
    html = render('# Plan\n\n**Basil** and *mint*, `a < b`\nnext line\n\n- Measure\n- Plant\n\n'
                  '[Guide](https://example.org/herbs?a=1&b=2)\n```\n<script>literal</script>\n```')
    assert '<h1>Plan</h1>' in html
    assert '<strong>Basil</strong>' in html and '<em>mint</em>' in html
    assert '<code>a &lt; b</code>' in html and '<br>next line' in html
    assert '<ul><li>Measure</li><li>Plant</li></ul>' in html
    assert 'href="https://example.org/herbs?a=1&amp;b=2"' in html
    assert '<pre><code>&lt;script&gt;literal&lt;/script&gt;</code></pre>' in html
    assert render('```\n<img>') == '<pre><code>&lt;img&gt;</code></pre>'
