"""A file from the web leaves Team only after its Supervisor acknowledged that exact file (ADR-0117 §7)."""

import tempfile
import unittest
from http import HTTPStatus
from unittest import mock

from core.http import strict as strict_http
from local import audit as local_audit
from local.errors import ApiProblemError
from local.http import server as local_server
from storage import files as team_storage
from storage import web as team_web
from tests.test_file_preview import bare_controller

SUPERVISOR = "a" * 32
PDF = b"%PDF-1.7 from the web"


class FileAcknowledgementTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.controller = bare_controller(directory.name)
        self.controller.web_files = team_web.WebFiles(self.controller.storage)
        self.audit = []
        recorded = mock.patch.object(
            local_audit,
            "record_request",
            side_effect=lambda operation, **fields: self.audit.append((operation, fields)),
        )
        recorded.start()
        self.addCleanup(recorded.stop)
        self.principal = mock.patch.object(local_audit, "human_principal", return_value=SUPERVISOR)
        self.principal.start()
        self.addCleanup(self.principal.stop)

    def web_file(self, content=PDF):
        import hashlib

        self.controller.web_files.mark("team_1", hashlib.sha256(content).hexdigest())
        return self.controller.storage.put("team_1", "edital.pdf", content, "application/pdf")

    def test_a_web_original_needs_its_supervisors_acknowledgement_and_an_upload_never_does(self):
        stored = self.web_file()
        with self.assertRaises(ApiProblemError) as caught:
            self.controller.file_content("team_1", stored["id"], self.fail)
        self.assertEqual(
            (caught.exception.status, caught.exception.code), (HTTPStatus.FORBIDDEN, "file-acknowledgement-required")
        )
        self.assertEqual(
            self.controller.acknowledge_file("team_1", stored["id"]), {"file_id": stored["id"], "acknowledged": True}
        )
        delivered = []
        self.controller.file_content("team_1", stored["id"], lambda *sent: delivered.append(sent))
        self.assertEqual(delivered[0][1], PDF)
        upload = self.controller.storage.put("team_1", "nota.txt", b"plain", "text/plain")
        self.controller.file_content("team_1", upload["id"], lambda *_sent: None)
        operations = [(operation, fields["result"]) for operation, fields in self.audit]
        self.assertIn(("file-content", "denied"), operations)
        self.assertIn(("file-acknowledgement", "ok"), operations)

    def test_another_supervisor_or_none_cannot_use_an_acknowledgement(self):
        stored = self.web_file()
        self.controller.acknowledge_file("team_1", stored["id"])
        for principal in ("b" * 32, None):
            with (
                self.subTest(principal=principal),
                mock.patch.object(local_audit, "human_principal", return_value=principal),
            ):
                with self.assertRaises(ApiProblemError) as caught:
                    self.controller.file_content("team_1", stored["id"], self.fail)
                self.assertEqual(caught.exception.code, "file-acknowledgement-required")
        with (
            mock.patch.object(local_audit, "human_principal", return_value=None),
            self.assertRaises(ApiProblemError) as caught,
        ):
            self.controller.acknowledge_file("team_1", stored["id"])
        self.assertEqual(caught.exception.code, "file-acknowledgement-required")

    def test_only_a_listed_web_file_can_be_acknowledged_and_an_unreadable_store_refuses(self):
        upload = self.controller.storage.put("team_1", "nota.txt", b"plain", "text/plain")
        with self.assertRaises(ApiProblemError) as caught:
            self.controller.acknowledge_file("team_1", upload["id"])
        self.assertEqual(caught.exception.code, "file-not-from-web")
        with self.assertRaises(ApiProblemError) as caught:
            self.controller.acknowledge_file("team_1", "f" * 32)
        self.assertEqual(caught.exception.code, "file-not-found")
        stored = self.web_file()
        with (
            mock.patch.object(self.controller.web_files, "is_web", side_effect=team_storage.StorageError("closed")),
            self.assertRaises(ApiProblemError),
        ):
            self.controller.file_content("team_1", stored["id"], self.fail)
        with (
            mock.patch.object(self.controller.web_files, "acknowledge", side_effect=team_storage.StorageError("full")),
            self.assertRaises(ApiProblemError),
        ):
            self.controller.acknowledge_file("team_1", stored["id"])

    def test_the_route_is_a_bodyless_post_audited_under_its_team(self):
        match = strict_http.resolve_controller_route(
            "POST", ("v1", "teams", "team_1", "files", "f" * 32, "acknowledgement")
        )
        self.assertEqual(match.operation, "file-acknowledgement")
        handler = mock.Mock()
        handler.server.controller.acknowledge_file.return_value = {"file_id": "f" * 32, "acknowledged": True}
        result = local_server._OPERATIONS["file-acknowledgement"](handler, match)
        self.assertEqual(
            result, (HTTPStatus.OK, {"file_id": "f" * 32, "acknowledged": True}, "file-acknowledgement", "team_1", None)
        )

    @staticmethod
    def fail(*_sent):
        raise AssertionError("nothing may be delivered")


if __name__ == "__main__":
    unittest.main()
