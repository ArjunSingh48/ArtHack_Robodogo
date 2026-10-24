"""The primary encounter's image caption, shared by Telegram and console previews."""

from __future__ import annotations

import html


class ImageMessageError(ValueError):
    """An image message cannot be prepared without changing its meaning/credit."""


def image_caption(scene, caption: str) -> str:
    """Return Telegram HTML, preserving the complete attribution and source link.

    Telegram counts the 1024-character caption limit after entity parsing.
    Never slice HTML or silently discard required attribution to fit that limit.
    """
    if scene.kind == "meme":
        credit = f"{scene.name}, {scene.year}. {scene.attribution}"
        source_label = "Image source"
        source_url = scene.image.source_page or scene.page_url
        # Presentation authorization is internal provenance, not visitor copy.
        rights = "" if scene.image.rights.value == "presentation_use" else scene.image.license_note
    else:
        date = f", {scene.date}" if scene.date else ""
        credit = f"{scene.artist} — {scene.title}{date}."
        source_label = scene.museum
        source_url = scene.page_url
        rights = f"{scene.rights.status}. {scene.rights.source_statement}"
    suffix = f" {rights}" if rights else ""
    plain = f"{caption}\n\n{credit} {source_label}.{suffix}"
    if len(plain) > 1024:
        raise ImageMessageError("caption and complete attribution exceed Telegram's 1024-character limit")
    source = f'<a href="{html.escape(str(source_url), quote=True)}">{html.escape(source_label)}</a>'
    return f"{html.escape(caption)}\n\n<i>{html.escape(credit)} {source}.{html.escape(suffix)}</i>"
