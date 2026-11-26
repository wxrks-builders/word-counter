import re

_IMAGE_MARKDOWN_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_LINK_MARKDOWN_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_LEADING_MARKUP_RE = re.compile(r"^\s*(#{1,6}|[-*+]|\d+\.|>)\s+", re.MULTILINE)


def count_words(text: str | None) -> int:
    if not text:
        return 0
    return len(text.split())


def clean_markdown_for_counting(text: str) -> str:
    """Strips markdown syntax a browser text-selection would never include: image markup, link URLs, heading/bullet markers."""
    text = _IMAGE_MARKDOWN_RE.sub("", text)
    text = _LINK_MARKDOWN_RE.sub(r"\1", text)
    text = _LEADING_MARKUP_RE.sub("", text)
    return text
