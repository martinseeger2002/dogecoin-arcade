"""Deciding whether a file is safe to show, and as what.

The danger
----------
An attachment is chosen by somebody else and rendered in the origin that holds
the wallet. Getting this wrong is not a cosmetic bug: a file served as HTML or
SVG in this origin runs script with access to everything the interface can do.

So three rules, each of which alone would be insufficient:

1. **The declared type is never trusted.** A sender can claim anything. The type
   used for rendering comes from the file's own leading bytes, and if those are
   not recognised the file is not rendered at all. `image/png` on a file starting
   with `<script>` gets a download link, not an `<img>`.

2. **The allow-list is of formats that cannot execute.** PNG, JPEG, GIF, WebP,
   MP3, WAV, OGG, MP4 and WebM are parsed by decoders, not interpreters. SVG is
   deliberately absent: it is XML that can carry script, and it is the single
   most common way this goes wrong. PDF is absent for the same reason -- PDF has
   an execution model.

3. **The response is defanged anyway.** `nosniff` so the browser cannot second
   guess the type, a sandbox CSP so a hypothetical bypass still cannot run
   anything, and `Content-Disposition: inline` only for types on the list.

Anything not on the list still arrives intact and downloadable. Refusing to
*render* is not refusing to deliver.
"""

from __future__ import annotations

from dataclasses import dataclass

#: Beyond this, a file is offered as a download rather than rendered inline.
#: A page that tries to inline 40 MB of video before the viewer has asked for it
#: is a worse experience than a link.
MAX_INLINE_BYTES = 12 * 1024 * 1024


@dataclass(frozen=True)
class Media:
    kind: str          # "image", "audio" or "video" -- which element to use
    mime: str          # what to send as Content-Type
    label: str         # what to call it in the interface


def _matches(data: bytes, prefix: bytes, offset: int = 0) -> bool:
    return data[offset:offset + len(prefix)] == prefix


def sniff(data: bytes) -> Media | None:
    """Identify a file from its leading bytes, or None if it is not renderable.

    Signature sources are the format specifications: PNG 5.2, JFIF, GIF89a,
    RIFF/WEBP, ISO/IEC 14496-12 ftyp, EBML for WebM/Matroska, and ID3/MPEG frame
    sync for MP3.
    """
    if len(data) < 12:
        return None

    if _matches(data, b"\x89PNG\r\n\x1a\n"):
        return Media("image", "image/png", "PNG image")
    if _matches(data, b"\xff\xd8\xff"):
        return Media("image", "image/jpeg", "JPEG image")
    if _matches(data, b"GIF87a") or _matches(data, b"GIF89a"):
        return Media("image", "image/gif", "GIF image")
    if _matches(data, b"RIFF") and _matches(data, b"WEBP", 8):
        return Media("image", "image/webp", "WebP image")

    if _matches(data, b"RIFF") and _matches(data, b"WAVE", 8):
        return Media("audio", "audio/wav", "WAV audio")
    if _matches(data, b"ID3"):
        return Media("audio", "audio/mpeg", "MP3 audio")
    # A bare MPEG frame sync: 11 set bits, with a layer that is not the reserved
    # value. Checked narrowly, because two loose bytes would match a great deal.
    if data[0] == 0xFF and (data[1] & 0xE0) == 0xE0 and (data[1] & 0x06) != 0x00:
        return Media("audio", "audio/mpeg", "MP3 audio")

    if _matches(data, b"OggS"):
        # Ogg carries either; the codec identifier sits in the first page.
        head = data[:64]
        if b"theora" in head or b"video" in head:
            return Media("video", "video/ogg", "Ogg video")
        return Media("audio", "audio/ogg", "Ogg audio")

    if _matches(data, b"ftyp", 4):
        brand = data[8:12]
        if brand in (b"isom", b"iso2", b"mp41", b"mp42", b"avc1", b"MSNV",
                     b"dash", b"M4V ", b"mmp4"):
            return Media("video", "video/mp4", "MP4 video")
        if brand in (b"M4A ", b"M4B "):
            return Media("audio", "audio/mp4", "M4A audio")
        if brand == b"qt  ":
            return Media("video", "video/quicktime", "QuickTime video")
        return None            # an ftyp brand we do not recognise stays a download

    if _matches(data, b"\x1a\x45\xdf\xa3"):
        # EBML: WebM and Matroska share it. The doctype appears early.
        head = data[:256]
        if b"webm" in head:
            return Media("video", "video/webm", "WebM video")
        if b"matroska" in head:
            return Media("video", "video/x-matroska", "Matroska video")
        return None

    return None


def renderable(data: bytes) -> Media | None:
    """`sniff`, plus the size limit that decides inline versus download."""
    if len(data) > MAX_INLINE_BYTES:
        return None
    return sniff(data)


#: Sent with every media response. `sandbox` alone would block scripts; the rest
#: is defence in depth for a file that should not have reached here at all.
MEDIA_CSP = ("default-src 'none'; img-src 'self' data:; media-src 'self' data:; "
             "style-src 'unsafe-inline'; sandbox")
