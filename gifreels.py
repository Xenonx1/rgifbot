"""Pure helpers for gifreels.com (no network, no third-party imports)."""
import re
from html import unescape
from urllib.parse import quote

SITE = "https://gifreels.com"
MEDIA_URL = "https://p5rn.com/cdn/storage/production/gifreels/post/{id}/gif.mp4"
POSTER_URL = "https://p5rn.com/cdn/storage/production/gifreels/post/{id}/poster-240.webp"

LINK_RE = re.compile(
    r'<a\b[^>]*?href=["\'](?:https?://(?:www\.)?gifreels\.com)?/gif/([A-Za-z0-9_-]{4,16})["\'][^>]*>(.*?)</a>',
    re.S | re.I,
)
BARE_ID_RE = re.compile(r'/gif/([A-Za-z0-9_-]{4,16})(?=["\'?#/\s<])')
ALT_RE = re.compile(r'alt=["\']([^"\']*)["\']', re.I)
TAGS_RE = re.compile(r"<[^>]+>")
COUNT_RE = re.compile(r"\s+\d[\d.,]*[kKmM]?$")
JUNK_TITLES = {"", "feed", "gif"}


def slugify(query: str) -> str:
    """'Big  Tits' -> 'big-tits' (the site's tag URL format)."""
    slug = "-".join(query.strip().lower().split())
    return quote(slug, safe="()+-_")


def media_url(gif_id: str) -> str:
    return MEDIA_URL.format(id=gif_id)


def add_page(url: str, page: int, param: str = "page") -> str:
    return f"{url}{'&' if '?' in url else '?'}{param}={page}"


def _clean_title(inner_html: str) -> str:
    alt = ALT_RE.search(inner_html)
    if alt and alt.group(1).strip():
        text = alt.group(1)
    else:
        text = COUNT_RE.sub("", " ".join(unescape(TAGS_RE.sub(" ", inner_html)).split()))
    return " ".join(unescape(text).split())[:100]


def parse_listing(html: str) -> list[dict]:
    """Return [{'id', 'title'}] in page order, de-duplicated."""
    found: dict[str, str] = {}
    for match in LINK_RE.finditer(html):
        gif_id, title = match.group(1), _clean_title(match.group(2))
        if gif_id not in found:
            found[gif_id] = title
        elif found[gif_id].lower() in JUNK_TITLES and title.lower() not in JUNK_TITLES:
            found[gif_id] = title

    for match in BARE_ID_RE.finditer(html):
        found.setdefault(match.group(1), "")

    return [
        {"id": gif_id, "title": title if title.lower() not in JUNK_TITLES else "GIF"}
        for gif_id, title in found.items()
    ]
