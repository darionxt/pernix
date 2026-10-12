"""The decoding corpus: declared, conflicting, and undeclared encodings of real-world shapes."""

from __future__ import annotations

import codecs

import pytest

from pernix import text_decoding
from pernix.text_decoding import codec_for, decode_text

CHINESE = "中文内容\uff0c这是一个测试。"
GBK_ONLY = "朱镕基 喆 堃"
"""Characters in GBK but not in GB2312, which pages labelled gb2312 still use."""
GB18030_ONLY = "𠀀"
"""A character GB18030 encodes in four bytes and GBK cannot encode at all."""


def _page(body: str, *, head: str = "") -> str:
    return f"<html><head>{head}<title>测试</title></head><body>{body}</body></html>"


def _gbk_page(head: str = "") -> bytes:
    return _page(CHINESE * 20, head=head).encode("gbk")


@pytest.mark.parametrize(
    "content_type",
    [
        "text/html; charset=gbk",
        'text/html; charset="GBK"',
        "text/html; charset='gb2312'",
        "TEXT/HTML;CHARSET=GB2312",
        'text/html; charset = "gbk" ;',
        "text/html;charset=x-gbk, text/plain",
    ],
)
def test_header_charsets_are_parsed_leniently(content_type: str) -> None:
    decoded = decode_text(_gbk_page(), content_type=content_type)

    assert (decoded.encoding, decoded.source) == ("gb18030", "header")
    assert CHINESE in decoded.text


def test_gbk_only_characters_declared_as_gb2312_decode() -> None:
    body = _page(GBK_ONLY).encode("gbk")

    decoded = decode_text(body, content_type="text/html; charset=gb2312")

    assert (decoded.encoding, decoded.source) == ("gb18030", "header")
    assert GBK_ONLY in decoded.text


def test_gb18030_characters_declared_as_gbk_decode() -> None:
    body = _page(GB18030_ONLY + CHINESE).encode("gb18030")

    decoded = decode_text(body, content_type="text/html; charset=gbk")

    assert GB18030_ONLY + CHINESE in decoded.text


def test_a_correct_header_wins_over_a_wrong_meta() -> None:
    body = _gbk_page(head='<meta charset="utf-8">')

    decoded = decode_text(body, content_type="text/html; charset=gbk")

    assert (decoded.encoding, decoded.source) == ("gb18030", "header")


def test_a_wrong_header_falls_through_to_the_meta() -> None:
    body = _gbk_page(head='<meta http-equiv="Content-Type" content="text/html; charset=gb2312">')

    decoded = decode_text(body, content_type="text/html; charset=utf-8")

    assert (decoded.encoding, decoded.source) == ("gb18030", "document")
    assert CHINESE in decoded.text


def test_a_bom_wins_over_the_header_and_is_removed() -> None:
    body = codecs.BOM_UTF8 + _page(CHINESE).encode()

    decoded = decode_text(body, content_type="text/html; charset=gbk")

    assert (decoded.encoding, decoded.source) == ("utf-8", "bom")
    assert decoded.text.startswith("<html>")


def test_utf16_boms_are_honored() -> None:
    body = codecs.BOM_UTF16_LE + _page(CHINESE).encode("utf-16-le")

    decoded = decode_text(body, content_type="text/html; charset=utf-8")

    assert (decoded.encoding, decoded.source) == ("utf-16-le", "bom")
    assert decoded.text == _page(CHINESE)


@pytest.mark.parametrize(
    "head",
    [
        '<meta charset="gbk">',
        "<meta charset=gbk>",
        "<META CHARSET='GB2312'/>",
        '<meta content="text/html; charset=gbk" http-equiv="content-type">',
        '<meta name="viewport" content="width=device-width"><meta charset="unknown-label"><meta charset="gbk">',
    ],
)
def test_meta_declarations(head: str) -> None:
    decoded = decode_text(_gbk_page(head=head))

    assert (decoded.encoding, decoded.source) == ("gb18030", "document")


def test_the_xml_declaration_is_a_document_declaration() -> None:
    body = f'<?xml version="1.0" encoding="GBK"?><rss><title>{CHINESE}</title></rss>'.encode("gbk")

    decoded = decode_text(body, content_type="application/rss+xml")

    assert (decoded.encoding, decoded.source) == ("gb18030", "document")


def test_meta_tags_inside_comments_are_ignored() -> None:
    decoded = decode_text(_gbk_page(head='<!-- <meta charset="gbk"> -->'))

    assert (decoded.encoding, decoded.source) == ("gb18030", "detected")


def test_meta_text_inside_attribute_values_is_not_a_declaration() -> None:
    page = '<div data-example="<meta charset=windows-1252>">café</div>'

    decoded = decode_text(page.encode())

    assert decoded == text_decoding.DecodedText(page, "utf-8", "utf-8")


def test_quoted_greater_than_signs_do_not_end_a_tag_early() -> None:
    body = _gbk_page(head='<link title="a > b"><meta charset="gbk">')

    assert decode_text(body).source == "document"
    assert decode_text(_gbk_page(head='<link title="<meta charset=gbk>">')).source == "detected"


def test_only_the_first_4096_bytes_are_scanned_for_a_meta() -> None:
    body = _page(CHINESE * 10, head=" " * 4096 + '<meta charset="gbk">').encode("gbk")

    decoded = decode_text(body)

    assert decoded.source == "detected"
    assert decoded.encoding == "gb18030"


def test_meta_declarations_of_utf16_mean_utf8() -> None:
    page = _page(CHINESE, head='<meta charset="utf-16">')

    decoded = decode_text(page.encode())

    assert decoded == text_decoding.DecodedText(page, "utf-8", "document")


def test_undeclared_utf8_is_recognized_before_detection() -> None:
    decoded = decode_text(_page(CHINESE).encode())

    assert (decoded.encoding, decoded.source) == ("utf-8", "utf-8")


def test_undeclared_gbk_is_detected() -> None:
    decoded = decode_text(_gbk_page())

    assert (decoded.encoding, decoded.source) == ("gb18030", "detected")
    assert CHINESE in decoded.text


def test_a_truncated_last_character_does_not_fail_strict_decoding() -> None:
    body = "<p>中文".encode() + "中".encode()[:2]

    assert decode_text(body) == text_decoding.DecodedText("<p>中文\ufffd", "utf-8", "utf-8")
    assert decode_text(body, content_type="text/html; charset=utf-8").source == "header"


def test_a_truncated_character_of_a_stateful_encoding_is_replaced() -> None:
    body = "日本".encode("iso2022_jp")[:-4]  # b"\x1b$BF|K": cut inside 本, still in JIS mode

    decoded = decode_text(body, content_type="text/plain; charset=iso-2022-jp")

    assert (decoded.source, decoded.text) == ("header", "日\ufffd")


def test_unknown_header_labels_are_ignored() -> None:
    decoded = decode_text(_page(CHINESE).encode(), content_type="text/html; charset=x-made-up")

    assert decoded.source == "utf-8"


def test_the_default_applies_when_detection_fails() -> None:
    body = _page(CHINESE * 20).encode()
    body = body[:100] + b"\xff" + body[100:]

    decoded = decode_text(body)

    assert (decoded.encoding, decoded.source) == ("utf-8", "default")
    assert "\ufffd" in decoded.text
    assert CHINESE in decoded.text
    assert decode_text(body, default_encoding="latin1").encoding == "cp1252"


def test_an_explicit_encoding_wins_over_everything() -> None:
    body = codecs.BOM_UTF8 + "naïve".encode()

    decoded = decode_text(body, content_type="text/html; charset=gbk", encoding="UTF8")

    assert decoded == text_decoding.DecodedText("naïve", "utf-8", "explicit")


def test_labels_map_as_browsers_map_them() -> None:
    assert decode_text(b"\x80", content_type="text/plain; charset=iso-8859-1").text == "€"
    assert decode_text("①".encode("cp932"), content_type="text/plain; charset=shift_jis").text == "①"
    assert [codec_for(label) for label in ("gb2312", "GBK", " gb18030 ", "big5", "euc-kr", "ascii")] == [
        "gb18030",
        "gb18030",
        "gb18030",
        "big5hkscs",
        "cp949",
        "cp1252",
    ]


def test_python_codec_names_are_mapped_too() -> None:
    assert [codec_for(name) for name in ("cp936", "latin_1", "euc_kr", "utf_8", "koi8_u")] == [
        "gb18030",
        "cp1252",
        "cp949",
        "utf-8",
        "koi8-u",
    ]


@pytest.mark.parametrize("label", ["made-up", "base64", "zlib", ""])
def test_labels_that_name_no_text_encoding_are_rejected(label: str) -> None:
    with pytest.raises(LookupError):
        codec_for(label)


def test_every_mapped_codec_exists() -> None:
    for codec in text_decoding._LABELS_BY_CODEC:  # pyright: ignore[reportPrivateUsage]
        assert codecs.lookup(codec).name == codec
