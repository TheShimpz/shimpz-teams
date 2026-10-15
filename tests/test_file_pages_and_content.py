"""A PDF page's preview and a file's original download: rendering, admission, lifecycle, and routes (ADR-0093)."""

import base64
import contextlib
import hashlib
import io
import math
import os
import tempfile
import threading
import types
import unittest
from http import HTTPStatus
from unittest import mock

import pypdfium2 as pdfium
from PIL import Image

from core.http import strict as strict_http
from local import app as local_app
from local import authority
from local import lifecycle as local_lifecycle
from local import prepare as local_prepare
from local.errors import ApiProblemError
from local.http import audit as http_audit
from prepare import image, limits, page, worker
from prepare import service as preparation
from protocol.http.v1 import content as http_content
from protocol.http.v1 import preview as http_preview
from tests import prepare_fixtures
from tests import test_file_preview as preview_tests
from tests.test_file_preview import WorkerHelper, bare_controller, get_handler, stored_file, supervisor_evidence


def _page(data: bytes, number: int) -> dict[str, object]:
    return worker.answer(worker.encode_request("pdf-page", data, number))


class PageRenderingTests(unittest.TestCase):
    def test_each_page_becomes_a_bounded_derivative_with_the_page_count(self) -> None:
        pdf = prepare_fixtures.text_pdf("First page", "Second page")
        for number in (1, 2):
            with self.subTest(page=number):
                answer = _page(pdf, number)
                self.assertEqual(answer["page_count"], 2)
                # A letter page fits the derivative's pixel count, its proportions kept.
                self.assertEqual((answer["media_type"], answer["width"], answer["height"]), ("image/png", 962, 1246))
                encoded = base64.b64decode(answer["base64"])
                self.assertLessEqual(len(encoded), limits.MAX_DERIVATIVE_BYTES)
                with Image.open(io.BytesIO(encoded)) as drawn:
                    # The text is drawn: the page is not blank white.
                    self.assertLess(drawn.convert("L").getextrema()[0], 128)
        self.assertEqual(_page(pdf, 3), {"type": "opaque", "reason": "unsupported"})

    def test_encrypted_oversized_and_unreadable_documents_have_no_page(self) -> None:
        # PDFium's last error is per process: the pageless document goes first, as each helper process reads one file.
        self.assertEqual(_page(prepare_fixtures.pageless_pdf(), 1), {"type": "opaque", "reason": "unreadable"})
        self.assertEqual(_page(b"%PDF-1.7 broken", 1), {"type": "opaque", "reason": "unreadable"})
        self.assertEqual(_page(prepare_fixtures.encrypted_pdf(), 1), {"type": "opaque", "reason": "encrypted"})
        # A document any reader opens without a password is still refused for its security handler.
        self.assertEqual(_page(prepare_fixtures.openly_encrypted_pdf(), 1), {"type": "opaque", "reason": "encrypted"})
        self.assertEqual(
            _page(prepare_fixtures.blank_pdf(limits.MAX_PDF_PAGES + 1), 1), {"type": "opaque", "reason": "too_large"}
        )

    def test_the_bitmap_is_sized_before_it_is_drawn_within_the_derivative(self) -> None:
        for width, height in ((612, 792), (14_400, 14_400), (1, 14_400), (3, 2), (200_000, 0.5), (0.01, 0.01)):
            with self.subTest(width=width, height=height):
                size = page.bitmap_size(width, height)
                self.assertLessEqual(max(size), limits.DERIVATIVE_LONG_EDGE)
                self.assertLessEqual(size[0] * size[1], limits.DERIVATIVE_PIXELS)
                self.assertGreaterEqual(min(size), 1)
        # A tiny page is drawn at most MAX_PAGE_SCALE times larger.
        self.assertEqual(page.bitmap_size(10, 20), (40, 80))
        for width, height in ((0, 792), (612, -1), (math.inf, 792), (612, math.nan)):
            with self.subTest(width=width, height=height), self.assertRaises(page.PageRefusedError) as caught:
                page.bitmap_size(width, height)
            self.assertEqual(caught.exception.reason, "unreadable")

    def test_a_bitmap_that_cannot_be_allocated_refuses_the_page(self) -> None:
        pdf = prepare_fixtures.text_pdf("Page")
        with mock.patch.object(page.pdfium_raw, "FPDFBitmap_Create", return_value=None):
            self.assertEqual(_page(pdf, 1), {"type": "opaque", "reason": "unreadable"})

    def test_a_page_too_detailed_for_a_png_falls_back_to_a_jpeg(self) -> None:
        noise = Image.frombytes("RGB", (600, 600), os.urandom(600 * 600 * 3))
        self.assertEqual(image.encode(noise, lossless=True)["media_type"], "image/jpeg")
        self.assertEqual(image.encode(Image.new("RGB", (40, 40)), lossless=True)["media_type"], "image/png")

    def test_the_build_runs_no_script_or_form_engine(self) -> None:
        self.assertEqual(pdfium.PDFIUM_INFO.flags, ())

    def test_a_page_request_names_a_page_within_the_ceiling_and_no_other_kind_does(self) -> None:
        pdf = prepare_fixtures.text_pdf("Page")
        unreadable = {"type": "opaque", "reason": "unreadable"}
        for number in (0, limits.MAX_PDF_PAGES + 1, True, "1"):
            with self.subTest(page=number):
                self.assertEqual(_page(pdf, number), unreadable)
        self.assertEqual(worker.answer(b'{"kind":"pdf-page","size":3}\nabc'), unreadable)
        self.assertEqual(worker.answer(b'{"kind":"pdf","size":3,"page":1}\nabc'), unreadable)
        self.assertEqual(worker.answer(b'{"size":3}\nabc'), unreadable)


class PageRenditionTests(unittest.TestCase):
    def test_a_stored_pdf_page_becomes_a_rendition_with_its_page_count(self) -> None:
        pdf = prepare_fixtures.text_pdf("One", "Two", "Three")
        rendition = preparation.prepare_rendition(
            stored_file("brief.pdf", pdf), WorkerHelper(), contextlib.nullcontext(), page=2
        )
        self.assertEqual((rendition.media_type, rendition.page_count), ("image/png", 3))
        self.assertTrue(rendition.data.startswith(b"\x89PNG"))
        # An image's rendition has no page count, and a PDF has no image rendition.
        png = prepare_fixtures.image("PNG", (4, 4))
        self.assertIsNone(
            preparation.prepare_rendition(
                stored_file("a.png", png), WorkerHelper(), contextlib.nullcontext()
            ).page_count
        )
        self.assertIsNone(
            preparation.prepare_rendition(stored_file("brief.pdf", pdf), WorkerHelper(), contextlib.nullcontext())
        )

    def test_only_a_pdf_within_its_ceiling_is_rendered_and_the_answer_is_validated(self) -> None:
        none = contextlib.nullcontext()
        reads: list[str] = []
        oversized = stored_file("big.pdf", b"%PDF", size=limits.MAX_PDF_BYTES + 1, reads=reads)
        self.assertIsNone(preparation.prepare_rendition(oversized, WorkerHelper(), none, page=1))
        self.assertEqual(reads, [])
        png = prepare_fixtures.image("PNG", (4, 4))
        self.assertIsNone(preparation.prepare_rendition(stored_file("a.pdf", png), WorkerHelper(), none, page=1))
        honest = {
            "type": "image",
            "media_type": "image/png",
            "width": 4,
            "height": 4,
            "base64": base64.b64encode(png).decode("ascii"),
        }
        pdf = prepare_fixtures.text_pdf("One")
        for answer in (
            honest,
            {**honest, "page_count": 0},
            {**honest, "page_count": "1"},
            {**honest, "page_count": limits.MAX_PDF_PAGES + 1},
            {"type": "opaque", "reason": "encrypted"},
            ["not", "an", "answer"],
        ):
            with self.subTest(answer=answer):
                self.assertIsNone(
                    preparation.prepare_rendition(stored_file("a.pdf", pdf), WorkerHelper(answer), none, page=1)
                )
        # A page count below the page asked for is refused, as is a lying derivative with a valid count.
        self.assertIsNone(
            preparation.prepare_rendition(
                stored_file("a.pdf", pdf), WorkerHelper({**honest, "page_count": 1}), none, page=2
            )
        )
        lying = {**honest, "width": 1, "page_count": 1}
        self.assertIsNone(preparation.prepare_rendition(stored_file("a.pdf", pdf), WorkerHelper(lying), none, page=1))
        counted = preparation.prepare_rendition(
            stored_file("a.pdf", pdf), WorkerHelper({**honest, "page_count": 1}), none, page=1
        )
        self.assertEqual(counted.page_count, 1)


class ControllerContentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.controller = bare_controller(self.directory.name)

    def test_a_listed_file_is_delivered_whole_and_verified_to_its_own_team_only(self) -> None:
        stored = self.controller.storage.put("team_1", "brief.pdf", b"%PDF-1.7 original", "application/pdf")
        delivered: list[tuple[dict[str, object], bytes]] = []
        metadata = self.controller.file_content("team_1", stored["id"], lambda *sent: delivered.append(sent))
        self.assertEqual(delivered[0][1], b"%PDF-1.7 original")
        self.assertEqual(delivered[0][0]["sha256"], hashlib.sha256(b"%PDF-1.7 original").hexdigest())
        self.assertEqual(metadata["id"], stored["id"])
        for team_id, file_id in (("team_2", stored["id"]), ("team_1", "f" * 32), ("team_1", "not-a-file")):
            with self.subTest(team=team_id, file=file_id), self.assertRaises(ApiProblemError) as caught:
                self.controller.file_content(team_id, file_id, self.fail)
            self.assertEqual(caught.exception.code, "file-not-found")

    def test_a_file_whose_bytes_fail_their_digest_is_never_delivered(self) -> None:
        stored = self.controller.storage.put("team_1", "notes.md", b"original", "text/markdown")
        with (
            mock.patch.object(self.controller.storage, "get", side_effect=local_app.team_storage.StorageError("bad")),
            self.assertRaises(ApiProblemError),
        ):
            self.controller.file_content("team_1", stored["id"], self.fail)

    def test_at_most_two_downloads_run_at_once(self) -> None:
        stored = self.controller.storage.put("team_1", "notes.md", b"original", "text/markdown")
        held = [local_app._FILE_DOWNLOADS.acquire(blocking=False) for _ in range(local_app.MAX_FILE_DOWNLOADS)]
        try:
            self.assertEqual(held, [True, True])
            with self.assertRaises(ApiProblemError) as caught:
                self.controller.file_content("team_1", stored["id"], self.fail)
            self.assertEqual(caught.exception.code, "file-download-busy")
        finally:
            for _ in held:
                local_app._FILE_DOWNLOADS.release()
        # A failed delivery releases its slot too.
        for _ in range(local_app.MAX_FILE_DOWNLOADS + 1):
            with self.assertRaises(BrokenPipeError):
                self.controller.file_content("team_1", stored["id"], mock.Mock(side_effect=BrokenPipeError))

    def test_a_teardown_before_the_commit_ends_the_download_and_after_it_waits_for_it(self) -> None:
        stored = self.controller.storage.put("team_1", "notes.md", b"original", "text/markdown")
        # Closed before its last check: the download ends as not found and sends nothing.
        with local_lifecycle.previews_closed("team_1"), self.assertRaises(ApiProblemError) as caught:
            self.controller.file_content("team_1", stored["id"], self.fail)
        self.assertEqual(caught.exception.code, "file-not-found")

        sending = threading.Event()
        finish = threading.Event()
        order: list[str] = []

        def deliver(_metadata: dict[str, object], _content: bytes) -> None:
            sending.set()
            finish.wait(5)
            order.append("sent")

        download = threading.Thread(target=self.controller.file_content, args=("team_1", stored["id"], deliver))
        download.start()
        self.assertTrue(sending.wait(5))

        def teardown() -> None:
            with local_lifecycle.previews_closed("team_1"):
                order.append("closed")

        closing = threading.Thread(target=teardown)
        closing.start()
        # The teardown waits while the committed download is still sending.
        closing.join(0.1)
        self.assertTrue(closing.is_alive())
        finish.set()
        download.join(5)
        closing.join(5)
        self.assertEqual(order, ["sent", "closed"])

    def test_a_pdf_page_is_previewed_through_the_helper_with_its_page(self) -> None:
        stored = self.controller.storage.put(
            "team_1", "brief.pdf", prepare_fixtures.text_pdf("A", "B"), "application/pdf"
        )
        rendition = preparation.Rendition("image/png", 4, 4, b"\x89PNG\r\n\x1a\n", 2)
        with mock.patch.object(local_prepare, "prepare_rendition", return_value=rendition) as prepared:
            self.assertIs(self.controller.file_preview("team_1", stored["id"], 2), rendition)
        self.assertEqual(prepared.call_args.kwargs["page"], 2)


class PageAndContentRouteTests(unittest.TestCase):
    PAGE_PATH = f"/v1/teams/team_1/files/{'e' * 32}/pages/2/preview"
    CONTENT_PATH = f"/v1/teams/team_1/files/{'e' * 32}/content"
    _respond = preview_tests.PreviewRouteTests._respond

    def test_the_routes_resolve_to_their_operations(self) -> None:
        for path, operation, params in (
            (self.PAGE_PATH, "file-page-preview", {"team_id": "team_1", "file_id": "e" * 32, "page": "2"}),
            (self.CONTENT_PATH, "file-content", {"team_id": "team_1", "file_id": "e" * 32}),
        ):
            with self.subTest(operation=operation):
                parts = tuple(part for part in path.split("/") if part)
                self.assertEqual(
                    strict_http.resolve_controller_route("GET", parts),
                    strict_http.ControllerRouteMatch(operation, params),
                )

    def test_a_page_answers_its_rendition_with_the_page_count(self) -> None:
        png = prepare_fixtures.image("PNG", (4, 4))
        controller = types.SimpleNamespace(
            file_preview=mock.Mock(return_value=preparation.Rendition("image/png", 4, 4, png, 3))
        )
        head, body = self._respond(get_handler(self.PAGE_PATH, controller), return_value=supervisor_evidence())
        self.assertIn(b" 200 OK\r\n", head)
        self.assertIn(f"{http_preview.PAGE_COUNT_HEADER}: 3\r\n".encode(), head)
        self.assertIn(b"Content-Type: image/png\r\n", head)
        self.assertEqual(body, png)
        controller.file_preview.assert_called_once_with("team_1", "e" * 32, 2)

    def test_a_page_outside_the_ceiling_or_without_a_count_is_refused(self) -> None:
        controller = types.SimpleNamespace(file_preview=mock.Mock())
        for number in ("0", "51", "01", "x"):
            with self.subTest(page=number):
                path = self.PAGE_PATH.replace("/2/", f"/{number}/")
                head, body = self._respond(get_handler(path, controller), return_value=supervisor_evidence())
                self.assertIn(b" 404 Not Found\r\n", head)
                self.assertIn(b"file-not-found", body)
        controller.file_preview.assert_not_called()
        uncounted = preparation.Rendition("image/png", 4, 4, prepare_fixtures.image("PNG", (4, 4)))
        head, body = self._respond(
            get_handler(self.PAGE_PATH, types.SimpleNamespace(file_preview=mock.Mock(return_value=uncounted))),
            return_value=supervisor_evidence(),
        )
        self.assertIn(b" 500 Internal Server Error\r\n", head)
        self.assertIn(b"file-preview-invalid", body)

    def _content_controller(self, content: bytes, sha256: str | None = None) -> object:
        metadata = {"id": "e" * 32, "sha256": sha256 or hashlib.sha256(content).hexdigest()}

        def file_content(_team_id, _file_id, deliver):
            deliver(metadata, content)
            return metadata

        return types.SimpleNamespace(file_content=mock.Mock(side_effect=file_content))

    def test_the_original_is_sent_as_an_octet_stream_with_its_length_and_digest(self) -> None:
        content = b"%PDF-1.7 <script>alert(1)</script>"
        head, body = self._respond(
            get_handler(self.CONTENT_PATH, self._content_controller(content)), return_value=supervisor_evidence()
        )
        digest = base64.b64encode(hashlib.sha256(content).digest()).decode()
        self.assertIn(b" 200 OK\r\n", head)
        self.assertIn(b"Content-Type: application/octet-stream\r\n", head)
        self.assertIn(f"Content-Length: {len(content)}\r\n".encode(), head)
        self.assertIn(f"Repr-Digest: sha-256=:{digest}:\r\n".encode(), head)
        self.assertIn(b"Cache-Control: no-store\r\n", head)
        self.assertIn(b"X-Content-Type-Options: nosniff\r\n", head)
        self.assertEqual(body, content)

    def test_the_release_is_audited_by_file_id_and_size_before_it_is_sent(self) -> None:
        handler = get_handler(self.CONTENT_PATH, self._content_controller(b"original"))
        with (
            mock.patch.object(authority, "verify", return_value=supervisor_evidence()),
            mock.patch.object(http_audit.local_audit, "record", return_value="d" * 32) as recorded,
        ):
            handler._handle()
        details = [call.kwargs.get("detail") for call in recorded.call_args_list]
        self.assertIn(f"file={'e' * 32} bytes=8", details)
        self.assertNotIn(b"original", repr(recorded.call_args_list).encode())

    def test_busy_and_misframed_downloads_answer_json(self) -> None:
        busy = ApiProblemError(HTTPStatus.SERVICE_UNAVAILABLE, "busy", code="file-download-busy")
        head, body = self._respond(
            get_handler(self.CONTENT_PATH, types.SimpleNamespace(file_content=mock.Mock(side_effect=busy))),
            return_value=supervisor_evidence(),
        )
        self.assertIn(b" 503 Service Unavailable\r\n", head)
        self.assertIn(b'"retry_after_ms":250', body)
        missing = ApiProblemError(HTTPStatus.NOT_FOUND, "file not found", code="file-not-found")
        head, body = self._respond(
            get_handler(self.CONTENT_PATH, types.SimpleNamespace(file_content=mock.Mock(side_effect=missing))),
            return_value=supervisor_evidence(),
        )
        self.assertIn(b" 404 Not Found\r\n", head)
        for content, sha256 in ((b"", None), (b"original", "A" * 64)):
            with self.subTest(length=len(content)):
                head, body = self._respond(
                    get_handler(self.CONTENT_PATH, self._content_controller(content, sha256)),
                    return_value=supervisor_evidence(),
                )
                self.assertIn(b" 500 Internal Server Error\r\n", head)
                self.assertIn(b"file-content-invalid", body)

    def test_the_content_framing_admits_only_an_exact_length_and_digest(self) -> None:
        self.assertIsNotNone(http_content.canonical_framing({"length": 1, "sha256": "0" * 64}))
        self.assertIsNone(
            http_content.canonical_framing({"length": http_content.MAX_CONTENT_BYTES + 1, "sha256": "0" * 64})
        )


if __name__ == "__main__":
    unittest.main()
