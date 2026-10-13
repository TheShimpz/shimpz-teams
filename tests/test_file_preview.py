"""A Team file's preview: the sanitized rendition, its bounded admission, its lifecycle, and its route (ADR-0093)."""

import base64
import contextlib
import hashlib
import tempfile
import threading
import types
import unittest
from email.message import Message
from http import HTTPStatus
from io import BytesIO
from pathlib import Path
from unittest import mock

from docker.errors import DockerException, NotFound

from action import execution as action_execution
from core.http import strict as strict_http
from local import activity as local_activity
from local import app as local_app
from local import authority
from local import lifecycle as local_lifecycle
from local import prepare as local_prepare
from local.errors import ApiProblemError
from local.http import audit as http_audit
from local.http import server
from prepare import helper as preparation_helper
from prepare import limits, worker
from prepare import service as preparation
from protocol.http.v1 import supervisor as supervisor_contract
from storage import files as team_storage
from tests import prepare_fixtures

IMAGE_ID = "sha256:" + "c" * 64
NETWORK = types.SimpleNamespace(id="a" * 64)
TOKEN = "-".join(("bearer", "test"))


def _stored(name: str, data: bytes, *, size: int | None = None, reads: list[str] | None = None) -> object:
    def read() -> bytes:
        if reads is not None:
            reads.append(name)
        return data

    return preparation.StoredFile("0" * 32, name, len(data) if size is None else size, "0" * 64, read)


class _Helper:
    """The real worker answering in process, or a fixed answer."""

    def __init__(self, answer: dict[str, object] | None = None) -> None:
        self.answer = answer
        self.opened = 0

    @contextlib.contextmanager
    def __call__(self):
        self.opened += 1
        yield self

    def prepare(self, kind: str, data: bytes) -> dict[str, object]:
        return self.answer if self.answer is not None else worker.answer(worker.encode_request(kind, data))


class _Container:
    def __init__(self, client: _Client, name: str) -> None:
        self.id = f"helper-{name}"
        self.client = client

    def start(self) -> None:
        self.client.events.append(("start", self.id))

    def remove(self, *, force: bool) -> None:
        self.client.events.append(("remove", self.id))
        if self.client.remove_error is not None:
            raise self.client.remove_error


class _Client:
    def __init__(self) -> None:
        self.events: list[object] = []
        self.remove_error: BaseException | None = None
        self.api = types.SimpleNamespace()
        self.containers = types.SimpleNamespace(
            create=self._create,
            get=lambda _host: types.SimpleNamespace(image=types.SimpleNamespace(id=IMAGE_ID)),
            list=lambda **_kwargs: [],
        )

    def _create(self, **kwargs: object) -> _Container:
        self.events.append(("create", kwargs["labels"][local_prepare.TEAM_LABEL]))
        return _Container(self, str(len(self.events)))


class RenditionTests(unittest.TestCase):
    def test_a_stored_image_becomes_the_bounded_derivative_without_its_metadata(self) -> None:
        helper = _Helper()
        rendition = preparation.prepare_rendition(
            _stored("photo.jpg", prepare_fixtures.oriented_jpeg((3000, 2000), 6)), helper, contextlib.nullcontext()
        )
        self.assertEqual(rendition.media_type, "image/jpeg")
        self.assertLessEqual(max(rendition.width, rendition.height), limits.DERIVATIVE_LONG_EDGE)
        self.assertLessEqual(rendition.width * rendition.height, limits.DERIVATIVE_PIXELS)
        # EXIF orientation 6 turns the landscape original into a portrait rendition.
        self.assertGreater(rendition.height, rendition.width)
        self.assertTrue(rendition.data.startswith(b"\xff\xd8\xff"))
        self.assertNotIn(b"Private Camera Maker", rendition.data)
        self.assertNotIn(b"private-icc-profile", rendition.data)
        self.assertLessEqual(len(rendition.data), limits.MAX_DERIVATIVE_BYTES)
        transparent = preparation.prepare_rendition(
            _stored("clip.png", prepare_fixtures.image("PNG", (40, 30), mode="RGBA")), helper, contextlib.nullcontext()
        )
        self.assertEqual((transparent.media_type, transparent.width, transparent.height), ("image/png", 40, 30))

    def test_the_kind_comes_from_the_bytes_and_anything_refused_has_no_rendition(self) -> None:
        helper = _Helper()
        reads: list[str] = []
        none = contextlib.nullcontext()
        # An oversized original is never read, whatever its name says.
        oversized = _stored("big.png", b"\x89PNG", size=limits.MAX_IMAGE_BYTES + 1, reads=reads)
        self.assertIsNone(preparation.prepare_rendition(oversized, helper, none))
        self.assertEqual(reads, [])
        self.assertIsNone(preparation.prepare_rendition(_stored("photo.png", b"plain text"), helper, none))
        self.assertIsNone(preparation.prepare_rendition(_stored("doc.png", b"%PDF-1.7"), helper, none))
        self.assertIsNone(
            preparation.prepare_rendition(_stored("a.webp", prepare_fixtures.animated_webp()), helper, none)
        )
        gif = prepare_fixtures.image("GIF", (8, 8))
        self.assertIsNone(preparation.prepare_rendition(_stored("a.png", gif), helper, none))
        self.assertIsNone(preparation.prepare_rendition(_stored("bad.png", b"\x89PNG\r\n\x1a\nbroken"), helper, none))
        # Only an image reached the helper.
        self.assertEqual(helper.opened, 2)
        # A short stored length that understates the bytes read is refused before the helper.
        understated = _stored("a.png", b"\x89PNG\r\n\x1a\n" + b"0" * limits.MAX_IMAGE_BYTES, size=10)
        self.assertIsNone(preparation.prepare_rendition(understated, helper, none))
        self.assertEqual(helper.opened, 2)

    def test_the_helpers_untrusted_answer_is_validated_as_a_turns_is(self) -> None:
        none = contextlib.nullcontext()
        png = prepare_fixtures.image("PNG", (4, 4))
        lying = {"type": "image", "media_type": "image/png", "width": 4, "height": 4, "base64": "bm90IGFuIGltYWdl"}
        huge = {**lying, "width": limits.DERIVATIVE_LONG_EDGE + 1}
        for answer in (lying, huge, {"type": "opaque", "reason": "animated"}, {"type": "text", "text": "x"}):
            with self.subTest(answer=answer):
                self.assertIsNone(preparation.prepare_rendition(_stored("a.png", png), _Helper(answer), none))

    def test_a_derivative_whose_bytes_declare_another_size_is_refused(self) -> None:
        none = contextlib.nullcontext()
        png = prepare_fixtures.image("PNG", (64, 48))
        jpeg = prepare_fixtures.image("JPEG", (64, 48))
        for media_type, data in (("image/png", png), ("image/jpeg", jpeg)):
            with self.subTest(media_type=media_type):
                encoded = base64.b64encode(data).decode("ascii")
                honest = {"type": "image", "media_type": media_type, "width": 64, "height": 48, "base64": encoded}
                self.assertIsNotNone(preparation.prepare_rendition(_stored("a.png", png), _Helper(honest), none))
                # A helper that understates the size a decoder would allocate is refused.
                lying = {**honest, "width": 1, "height": 1}
                self.assertIsNone(preparation.prepare_rendition(_stored("a.png", png), _Helper(lying), none))

    def test_the_encoded_size_is_read_from_the_header_alone(self) -> None:
        jpeg = prepare_fixtures.image("JPEG", (640, 481))
        self.assertEqual(preparation.encoded_size("image/jpeg", jpeg), (640, 481))
        self.assertEqual(preparation.encoded_size("image/png", prepare_fixtures.image("PNG", (37, 21))), (37, 21))
        # Fill bytes and standalone markers before the frame header are skipped.
        padded = jpeg[:2] + b"\xff\xff\xd0" + jpeg[2:]
        self.assertEqual(preparation.encoded_size("image/jpeg", padded), (640, 481))
        refused = (
            ("image/png", b"\x89PNG\r\n\x1a\n"),
            ("image/png", b"\x89PNG\r\n\x1a\n\x00\x00\x00\x0dIDAT" + b"\x00" * 8),
            ("image/jpeg", b"\xff\xd8\xff\xda\x00\x02"),
            ("image/jpeg", b"\xff\xd8\x00\x00\x00\x00"),
            ("image/jpeg", b"\xff\xd8\xff\xc0\x00\x11\x08"),
            ("image/jpeg", b"\xff\xd8\xff\xe0\x00\x10"),
        )
        for media_type, data in refused:
            with self.subTest(data=data):
                self.assertIsNone(preparation.encoded_size(media_type, data))

    def test_an_ended_request_reads_nothing_further_and_starts_no_helper(self) -> None:
        class EndedError(Exception):
            pass

        reads: list[str] = []
        calls: list[int] = []

        def interrupt() -> None:
            calls.append(1)
            if len(calls) >= 2:
                raise EndedError

        helper = _Helper()
        png = prepare_fixtures.image("PNG", (4, 4))
        with self.assertRaises(EndedError):
            preparation.prepare_rendition(
                _stored("a.png", png, reads=reads), helper, contextlib.nullcontext(), interrupt
            )
        self.assertEqual((reads, helper.opened), (["a.png"], 0))

        calls.clear()

        def after_helper() -> None:
            calls.append(1)
            if len(calls) >= 3:
                raise EndedError

        with self.assertRaises(EndedError):
            preparation.prepare_rendition(_stored("a.png", png), helper, contextlib.nullcontext(), after_helper)
        self.assertEqual(helper.opened, 1)


class BoundedAdmissionTests(unittest.TestCase):
    def test_the_slot_is_held_and_released(self) -> None:
        slot = threading.Lock()
        admission = preparation.BoundedAdmission(slot, queue=1, deadline=1.0)
        with admission.admit(lambda: None):
            self.assertTrue(slot.locked())
        self.assertFalse(slot.locked())

    def test_a_full_queue_and_an_expired_deadline_are_busy(self) -> None:
        slot = threading.Lock()
        slot.acquire()
        clock = iter([0.0, 0.05, 5.0])
        admission = preparation.BoundedAdmission(slot, queue=1, deadline=1.0, clock=lambda: next(clock))
        with self.assertRaises(preparation.AdmissionBusyError), admission.admit(lambda: None):
            self.fail("an expired wait must not hold the slot")
        # The expired waiter left the queue.
        self.assertEqual(admission._waiting, 0)

        waiting = threading.Event()
        leave = threading.Event()

        def interrupt() -> None:
            waiting.set()
            if leave.is_set():
                raise RuntimeError("ended")

        queued = preparation.BoundedAdmission(slot, queue=1, deadline=30.0)

        def wait() -> None:
            with contextlib.suppress(RuntimeError), queued.admit(interrupt):
                pass

        waiter = threading.Thread(target=wait)
        waiter.start()
        self.assertTrue(waiting.wait(5))
        with self.assertRaises(preparation.AdmissionBusyError), queued.admit(lambda: None):
            self.fail("a request beyond the queue must not wait")
        leave.set()
        waiter.join(5)
        self.assertFalse(waiter.is_alive())
        slot.release()


class PreviewBookTests(unittest.TestCase):
    def test_a_closed_team_opens_no_preview_until_it_reopens(self) -> None:
        book = local_prepare.PreviewBook()
        with book.closed("team_1"):
            with self.assertRaises(local_prepare.PreviewEndedError), book.open("team_1"):
                pass
            with book.open("team_2") as other:
                other.interrupt()
            with book.closed("team_1"), self.assertRaises(local_prepare.PreviewEndedError), book.open("team_1"):
                pass
        with book.closed(), self.assertRaises(local_prepare.PreviewEndedError), book.open("team_2"):
            pass
        with book.open("team_1") as preview:
            preview.interrupt()

    def test_closing_ends_previews_removes_their_helper_and_waits_for_them(self) -> None:
        book = local_prepare.PreviewBook()
        container = types.SimpleNamespace(remove=mock.Mock())
        with book.open("team_1") as preview, book.open("team_2") as other:
            preview.started(container)
            other.started(object())
            # Both previews are still open here, so the wait for the ended one to leave is answered at once.
            with mock.patch.object(book.guard, "wait_for", return_value=True), book.closed("team_1"):
                pass
            container.remove.assert_called_once_with(force=True)
            self.assertTrue(preview.ended)
            self.assertFalse(other.ended)
            with self.assertRaises(local_prepare.PreviewEndedError):
                preview.interrupt()
            with self.assertRaises(local_prepare.PreviewEndedError):
                preview.started(object())
            preview.stopped(object())
            self.assertIs(preview.container, container)
            preview.stopped(container)
            self.assertIsNone(preview.container)

    def test_a_preview_that_does_not_leave_or_a_helper_that_remains_fails_the_close(self) -> None:
        book = local_prepare.PreviewBook()
        with (
            book.open("team_1"),
            mock.patch.object(local_prepare, "PREVIEW_DRAIN_SECONDS", 0.01),
            self.assertRaises(local_prepare.PreviewDrainError),
            book.closed("team_1"),
        ):
            self.fail("the close must not proceed")
        # The failed close left the Team open again.
        with book.open("team_1"):
            pass
        stuck = types.SimpleNamespace(remove=mock.Mock(side_effect=DockerException("busy")))
        gone = types.SimpleNamespace(
            remove=mock.Mock(side_effect=NotFound("gone", response=types.SimpleNamespace(status_code=404)))
        )
        with book.open("team_1") as preview:
            preview.started(stuck)
            with self.assertRaises(local_prepare.PreviewDrainError), book.closed("team_1"):
                self.fail("a remaining helper must fail the close")
        with book.open("team_1") as preview:
            preview.started(gone)
            with mock.patch.object(book.guard, "wait_for", return_value=True), book.closed("team_1"):
                pass
        gone.remove.assert_called_once_with(force=True)


def _controller(directory: str, client: _Client | None = None) -> local_app.LocalController:
    controller = object.__new__(local_app.LocalController)
    controller._locks = tuple(threading.RLock() for _ in range(64))
    controller.storage = team_storage.TeamStorage(Path(directory) / "teams")
    controller.assistant_lifecycle = types.SimpleNamespace(_network=lambda _team_id: NETWORK)
    controller.client = client or _Client()
    controller.space_id = "local-space"
    controller.cpuset_cpus = None
    controller._raise_storage_problem = local_app.LocalController._raise_storage_problem
    return controller


class ControllerPreviewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)

    def _in_process(self):
        def exchange(_container_id, _argv, request, _strategy):
            return worker.answer(request)

        return mock.patch.object(action_execution, "rpc_exchange", side_effect=exchange)

    def test_a_listed_image_of_the_named_team_previews_through_the_helper(self) -> None:
        client = _Client()
        controller = _controller(self.directory.name, client)
        png = prepare_fixtures.image("PNG", (64, 48))
        stored = controller.storage.put("team_1", "photo.png", png, "image/png")
        with self._in_process():
            rendition = controller.file_preview("team_1", stored["id"])
        # An opaque image is re-encoded as JPEG.
        self.assertEqual((rendition.media_type, rendition.width, rendition.height), ("image/jpeg", 64, 48))
        # The helper ran for this Team and is gone again.
        self.assertEqual(client.events[0], ("create", "team_1"))
        self.assertEqual(client.events[-1][0], "remove")
        # Another Team cannot read it, and the read renewed nothing.
        with self.assertRaises(ApiProblemError) as caught:
            controller.file_preview("team_2", stored["id"])
        self.assertEqual(caught.exception.code, "file-not-found")

    def test_missing_collected_and_unsupported_files_have_no_preview(self) -> None:
        controller = _controller(self.directory.name)
        with self.assertRaises(ApiProblemError) as caught:
            controller.file_preview("team_1", "f" * 32)
        self.assertEqual((caught.exception.status, caught.exception.code), (HTTPStatus.NOT_FOUND, "file-not-found"))
        with self.assertRaises(ApiProblemError) as caught:
            controller.file_preview("team_1", "not-a-file")
        self.assertEqual(caught.exception.code, "file-not-found")
        text = controller.storage.put("team_1", "notes.png", b"plain text", "image/png")
        with self.assertRaises(ApiProblemError) as caught:
            controller.file_preview("team_1", text["id"])
        self.assertEqual(caught.exception.code, "file-preview-unsupported")
        # A file idle past its grace period counts as collected even before a sweep removes it.
        with mock.patch.object(
            controller.storage, "_now", return_value=controller.storage._now() + team_storage.UNREFERENCED_GRACE_SECONDS
        ):
            with self.assertRaises(ApiProblemError) as caught:
                controller.file_preview("team_1", text["id"])
            self.assertEqual(caught.exception.code, "file-not-found")
            controller.storage.reference("team_1", [text["id"]])
            self.assertEqual(controller.storage.listed("team_1", text["id"])["size"], 10)

    def test_busy_unavailable_ended_and_changed_previews_map_to_their_outcome(self) -> None:
        controller = _controller(self.directory.name)
        stored = controller.storage.put("team_1", "photo.png", prepare_fixtures.image("PNG", (4, 4)), "image/png")
        outcomes = (
            (preparation.AdmissionBusyError("busy"), "file-preview-busy"),
            (preparation_helper.HelperUnavailableError("down"), "file-preview-unavailable"),
            (local_prepare.PreviewEndedError("ended"), "file-not-found"),
            (team_storage.StorageError("unsafe"), "storage-safety-failed"),
        )
        for error, code in outcomes:
            with self.subTest(code=code), mock.patch.object(local_prepare, "prepare_rendition", side_effect=error):
                with self.assertRaises(ApiProblemError) as caught:
                    controller.file_preview("team_1", stored["id"])
                self.assertEqual(caught.exception.code, code)
        changed = (dict(stored, sha256="0" * 64), b"other")
        with (
            mock.patch.object(controller.storage, "get", return_value=changed),
            self.assertRaises(ApiProblemError) as caught,
        ):
            controller.file_preview("team_1", stored["id"])
        self.assertEqual(caught.exception.code, "file-not-found")

    def test_a_file_collected_while_it_was_prepared_is_not_answered(self) -> None:
        controller = _controller(self.directory.name)
        png = prepare_fixtures.image("PNG", (4, 4))
        stored = controller.storage.put("team_1", "photo.png", png, "image/png")

        def collected(*_args, **_kwargs):
            controller.storage.delete("team_1", stored["id"])
            return preparation.Rendition("image/png", 4, 4, png)

        with (
            mock.patch.object(local_prepare, "prepare_rendition", side_effect=collected),
            self.assertRaises(ApiProblemError) as caught,
        ):
            controller.file_preview("team_1", stored["id"])
        self.assertEqual(caught.exception.code, "file-not-found")

    def test_a_queued_preview_ended_by_the_deletion_leaves_before_it_and_never_starts_a_helper(self) -> None:
        client = _Client()
        controller = _controller(self.directory.name, client)
        controller.chat_turn_service = types.SimpleNamespace(
            _file_deletion_slot=lambda _team_id: contextlib.nullcontext(), _forget_file=mock.Mock()
        )
        stored = controller.storage.put("team_1", "photo.png", prepare_fixtures.image("PNG", (4, 4)), "image/png")
        outcome: list[BaseException] = []

        def preview() -> None:
            try:
                controller.file_preview("team_1", stored["id"])
            except ApiProblemError as exc:
                outcome.append(exc)

        # Another preparation holds the controller's one slot, so this preview waits in the queue.
        with local_prepare._SERIAL:
            waiter = threading.Thread(target=preview)
            waiter.start()
            for _ in range(200):
                if local_prepare._PREVIEW_ADMISSION._waiting:
                    break
                threading.Event().wait(0.01)
            self.assertEqual(local_prepare._PREVIEW_ADMISSION._waiting, 1)
            self.assertTrue(controller.delete_file("team_1", stored["id"])["deleted"])
            # The deletion returned only once the queued preview had left.
            waiter.join(0.01)
            self.assertFalse(waiter.is_alive())
        self.assertEqual(outcome[0].code, "file-not-found")
        self.assertEqual(client.events, [])

    def test_a_running_preview_ended_by_teardown_loses_its_helper_before_teardown_continues(self) -> None:
        client = _Client()
        controller = _controller(self.directory.name, client)
        stored = controller.storage.put("team_1", "photo.png", prepare_fixtures.image("PNG", (4, 4)), "image/png")
        running = threading.Event()
        removed = threading.Event()
        outcome: list[BaseException] = []

        def exchange(container_id, _argv, _request, _strategy):
            running.set()
            removed.wait(5)
            raise action_execution.RpcExchangeError("the helper was removed")

        def preview() -> None:
            try:
                controller.file_preview("team_1", stored["id"])
            except ApiProblemError as exc:
                outcome.append(exc)

        def removal(*, force: bool) -> None:
            client.events.append(("remove", "running"))
            removed.set()

        with mock.patch.object(action_execution, "rpc_exchange", side_effect=exchange):
            waiter = threading.Thread(target=preview)
            waiter.start()
            self.assertTrue(running.wait(5))
            [ticket] = local_prepare.PREVIEWS._previews
            ticket.container.remove = removal
            with local_lifecycle.previews_closed("team_1"):
                # Teardown proceeds only once the preview left, its helper removed.
                self.assertFalse(waiter.is_alive())
                self.assertIn(("remove", "running"), client.events)
                with self.assertRaises(ApiProblemError) as caught:
                    controller.file_preview("team_1", stored["id"])
                self.assertEqual(caught.exception.code, "file-not-found")
            waiter.join(5)
        self.assertEqual(outcome[0].code, "file-not-found")

    def test_a_close_whose_preview_does_not_end_refuses_the_lifecycle_operation(self) -> None:
        stuck = types.SimpleNamespace(closed=mock.Mock(side_effect=local_prepare.PreviewDrainError("stuck")))
        with (
            mock.patch.object(local_prepare, "PREVIEWS", stuck),
            self.assertRaises(ApiProblemError) as caught,
            local_lifecycle.previews_closed("team_1"),
        ):
            self.fail("the lifecycle operation must not proceed")
        self.assertEqual(caught.exception.code, "file-preview-active")


def _handler(path: str, controller: object, *, bearer: bool = True, assertion: bool = True) -> server.Handler:
    handler = object.__new__(server.Handler)
    handler.command = "GET"
    handler.path = path
    handler.server = types.SimpleNamespace(controller=controller, token=TOKEN, activity=local_activity.Activity())
    handler.headers = Message()
    if bearer:
        handler.headers.add_header("Authorization", f"Bearer {TOKEN}")
    if assertion:
        handler.headers.add_header(supervisor_contract.ASSERTION_HEADER, "Bearer assertion")
    handler.rfile = BytesIO()
    handler.wfile = BytesIO()
    handler.requestline = f"GET {path} HTTP/1.1"
    handler.request_version = "HTTP/1.1"
    return handler


def _evidence() -> authority.Evidence:
    return authority.Evidence(
        supervisor_id="a" * 32,
        authority_kind="session",
        authority_digest="b" * 64,
        assertion_id="c" * 32,
        expires_at=2_200_000_015,
    )


class PreviewRouteTests(unittest.TestCase):
    PATH = f"/v1/teams/team_1/files/{'e' * 32}/preview"

    def test_the_route_resolves_to_its_operation(self) -> None:
        parts = tuple(part for part in self.PATH.split("/") if part)
        self.assertEqual(
            strict_http.resolve_controller_route("GET", parts),
            strict_http.ControllerRouteMatch("file-preview", {"team_id": "team_1", "file_id": "e" * 32}),
        )
        self.assertIsNone(strict_http.resolve_controller_route("POST", parts))

    def _respond(self, handler: server.Handler, **verify: object) -> tuple[bytes, bytes]:
        with (
            mock.patch.object(authority, "verify", **verify) as verified,
            mock.patch.object(http_audit.local_audit, "record", return_value="d" * 32),
        ):
            handler._handle()
        self.verified = verified
        head, _separator, body = handler.wfile.getvalue().partition(b"\r\n\r\n")
        return head, body

    def test_both_factors_answer_the_exact_framed_rendition(self) -> None:
        png = prepare_fixtures.image("PNG", (4, 4))
        controller = types.SimpleNamespace(
            file_preview=mock.Mock(return_value=preparation.Rendition("image/png", 4, 4, png))
        )
        head, body = self._respond(_handler(self.PATH, controller), return_value=_evidence())
        self.assertIn(b" 200 OK\r\n", head)
        self.assertIn(b"Content-Type: image/png\r\n", head)
        self.assertIn(f"Content-Length: {len(png)}\r\n".encode(), head)
        self.assertIn(b"Cache-Control: no-store\r\n", head)
        self.assertIn(b"X-Content-Type-Options: nosniff\r\n", head)
        self.assertEqual(body, png)
        controller.file_preview.assert_called_once_with("team_1", "e" * 32)
        request = self.verified.call_args.kwargs["request"]
        self.assertEqual(
            (request.method, request.path, request.authority_kinds), ("GET", self.PATH, frozenset({"session"}))
        )
        self.assertEqual(request.body["kind"], "none")

    def test_neither_factor_alone_reaches_the_file(self) -> None:
        controller = types.SimpleNamespace(file_preview=mock.Mock())
        head, _body = self._respond(_handler(self.PATH, controller, bearer=False), return_value=_evidence())
        self.assertIn(b" 401 Unauthorized\r\n", head)
        head, body = self._respond(
            _handler(self.PATH, controller, assertion=False), side_effect=authority.SupervisorDeniedError("absent")
        )
        self.assertIn(b" 403 Forbidden\r\n", head)
        self.assertIn(b"invalid-supervisor", body)
        controller.file_preview.assert_not_called()

    def test_busy_failed_and_misframed_previews_answer_json(self) -> None:
        busy = ApiProblemError(
            HTTPStatus.SERVICE_UNAVAILABLE, "file preview capacity is busy", code="file-preview-busy"
        )
        head, body = self._respond(
            _handler(self.PATH, types.SimpleNamespace(file_preview=mock.Mock(side_effect=busy))),
            return_value=_evidence(),
        )
        self.assertIn(b" 503 Service Unavailable\r\n", head)
        self.assertIn(b'"retry_after_ms":250', body)
        missing = ApiProblemError(HTTPStatus.NOT_FOUND, "file not found", code="file-not-found")
        head, body = self._respond(
            _handler(self.PATH, types.SimpleNamespace(file_preview=mock.Mock(side_effect=missing))),
            return_value=_evidence(),
        )
        self.assertIn(b" 404 Not Found\r\n", head)
        self.assertNotIn(b"retry_after_ms", body)
        for rendition in (
            preparation.Rendition("image/png", 4, 4, b"not a png"),
            preparation.Rendition("image/webp", 4, 4, b"RIFF"),
            preparation.Rendition("image/png", 4, 4, b"\x89PNG\r\n\x1a\n" + b"0" * limits.MAX_DERIVATIVE_BYTES),
        ):
            with self.subTest(media_type=rendition.media_type, length=len(rendition.data)):
                head, body = self._respond(
                    _handler(self.PATH, types.SimpleNamespace(file_preview=mock.Mock(return_value=rendition))),
                    return_value=_evidence(),
                )
                self.assertIn(b" 500 Internal Server Error\r\n", head)
                self.assertIn(b"file-preview-invalid", body)
                self.assertIn(b"Content-Type: application/json\r\n", head)


class StorageListingTests(unittest.TestCase):
    def test_a_listed_file_is_reported_without_its_bytes_and_absent_teams_have_none(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            storage = team_storage.TeamStorage(Path(directory) / "teams")
            with self.assertRaises(team_storage.StorageNotFoundError):
                storage.listed("team_1", "e" * 32)
            stored = storage.put("team_1", "a.png", b"bytes", "image/png")
            self.assertEqual(
                storage.listed("team_1", stored["id"]),
                {
                    "id": stored["id"],
                    "name": "a.png",
                    "media_type": "image/png",
                    "size": 5,
                    "sha256": hashlib.sha256(b"bytes").hexdigest(),
                },
            )


if __name__ == "__main__":
    unittest.main()
