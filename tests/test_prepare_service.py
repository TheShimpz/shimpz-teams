"""Per-file and per-message attachment admission around the untrusted helper (ADR-0093)."""

from __future__ import annotations

import base64
import codecs
import contextlib
import hashlib
import unittest
from unittest import mock

from prepare import limits, service, worker
from tests import prepare_fixtures


class _InProcessHelper:
    """The real worker answering in process, counting how often a helper segment is opened."""

    def __init__(self, answers: dict[str, dict[str, object]] | None = None) -> None:
        self.opened = 0
        self.calls: list[str] = []
        self.answers = answers or {}

    @contextlib.contextmanager
    def __call__(self):
        self.opened += 1
        yield self

    def prepare(self, kind: str, data: bytes) -> dict[str, object]:
        self.calls.append(kind)
        if kind in self.answers:
            return self.answers[kind]
        return worker.answer(worker.encode_request(kind, data))


def _file(name: str, data: bytes, file_id: str = "0" * 32) -> service.StoredFile:
    return service.StoredFile(file_id, name, len(data), hashlib.sha256(data).hexdigest(), lambda: data)


class PreparationServiceTests(unittest.TestCase):
    def test_text_is_read_in_the_controller_without_a_helper(self) -> None:
        helper = _InProcessHelper()
        prepared = service.prepare_attachments([_file("notes.md", "# Ação\nItem".encode())], helper)
        self.assertEqual(helper.opened, 0)
        self.assertEqual(prepared[0].media_type, "text/markdown")
        self.assertEqual(prepared[0].content, {"type": "text", "text": "# Ação\nItem", "pdf": False})
        self.assertEqual(set(prepared[0].wire()), {"id", "name", "media_type", "size", "sha256", "content"})

    def test_one_helper_segment_serves_every_image_and_pdf(self) -> None:
        helper = _InProcessHelper()
        files = [
            _file("a.png", prepare_fixtures.image("PNG", (40, 20)), "1" * 32),
            _file("b.pdf", prepare_fixtures.text_pdf("Report body"), "2" * 32),
            _file("c.zip", b"PK\x03\x04binary", "3" * 32),
        ]
        prepared = service.prepare_attachments(files, helper)
        self.assertEqual(helper.opened, 1)
        self.assertEqual(helper.calls, ["image", "pdf"])
        image, pdf, opaque = prepared
        self.assertEqual(image.content["type"], "image")
        self.assertEqual(image.content["sha256"], hashlib.sha256(base64.b64decode(image.content["base64"])).hexdigest())
        self.assertEqual(pdf.content, {"type": "text", "text": "Report body", "pdf": True})
        self.assertEqual(
            (opaque.media_type, opaque.content),
            ("application/octet-stream", {"type": "opaque", "reason": "unsupported"}),
        )

    def test_files_over_their_source_ceiling_are_opaque_without_reaching_the_helper(self) -> None:
        helper = _InProcessHelper()
        big_text = _file("big.txt", b"a" * (limits.MAX_TEXT_SOURCE_BYTES + 1))
        long_text = _file("long.txt", b"a" * (limits.MAX_TEXT_CHARACTERS + 1))
        prepared = service.prepare_attachments([big_text, long_text], helper)
        self.assertEqual([item.content for item in prepared], [{"type": "opaque", "reason": "too_large"}] * 2)
        self.assertEqual(helper.opened, 0)

    def test_an_original_beyond_every_source_ceiling_is_never_read(self) -> None:
        def unreadable() -> bytes:
            raise AssertionError("the oversized original was read")

        huge = service.StoredFile("d" * 32, "video.mp4", limits.MAX_PDF_BYTES + 1, "0" * 64, unreadable)
        prepared = service.prepare_attachments([huge], _InProcessHelper())
        self.assertEqual(
            (prepared[0].media_type, prepared[0].content),
            ("application/octet-stream", {"type": "opaque", "reason": "too_large"}),
        )

    def test_oversized_text_is_refused_without_decoding_past_its_ceiling(self) -> None:
        big = b"a" * (8 * 1024 * 1024)
        with mock.patch.object(service.detect.codecs, "getincrementaldecoder", wraps=codecs.getincrementaldecoder) as d:
            prepared = service.prepare_attachments([_file("big.txt", big)], _InProcessHelper())
        self.assertEqual(prepared[0].content, {"type": "opaque", "reason": "too_large"})
        self.assertEqual(prepared[0].media_type, "text/plain")
        d.assert_called_once()
        self.assertIsNone(service.detect.decode_text(big))
        # A multi-byte character split at the ceiling still classifies the prefix as text.
        split = b"a" * (limits.MAX_TEXT_SOURCE_BYTES - 1) + "é".encode() + b"tail"
        self.assertEqual(service.detect.detect("a.txt", split).kind, service.detect.TEXT)

    def test_untrusted_helper_answers_are_revalidated(self) -> None:
        png = prepare_fixtures.image("PNG", (8, 8))
        bogus = (
            {"type": "image", "media_type": "image/gif", "width": 8, "height": 8, "base64": "AAAA"},
            {"type": "image", "media_type": "image/png", "width": 4000, "height": 8, "base64": "AAAA"},
            {"type": "image", "media_type": "image/png", "width": 8, "height": 8, "base64": "not base64"},
            {
                "type": "image",
                "media_type": "image/jpeg",
                "width": 8,
                "height": 8,
                "base64": base64.b64encode(png).decode(),
            },
            {"type": "image", "media_type": "image/png", "width": 8, "height": 8, "base64": "", "extra": 1},
            {"type": "opaque", "reason": "made_up"},
            {"type": "text", "text": "an image answered with text"},
        )
        for answer in bogus:
            with self.subTest(answer=answer):
                prepared = service.prepare_attachments([_file("a.png", png)], _InProcessHelper({"image": answer}))
                self.assertEqual(prepared[0].content, {"type": "opaque", "reason": "unreadable"})
        prepared = service.prepare_attachments(
            [_file("a.pdf", prepare_fixtures.text_pdf("x"))],
            _InProcessHelper({"pdf": {"type": "text", "text": "x" * (limits.MAX_TEXT_CHARACTERS + 1)}}),
        )
        self.assertEqual(prepared[0].content, {"type": "opaque", "reason": "too_large"})

    def test_per_message_ceilings_refuse_the_whole_message(self) -> None:
        image = prepare_fixtures.image("PNG", (8, 8))
        five_images = [_file(f"{index}.png", image, f"{index:032x}") for index in range(5)]
        with self.assertRaises(service.AttachmentLimitError) as refused:
            service.prepare_attachments(five_images, _InProcessHelper())
        self.assertEqual(refused.exception.code, "attachments-too-many-images")

        text = "a" * limits.MAX_TEXT_CHARACTERS
        five_texts = [_file(f"{index}.txt", text.encode(), f"{index:032x}") for index in range(5)]
        with self.assertRaises(service.AttachmentLimitError) as refused:
            service.prepare_attachments(five_texts, _InProcessHelper())
        self.assertEqual(refused.exception.code, "attachments-too-much-text")

        huge = service.StoredFile("f" * 32, "huge.bin", limits.MAX_SELECTED_ORIGINAL_BYTES + 1, "0" * 64, bytes)
        with self.assertRaises(service.AttachmentLimitError) as refused:
            service.prepare_attachments([huge], _InProcessHelper())
        self.assertEqual(refused.exception.code, "attachments-too-large")

        nine = [_file(f"{index}.txt", b"x", f"{index:032x}") for index in range(9)]
        with self.assertRaises(service.AttachmentLimitError) as refused:
            service.prepare_attachments(nine, _InProcessHelper())
        self.assertEqual(refused.exception.code, "attachments-too-many")

    def test_the_encoded_field_stays_within_its_own_ceiling(self) -> None:
        wide = service.Attachment(
            "0" * 32,
            "a",
            "image/png",
            1,
            "0" * 64,
            {"type": "image", "base64": "A" * (limits.MAX_ATTACHMENTS_FIELD_BYTES)},
        )
        with self.assertRaises(service.AttachmentLimitError) as refused:
            service.admit_message([wide])
        self.assertEqual(refused.exception.code, "attachments-too-large")

    def test_helper_answers_outside_their_shapes_become_closed_opaque_reasons(self) -> None:
        self.assertEqual(service._text(None, pdf=False), {"type": "opaque", "reason": "unreadable"})
        self.assertEqual(service._text("  ", pdf=True), {"type": "opaque", "reason": "no_text"})
        self.assertEqual(
            service._pdf_answer({"type": "opaque", "reason": "encrypted"}), {"type": "opaque", "reason": "encrypted"}
        )
        self.assertEqual(
            service._opaque_answer({"type": "opaque", "reason": "maybe"}), {"type": "opaque", "reason": "unreadable"}
        )
        self.assertIsNone(service._decoded(7))
        self.assertIsNone(service._decoded("YWJ="))


if __name__ == "__main__":
    unittest.main()
