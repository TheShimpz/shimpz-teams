"""Local Stop interrupts its Team's running turn even when continuation cleanup fails.

The withdrawn continuation and its paused Action batch are cleaned first; when that cleanup fails, the cancelled turn's
Brain request is still aborted and its executing Action fail-stopped before the error is reported.
"""

from __future__ import annotations

import sys
import threading
import unittest
from contextlib import nullcontext
from http import HTTPStatus
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from local.chat import resume as local_chat_resume
from local.errors import ApiProblemError as ApiProblem

TOKEN = "turn-token"


class LocalStopCleanupFailureTests(unittest.TestCase):
    def _service(self, *, delete: object, purge: object) -> SimpleNamespace:
        withdrawn = SimpleNamespace(id="challenge", payload=SimpleNamespace())
        return SimpleNamespace(
            _lock=lambda _team_id: nullcontext(),
            _active_chat_guard=threading.Lock(),
            _routine_holders=set(),
            _active_chat_tokens={"team_1": TOKEN},
            _cancelled_chat_tokens=set(),
            _active_action_containers={"team_1": (TOKEN, "action-container")},
            _brain_aborts={TOKEN: SimpleNamespace(abort=mock.Mock())},
            assistant_lifecycle=SimpleNamespace(_network=mock.Mock(), _fail_stop_action=mock.Mock()),
            integration_challenges=SimpleNamespace(withdraw_team=mock.Mock(return_value=None)),
            human_challenges=SimpleNamespace(withdraw_team=mock.Mock(return_value=withdrawn)),
            oauth_pkce=SimpleNamespace(cancel_team=mock.Mock()),
            _delete_withdrawn_continuation=mock.Mock(side_effect=delete),
            _purge_human_pending=mock.Mock(side_effect=purge),
        )

    def _assert_interrupted(self, service: SimpleNamespace) -> None:
        self.assertEqual(service._cancelled_chat_tokens, {TOKEN})
        service._brain_aborts[TOKEN].abort.assert_called_once_with()
        service.assistant_lifecycle._fail_stop_action.assert_called_once_with("action-container")

    def test_a_failing_continuation_deletion_still_interrupts_the_turn(self) -> None:
        failure = ApiProblem(HTTPStatus.SERVICE_UNAVAILABLE, "chat state is unavailable", code="unavailable")
        service = self._service(delete=failure, purge=None)

        with self.assertRaises(ApiProblem) as raised:
            local_chat_resume.stop_chat(service, "team_1")

        self.assertIs(raised.exception, failure)
        self._assert_interrupted(service)

    def test_a_failing_batch_purge_still_interrupts_the_turn(self) -> None:
        failure = ApiProblem(HTTPStatus.SERVICE_UNAVAILABLE, "Action state is unavailable", code="unavailable")
        service = self._service(delete=lambda *_args: True, purge=failure)

        with self.assertRaises(ApiProblem) as raised:
            local_chat_resume.stop_chat(service, "team_1")

        self.assertIs(raised.exception, failure)
        self._assert_interrupted(service)


if __name__ == "__main__":
    unittest.main()
