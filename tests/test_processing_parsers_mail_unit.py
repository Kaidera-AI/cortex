"""Unit tests for the EML/mbox parser adapters (parser.mail@1)."""

from __future__ import annotations

import hashlib
from uuid import UUID

from cortex_v2.processing.contracts import (
    Outcome,
    Parser,
    ParserLimits,
    SourceRef,
)
from cortex_v2.processing.keys import text_sha256
from cortex_v2.processing.parsers.mail import (
    DEPENDENCIES,
    PARSERS,
    EmlParser,
    MboxParser,
)

SCOPE_ID = UUID("11111111-1111-4111-8111-111111111111")
CONTENT_ID = UUID("22222222-2222-4222-8222-222222222222")

EML_RAW = (
    "From: alice@example.com\n"
    "To: bob@example.com\n"
    "Subject: Q3 report\n"
    "Date: Mon, 1 Jan 2024 00:00:00 +0000\n"
    "Message-ID: <m1@example.com>\n"
    "MIME-Version: 1.0\n"
    'Content-Type: multipart/mixed; boundary="BB"\n'
    "\n"
    "--BB\n"
    'Content-Type: text/plain; charset="utf-8"\n'
    "Content-Transfer-Encoding: base64\n"
    "\n"
    "SGVsbG8gcGxhaW4gYm9keS4=\n"
    "--BB\n"
    'Content-Type: text/html; charset="utf-8"\n'
    "\n"
    "<p>Hello html body.</p>\n"
    "--BB\n"
    'Content-Type: application/pdf; name="doc.pdf"\n'
    'Content-Disposition: attachment; filename="doc.pdf"\n'
    "Content-Transfer-Encoding: base64\n"
    "\n"
    "JVBERi0xLjQK\n"
    "--BB--\n"
)

NESTED_RAW = (
    "From: outer@example.com\n"
    "To: inner@example.com\n"
    "Subject: outer\n"
    "Date: Mon, 1 Jan 2024 00:00:00 +0000\n"
    "Message-ID: <o1@example.com>\n"
    "MIME-Version: 1.0\n"
    'Content-Type: multipart/mixed; boundary="OB"\n'
    "\n"
    "--OB\n"
    "Content-Type: text/plain\n"
    "\n"
    "outer text\n"
    "--OB\n"
    "Content-Type: message/rfc822\n"
    "\n"
    "Subject: middle\n"
    "Content-Type: message/rfc822\n"
    "\n"
    "Subject: inner\n"
    "Content-Type: text/plain\n"
    "\n"
    "inner text\n"
    "--OB--\n"
)

BAD_CHARSET_RAW = (
    "From: a@example.com\n"
    "To: b@example.com\n"
    "Subject: bad charset\n"
    "Date: Mon, 1 Jan 2024 00:00:00 +0000\n"
    "Message-ID: <m2@example.com>\n"
    "MIME-Version: 1.0\n"
    'Content-Type: text/plain; charset="x-no-such-charset"\n'
    "\n"
    "body text\n"
)


def make_source(
    body_text: str = "",
    *,
    media_type: str = "message/rfc822",
    body_bytes: bytes | None = None,
) -> SourceRef:
    return SourceRef(
        scope_id=SCOPE_ID,
        content_id=CONTENT_ID,
        revision=1,
        content_class="artifact",
        media_type=media_type,
        body_text=body_text,
        content_hash=text_sha256(body_text),
        body_bytes=body_bytes,
    )


def mbox_entry(index: int) -> str:
    return (
        f"From: sender{index}@example.com\n"
        f"To: rcpt@example.com\n"
        f"Subject: msg {index}\n"
        f"Date: Mon, 1 Jan 2024 00:00:0{index} +0000\n"
        f"Message-ID: <m{index}@example.com>\n"
        f'Content-Type: text/plain; charset="utf-8"\n'
        f"\n"
        f"Body of message {index}.\n"
    )


def mbox_bytes(count: int = 3) -> bytes:
    data = b""
    for index in range(count):
        data += f"From sender{index}@example.com Mon Jan  1 00:00:00 2024\n".encode()
        data += mbox_entry(index).encode()
        data += b"\n"
    return data


def test_module_exposes_parsers_without_dependencies() -> None:
    assert set(PARSERS) == {"eml", "mbox"}
    assert isinstance(PARSERS["eml"], EmlParser)
    assert isinstance(PARSERS["mbox"], MboxParser)
    assert PARSERS["eml"].parser_id == "parser.mail@1"
    assert PARSERS["mbox"].parser_id == "parser.mail@1"
    assert PARSERS["eml"].format_ids == ("eml",)
    assert PARSERS["mbox"].format_ids == ("mbox",)
    assert isinstance(PARSERS["eml"], Parser)
    assert DEPENDENCIES == {}


def test_registry_loads_mail_adapter() -> None:
    from cortex_v2.processing import parsers as registry

    assert registry.parser_for("eml") is PARSERS["eml"]
    assert registry.parser_for("mbox") is PARSERS["mbox"]
    assert registry.declared_dependency("eml") is None


def test_eml_headers_and_text_parts_with_attachment_inventory() -> None:
    result = EmlParser().parse(make_source(EML_RAW), ParserLimits())
    assert result.outcome is Outcome.OK
    assert result.executor == "parser.mail@1"
    assert result.format_id is None
    assert result.detected_by is None
    assert result.warnings == ("attachment_not_extracted",)
    headers, plain, html = result.blocks
    assert headers.block_kind == "headers"
    expected_headers = "\n".join(EML_RAW.split("\n")[:5])
    assert headers.text == expected_headers
    assert headers.span.start == 0
    assert headers.span.end == len(expected_headers)
    assert EML_RAW[headers.span.start : headers.span.end] == headers.text
    assert (headers.span.line_start, headers.span.line_end) == (1, 5)
    assert headers.span.detail == {
        "attachments": [
            {
                "filename": "doc.pdf",
                "content_type": "application/pdf",
                "size": 9,
                "sha256": hashlib.sha256(b"%PDF-1.4\n").hexdigest(),
            }
        ]
    }
    assert (plain.block_kind, html.block_kind) == ("part", "part")
    assert plain.span.part == "1"
    assert plain.text == "Hello plain body."
    assert (plain.span.start, plain.span.end) == (0, len("Hello plain body."))
    assert html.span.part == "2"
    assert html.text == "<p>Hello html body.</p>"
    assert html.span.detail["content_type"] == "text/html"
    assert all("%PDF" not in block.text for block in result.blocks)


def test_eml_nested_rfc822_recurses_with_dotted_paths() -> None:
    result = EmlParser().parse(make_source(NESTED_RAW), ParserLimits())
    assert result.outcome is Outcome.OK
    parts = [block for block in result.blocks if block.block_kind == "part"]
    assert [(block.span.part, block.text) for block in parts] == [
        ("1", "outer text"),
        ("2", "inner text"),
    ]


def test_eml_nested_rfc822_stops_at_max_depth() -> None:
    result = EmlParser().parse(make_source(NESTED_RAW), ParserLimits(max_depth=1))
    assert result.outcome is Outcome.OK
    assert "depth_limit_reached" in result.warnings
    parts = [block for block in result.blocks if block.block_kind == "part"]
    assert [(block.span.part, block.text) for block in parts] == [
        ("1", "outer text"),
    ]


def test_eml_undecodable_part_is_warned_not_fatal() -> None:
    result = EmlParser().parse(make_source(BAD_CHARSET_RAW), ParserLimits())
    assert result.outcome is Outcome.OK
    assert result.warnings == ("undecodable_part_1",)
    assert [block.block_kind for block in result.blocks] == ["headers"]


def test_eml_attachment_inventory_is_bounded() -> None:
    raw = (
        "Subject: two files\n"
        "MIME-Version: 1.0\n"
        'Content-Type: multipart/mixed; boundary="TB"\n'
        "\n"
        "--TB\n"
        "Content-Type: text/plain\n"
        "\n"
        "body\n"
        "--TB\n"
        'Content-Type: application/octet-stream; name="one.bin"\n'
        "Content-Transfer-Encoding: base64\n"
        "\n"
        "AA==\n"
        "--TB\n"
        'Content-Type: application/octet-stream; name="two.bin"\n'
        "Content-Transfer-Encoding: base64\n"
        "\n"
        "AQ==\n"
        "--TB--\n"
    )
    result = EmlParser().parse(make_source(raw), ParserLimits(max_attachments=1))
    assert result.outcome is Outcome.OK
    assert result.truncated is True
    assert "attachment_limit_reached" in result.warnings
    assert "attachment_not_extracted" in result.warnings
    headers = result.blocks[0]
    inventory = headers.span.detail["attachments"]
    assert [entry["filename"] for entry in inventory] == ["one.bin"]
    assert inventory[0]["size"] == 1
    assert inventory[0]["sha256"] == hashlib.sha256(b"\x00").hexdigest()


def test_eml_without_content_is_input_empty() -> None:
    result = EmlParser().parse(make_source(""), ParserLimits())
    assert result.outcome is Outcome.INPUT_EMPTY


def test_eml_nul_byte_is_input_rejected() -> None:
    result = EmlParser().parse(
        make_source("Subject: x\n\nbody\u0000\n"), ParserLimits()
    )
    assert result.outcome is Outcome.INPUT_REJECTED


def test_eml_oversize_bytes_are_quota_exceeded() -> None:
    result = EmlParser().parse(
        make_source("", body_bytes=b"Subject: x\n\n" + b"y" * 64),
        ParserLimits(max_input_bytes=32),
    )
    assert result.outcome is Outcome.QUOTA_EXCEEDED


def test_eml_parses_from_body_bytes_when_present() -> None:
    result = EmlParser().parse(
        make_source("", body_bytes=EML_RAW.encode()), ParserLimits()
    )
    assert result.outcome is Outcome.OK
    headers = result.blocks[0]
    assert headers.block_kind == "headers"
    assert "Subject: Q3 report" in headers.text
    assert len([b for b in result.blocks if b.block_kind == "part"]) == 2


def test_mbox_yields_one_message_block_per_entry() -> None:
    data = mbox_bytes(3)
    result = MboxParser().parse(
        make_source("", media_type="application/mbox", body_bytes=data), ParserLimits()
    )
    assert result.outcome is Outcome.OK
    assert result.executor == "parser.mail@1"
    messages = [block for block in result.blocks if block.block_kind == "message"]
    parts = [block for block in result.blocks if block.block_kind == "part"]
    assert [block.span.row for block in messages] == [0, 1, 2]
    assert len(parts) == 3
    for index, (message, part) in enumerate(
        ((messages[i], parts[i]) for i in range(3))
    ):
        expected_headers = "\n".join(mbox_entry(index).split("\n")[:5])
        assert message.text == expected_headers
        assert message.span.row == index
        entry = mbox_entry(index)
        assert entry[message.span.start : message.span.end] == message.text
        assert part.text == f"Body of message {index}.\n"
        assert part.span.row == index
        assert part.span.part == "1"
        assert (part.span.start, part.span.end) == (0, len(part.text))


def test_mbox_falls_back_to_body_text() -> None:
    result = MboxParser().parse(
        make_source(mbox_bytes(2).decode(), media_type="application/mbox"),
        ParserLimits(),
    )
    assert result.outcome is Outcome.OK
    messages = [block for block in result.blocks if block.block_kind == "message"]
    assert [block.span.row for block in messages] == [0, 1]


def test_mbox_row_limit_truncates() -> None:
    result = MboxParser().parse(
        make_source("", media_type="application/mbox", body_bytes=mbox_bytes(3)),
        ParserLimits(max_rows=2),
    )
    assert result.outcome is Outcome.OK
    messages = [block for block in result.blocks if block.block_kind == "message"]
    assert [block.span.row for block in messages] == [0, 1]
    assert result.truncated is True
    assert "row_limit_reached" in result.warnings


def test_mbox_without_messages_is_input_empty() -> None:
    empty = MboxParser().parse(
        make_source("", media_type="application/mbox", body_bytes=b""), ParserLimits()
    )
    assert empty.outcome is Outcome.INPUT_EMPTY
    junk = MboxParser().parse(
        make_source(
            "", media_type="application/mbox", body_bytes=b"no from lines here\n"
        ),
        ParserLimits(),
    )
    assert junk.outcome is Outcome.INPUT_EMPTY


def test_mbox_nul_byte_in_text_is_input_rejected() -> None:
    result = MboxParser().parse(
        make_source("Subject: x\n\nbody\u0000\n", media_type="application/mbox"),
        ParserLimits(),
    )
    assert result.outcome is Outcome.INPUT_REJECTED


def test_mbox_oversize_bytes_are_quota_exceeded() -> None:
    result = MboxParser().parse(
        make_source("", media_type="application/mbox", body_bytes=mbox_bytes(3)),
        ParserLimits(max_input_bytes=32),
    )
    assert result.outcome is Outcome.QUOTA_EXCEEDED
