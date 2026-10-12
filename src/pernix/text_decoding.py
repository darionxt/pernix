"""Decoding response bodies to text the way browsers would, with fallbacks for crawling.

The encoding is chosen in this order:

1. an explicit encoding;
2. a byte order mark;
3. the `charset` of the `Content-Type` header;
4. a `<meta>` tag or XML declaration in the first 4096 bytes;
5. UTF-8, if the body is valid UTF-8;
6. detection;
7. a default.

Encoding labels are mapped to Python codecs as the WHATWG Encoding Standard maps
them to encodings, so `gb2312` and `gbk` decode as GB18030 and `iso-8859-1` as
windows-1252. A declared encoding (header or document) is used only if the body
decodes with it strictly, an incomplete character cut off at the end aside;
otherwise the next step is tried. Undecodable bytes are replaced with U+FFFD.
"""

from __future__ import annotations

import codecs
import re
from dataclasses import dataclass
from typing import Literal

from charset_normalizer import from_bytes

__all__ = ["DecodedText", "EncodingSource", "codec_for", "decode_text"]

type EncodingSource = Literal["explicit", "bom", "header", "document", "utf-8", "detected", "default"]
"""Which step chose the encoding; `document` is a `<meta>` tag or an XML declaration."""

# Every label of the WHATWG Encoding Standard, grouped by the Python codec that decodes
# it. The `replacement` and `x-user-defined` encodings have no codec and are left out,
# so their labels count as unknown.
_LABELS_BY_CODEC = {
    "utf-8": "unicode-1-1-utf-8 unicode11utf8 unicode20utf8 utf-8 utf8 x-unicode20utf8",
    "cp866": "866 cp866 csibm866 ibm866",
    "iso8859-2": "csisolatin2 iso-8859-2 iso-ir-101 iso8859-2 iso88592 iso_8859-2 iso_8859-2:1987 l2 latin2",
    "iso8859-3": "csisolatin3 iso-8859-3 iso-ir-109 iso8859-3 iso88593 iso_8859-3 iso_8859-3:1988 l3 latin3",
    "iso8859-4": "csisolatin4 iso-8859-4 iso-ir-110 iso8859-4 iso88594 iso_8859-4 iso_8859-4:1988 l4 latin4",
    "iso8859-5": "csisolatincyrillic cyrillic iso-8859-5 iso-ir-144 iso8859-5 iso88595 iso_8859-5 iso_8859-5:1988",
    "iso8859-6": (
        "arabic asmo-708 csiso88596e csiso88596i csisolatinarabic ecma-114 iso-8859-6 iso-8859-6-e iso-8859-6-i"
        " iso-ir-127 iso8859-6 iso88596 iso_8859-6 iso_8859-6:1987"
    ),
    "iso8859-7": (
        "csisolatingreek ecma-118 elot_928 greek greek8 iso-8859-7 iso-ir-126 iso8859-7 iso88597 iso_8859-7"
        " iso_8859-7:1987 sun_eu_greek"
    ),
    "iso8859-8": (
        "csiso88598e csisolatinhebrew hebrew iso-8859-8 iso-8859-8-e iso-ir-138 iso8859-8 iso88598 iso_8859-8"
        " iso_8859-8:1988 visual csiso88598i iso-8859-8-i logical"
    ),
    "iso8859-10": "csisolatin6 iso-8859-10 iso-ir-157 iso8859-10 iso885910 l6 latin6",
    "iso8859-13": "iso-8859-13 iso8859-13 iso885913",
    "iso8859-14": "iso-8859-14 iso8859-14 iso885914",
    "iso8859-15": "csisolatin9 iso-8859-15 iso8859-15 iso885915 iso_8859-15 l9",
    "iso8859-16": "iso-8859-16",
    "koi8-r": "cskoi8r koi koi8 koi8-r koi8_r",
    "koi8-u": "koi8-ru koi8-u",
    "mac-roman": "csmacintosh mac macintosh x-mac-roman",
    "cp874": "dos-874 iso-8859-11 iso8859-11 iso885911 tis-620 windows-874",
    "cp1250": "cp1250 windows-1250 x-cp1250",
    "cp1251": "cp1251 windows-1251 x-cp1251",
    "cp1252": (
        "ansi_x3.4-1968 ascii cp1252 cp819 csisolatin1 ibm819 iso-8859-1 iso-ir-100 iso8859-1 iso88591 iso_8859-1"
        " iso_8859-1:1987 l1 latin1 us-ascii windows-1252 x-cp1252"
    ),
    "cp1253": "cp1253 windows-1253 x-cp1253",
    "cp1254": (
        "cp1254 csisolatin5 iso-8859-9 iso-ir-148 iso8859-9 iso88599 iso_8859-9 iso_8859-9:1989 l5 latin5"
        " windows-1254 x-cp1254"
    ),
    "cp1255": "cp1255 windows-1255 x-cp1255",
    "cp1256": "cp1256 windows-1256 x-cp1256",
    "cp1257": "cp1257 windows-1257 x-cp1257",
    "cp1258": "cp1258 windows-1258 x-cp1258",
    "mac-cyrillic": "x-mac-cyrillic x-mac-ukrainian",
    "gb18030": "chinese csgb2312 csiso58gb231280 gb2312 gb_2312 gb_2312-80 gbk iso-ir-58 x-gbk gb18030",
    "big5hkscs": "big5 big5-hkscs cn-big5 csbig5 x-x-big5",
    "euc_jp": "cseucpkdfmtjapanese euc-jp x-euc-jp",
    "iso2022_jp": "csiso2022jp iso-2022-jp",
    "cp932": "csshiftjis ms932 ms_kanji shift-jis shift_jis sjis windows-31j x-sjis",
    "cp949": (
        "cseuckr csksc56011987 euc-kr iso-ir-149 korean ks_c_5601-1987 ks_c_5601-1989 ksc5601 ksc_5601 windows-949"
    ),
    "utf-16-be": "unicodefffe utf-16be",
    "utf-16-le": "csunicode iso-10646-ucs-2 ucs-2 unicode unicodefeff utf-16 utf-16le",
}
_WEB_CODECS = {label: codec for codec, labels in _LABELS_BY_CODEC.items() for label in labels.split()}

_BOMS = ((codecs.BOM_UTF8, "utf-8"), (codecs.BOM_UTF16_BE, "utf-16-be"), (codecs.BOM_UTF16_LE, "utf-16-le"))
_PRESCAN_BYTES = 4096
_CHARSET_PARAMETER = re.compile(r"""(?:^|[\s;])charset\s*=\s*["']?\s*([^\s;,"']+)""", re.IGNORECASE)
_XML_DECLARATION = re.compile(r"""<\?xml\s[^>]*?encoding\s*=\s*["']\s*([^\s"']+)""")
_COMMENT = re.compile(r"<!--.*?(?:-->|$)", re.DOTALL)
_META_TAG = re.compile(r"<meta[\s/]([^>]*)", re.IGNORECASE)
_ATTRIBUTE = re.compile(r"""([^\s/=>"']+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>"']*)))?""")


@dataclass(frozen=True, slots=True)
class DecodedText:
    """A decoded body and how its encoding was chosen.

    Attributes:
        text: The decoded text, without a byte order mark.
        encoding: The Python codec that decoded it.
        source: The step that chose the encoding.
    """

    text: str
    encoding: str
    source: EncodingSource


def codec_for(label: str) -> str:
    """Return the Python codec for an encoding label, mapped as browsers map labels.

    Args:
        label: A WHATWG encoding label such as `"gb2312"`, or a Python codec name such
            as `"cp936"`; case and surrounding whitespace are ignored.

    Returns:
        The codec name, for example `"gb18030"` for both of the examples above.

    Raises:
        LookupError: The label names no text encoding.
    """
    key = label.strip().lower()
    if (codec := _WEB_CODECS.get(key)) is not None:
        return codec
    info = codecs.lookup(key)
    # The flag bytes.decode() itself checks, to reject codecs such as base64 and zlib.
    if not getattr(info, "_is_text_encoding", True):
        raise LookupError(f"{label!r} is not a text encoding")
    return _WEB_CODECS.get(info.name) or _WEB_CODECS.get(info.name.replace("_", "-")) or info.name


def decode_text(
    body: bytes,
    *,
    content_type: str | None = None,
    encoding: str | None = None,
    default_encoding: str = "utf-8",
) -> DecodedText:
    """Decode a response body, choosing the encoding as the module describes.

    Args:
        body: The response body.
        content_type: The `Content-Type` header value, if any.
        encoding: An explicit encoding, used whatever the body declares.
        default_encoding: The encoding used when no other step decides.

    Returns:
        The text, the codec used, and the step that chose it.

    Raises:
        LookupError: `encoding` or `default_encoding` names no text encoding.
    """
    if encoding is not None:
        codec = codec_for(encoding)
        return DecodedText(body.decode(codec, errors="replace").removeprefix("﻿"), codec, "explicit")
    for bom, codec in _BOMS:
        if body.startswith(bom):
            return DecodedText(body[len(bom) :].decode(codec, errors="replace"), codec, "bom")

    tried: set[str] = set()
    if (decoded := _try(body, _header_codec(content_type), "header", tried)) is not None:
        return decoded
    if (decoded := _try(body, _document_codec(body), "document", tried)) is not None:
        return decoded
    if (decoded := _try(body, "utf-8", "utf-8", tried)) is not None:
        return decoded
    if (codec := _detected_codec(body)) is not None:
        return DecodedText(body.decode(codec, errors="replace"), codec, "detected")
    codec = codec_for(default_encoding)
    return DecodedText(body.decode(codec, errors="replace"), codec, "default")


def _try(body: bytes, codec: str | None, source: EncodingSource, tried: set[str]) -> DecodedText | None:
    """Decode strictly with `codec`, unless it is unknown or already failed."""
    if codec is None or codec in tried:
        return None
    tried.add(codec)
    text = _decode_strictly(body, codec)
    return None if text is None else DecodedText(text, codec, source)


def _decode_strictly(body: bytes, codec: str) -> str | None:
    """Decode `body`, or return `None` if it has bytes `codec` cannot decode.

    An incomplete character at the very end, as left by a truncated transfer, is
    replaced rather than counted as a failure.
    """
    decoder = codecs.getincrementaldecoder(codec)()
    try:
        text = decoder.decode(body, final=False)
    except UnicodeDecodeError:
        return None
    pending: bytes = decoder.getstate()[0]
    return text + pending.decode(codec, errors="replace") if pending else text


def _header_codec(content_type: str | None) -> str | None:
    if content_type is None or (match := _CHARSET_PARAMETER.search(content_type)) is None:
        return None
    return _WEB_CODECS.get(match[1].lower())


def _document_codec(body: bytes) -> str | None:
    """Find the encoding declared by an XML declaration or a `<meta>` tag near the start."""
    head = body[:_PRESCAN_BYTES].decode("latin-1")
    if head.startswith("<?xml") and (match := _XML_DECLARATION.match(head)) is not None:
        return _declared(match[1])
    for tag in _META_TAG.finditer(_COMMENT.sub("", head)):
        attributes = _attributes(tag[1])
        label = attributes.get("charset")
        if label is None and attributes.get("http-equiv", "").strip().lower() == "content-type":
            match = _CHARSET_PARAMETER.search(attributes.get("content", ""))
            label = None if match is None else match[1]
        if label is not None and (codec := _declared(label)) is not None:
            return codec
    return None


def _attributes(tag: str) -> dict[str, str]:
    """Parse the attributes of a tag; as in HTML, the first of repeated attributes wins."""
    attributes: dict[str, str] = {}
    for name, double_quoted, single_quoted, unquoted in _ATTRIBUTE.findall(tag):
        attributes.setdefault(name.lower(), double_quoted or single_quoted or unquoted)
    return attributes


def _declared(label: str) -> str | None:
    """Map a label found in the document; such a document is ASCII-compatible, so never UTF-16."""
    codec = _WEB_CODECS.get(label.strip().lower())
    return "utf-8" if codec in ("utf-16-be", "utf-16-le") else codec


def _detected_codec(body: bytes) -> str | None:
    match = from_bytes(body).best()
    if match is None:
        return None
    try:
        return codec_for(match.encoding)
    except LookupError:
        return None
