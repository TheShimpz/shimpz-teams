"""A file from the web leaves Team only after its Supervisor acknowledged that exact file (ADR-0117 §7)."""

import tempfile
import unittest
from http import HTTPStatus
from pathlib import Path
from unittest import mock

from core.http import strict as strict_http
from local import audit as local_audit
from local.errors import ApiProblemError
from local.http import server as local_server
from storage import files as team_storage
from tests.test_file_preview import bare_controller

SUPERVISOR = "a" * 32
PDF = b"%PDF-1.7 from the web"


class FileAcknowledgementTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.controller = bare_controller(directory.name)
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
        return self.controller.storage.put("team_1", "edital.pdf", content, "application/pdf", web=True)

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
            mock.patch.object(self.controller.storage, "released", side_effect=team_storage.StorageError("closed")),
            self.assertRaises(ApiProblemError),
        ):
            self.controller.file_content("team_1", stored["id"], self.fail)
        with (
            mock.patch.object(team_storage, "MAX_ACKNOWLEDGEMENTS", 0),
            self.assertRaises(ApiProblemError) as caught,
        ):
            self.controller.acknowledge_file("team_1", stored["id"])
        self.assertEqual(caught.exception.code, "storage-quota-exceeded")

    def test_no_acknowledgement_exists_without_its_audit_or_outlives_its_team(self):
        stored = self.web_file()
        with (
            mock.patch.object(local_audit, "record_request", side_effect=OSError("audit closed")),
            self.assertRaises(OSError),
        ):
            self.controller.acknowledge_file("team_1", stored["id"])
        self.assertFalse(self.controller.storage.released("team_1", stored["id"], SUPERVISOR))
        # A Team whose files are gone has nothing to acknowledge, and nothing is created for it.
        self.controller.storage.destroy("team_1")
        with self.assertRaises(ApiProblemError) as caught:
            self.controller.acknowledge_file("team_1", stored["id"])
        self.assertEqual(caught.exception.code, "file-not-found")
        self.assertFalse((self.controller.storage.root / "team_1").exists())

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


class WebProvenanceTests(unittest.TestCase):
    """The web mark and each acknowledgement live with their file in the Team's one file store."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.now = [1_000_000]
        self.storage = team_storage.TeamStorage(Path(directory.name), clock=lambda: self.now[0])

    def test_a_web_mark_survives_an_identical_upload_and_goes_with_its_file(self):
        stored = self.storage.put("team_1", "edital.pdf", PDF, "application/pdf", web=True)
        again = self.storage.put("team_1", "edital.pdf", PDF, "application/pdf")
        self.assertEqual(again["id"], stored["id"])
        self.assertFalse(self.storage.released("team_1", stored["id"], None))
        self.storage.acknowledge("team_1", stored["id"], SUPERVISOR, lambda _file_id: None)
        self.assertTrue(self.storage.released("team_1", stored["id"], SUPERVISOR))
        upload = self.storage.put("team_1", "nota.txt", b"plain", "text/plain")
        self.assertTrue(self.storage.released("team_1", upload["id"], None))
        # Collected with its file, the acknowledgement is gone too.
        self.now[0] += team_storage.UNREFERENCED_GRACE_SECONDS + 1
        self.storage.settle("team_1", [])
        with self.assertRaises(team_storage.StorageNotFoundError):
            self.storage.released("team_1", stored["id"], SUPERVISOR)
        with self.assertRaises(team_storage.StorageNotFoundError):
            self.storage.acknowledge("team_1", stored["id"], SUPERVISOR, lambda _file_id: None)

    def test_the_web_mark_follows_the_bytes_whatever_their_name(self):
        upload = self.storage.put("team_1", "nota.pdf", PDF, "application/pdf")
        self.assertTrue(self.storage.released("team_1", upload["id"], None))
        self.storage.put("team_1", "edital.pdf", PDF, "application/pdf", web=True)
        # The earlier upload of the same bytes is marked with them, and so is a later one under any name.
        self.assertFalse(self.storage.released("team_1", upload["id"], None))
        later = self.storage.put("team_1", "copia.bin", PDF, "application/octet-stream")
        self.assertFalse(self.storage.released("team_1", later["id"], None))
        other = self.storage.put("team_1", "outro.txt", b"other bytes", "text/plain")
        self.assertTrue(self.storage.released("team_1", other["id"], None))

    def test_an_absent_store_or_principal_outside_its_form_refuses(self):
        with self.assertRaises(team_storage.StorageNotFoundError):
            self.storage.released("team_1", "f" * 32, SUPERVISOR)
        stored = self.storage.put("team_1", "edital.pdf", PDF, "application/pdf", web=True)
        for principal in ("", "x" * 129, "a\nb", 7):
            with self.subTest(principal=principal), self.assertRaises(team_storage.StorageInputError):
                self.storage.acknowledge("team_1", stored["id"], principal, lambda _file_id: None)
            with self.subTest(principal=principal), self.assertRaises(team_storage.StorageInputError):
                self.storage.released("team_1", stored["id"], principal)


if __name__ == "__main__":
    unittest.main()
