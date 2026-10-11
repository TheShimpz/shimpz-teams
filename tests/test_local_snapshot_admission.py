"""One admission verdict per staged Local snapshot, reused per immutable image and reported in the Local inventory."""

import threading
import unittest
from types import SimpleNamespace
from unittest import mock

from docker.errors import DockerException, ImageNotFound

from local.install import admission, service, snapshots
from protocol.http.v1 import payload as http_payload
from tests import catalog_fixtures
from tests.local_snapshot_fixtures import CREATED, IMAGE_ID
from tests.local_snapshot_fixtures import client as _client
from tests.test_assistant_manifest import manifest

# A Stored Input the current contract refuses: it names its host and header but declares no reviewed routes.
UNROUTED_STORED_INPUT = manifest(
    integrations=(
        "[stored_inputs.api-token]\n"
        'kind = "password"\n'
        'label = "API token"\n'
        f'description = "{catalog_fixtures.STORED_INPUT_HELP}"\n'
        f'help_url = "{catalog_fixtures.HELP_URL}"\n'
        'host = "api.example.com"\n'
        'header = "X-Api-Key"\n'
    )
)


def _candidate(image_id: str = IMAGE_ID, assistant_id: str = "fixture-assistant") -> snapshots.LocalSnapshotCandidate:
    return snapshots.LocalSnapshotCandidate(
        assistant_id,
        "0.1.0",
        "Fixture Assistant",
        "Exercise immutable admission.",
        ("@fixture",),
        ("ping",),
        (),
        image_id,
        "linux/amd64",
        CREATED,
    )


def _image_id(index: int) -> str:
    return f"sha256:{index:064x}"


def _observe_join(admissions: admission.LocalSnapshotAdmissions) -> threading.Event:
    """An event set once another read waits on the fixture image's in-flight judgment."""
    judgment = admissions._judgments[admission._key(_candidate())]
    joined = threading.Event()
    result = judgment.result

    def observed(*args: object, **kwargs: object) -> snapshots.Admission:
        joined.set()
        return result(*args, **kwargs)

    judgment.result = observed
    return joined


ADMISSIBLE = snapshots.Admission(snapshots.ADMISSIBLE)
INADMISSIBLE = snapshots.Admission(snapshots.INADMISSIBLE, snapshots.DECLARATION_INVALID)
UNAVAILABLE = snapshots.Admission(snapshots.UNAVAILABLE)


class SnapshotVerdictTests(unittest.TestCase):
    def test_a_snapshot_the_full_install_admission_accepts_is_admissible(self) -> None:
        client, _image, _container = _client()

        self.assertEqual(snapshots.admission(client, IMAGE_ID), ADMISSIBLE)

    def test_a_declaration_the_current_contract_refuses_is_inadmissible(self) -> None:
        client, _image, container = _client(declaration=UNROUTED_STORED_INPUT)

        self.assertEqual(snapshots.admission(client, IMAGE_ID), INADMISSIBLE)
        container.remove.assert_called_once_with(force=True, v=False)

    def test_a_label_only_a_preview_compares_makes_the_snapshot_inadmissible(self) -> None:
        # Install admission once compared only the identity, Creators, and capabilities with the labels; the preview
        # also compares the name and summary, so a snapshot it refuses must never be offered or installed.
        for label in (snapshots.NAME_LABEL, snapshots.SUMMARY_LABEL):
            with self.subTest(label=label):
                client, image, _container = _client()
                image.attrs["Config"]["Labels"][label] = "Another label"

                self.assertEqual(
                    snapshots.admission(client, IMAGE_ID),
                    snapshots.Admission(snapshots.INADMISSIBLE, snapshots.IMAGE_INCONSISTENT),
                )
                with self.assertRaisesRegex(snapshots.InconsistentSnapshotError, "does not match its image labels"):
                    snapshots.admit(client, IMAGE_ID)
                with self.assertRaisesRegex(snapshots.InconsistentSnapshotError, "does not match its image labels"):
                    snapshots.preview(client, IMAGE_ID)

    def test_an_image_without_a_stage_file_is_inconsistent(self) -> None:
        client, _image, _container = _client(pack=None)

        self.assertEqual(
            snapshots.admission(client, IMAGE_ID),
            snapshots.Admission(snapshots.INADMISSIBLE, snapshots.IMAGE_INCONSISTENT),
        )

    def test_docker_failing_to_answer_is_unavailable_never_inadmissible(self) -> None:
        for failure in (ImageNotFound("removed"), DockerException("offline")):
            with self.subTest(failure=type(failure).__name__):
                client, _image, _container = _client()
                client.images.get.side_effect = failure

                self.assertEqual(snapshots.admission(client, IMAGE_ID), UNAVAILABLE)
        client, _image, _container = _client()
        client.containers.create.side_effect = DockerException("offline")

        self.assertEqual(snapshots.admission(client, IMAGE_ID), UNAVAILABLE)


class AdmissionCacheTests(unittest.TestCase):
    def test_a_settled_verdict_is_reused_for_the_exact_image_and_tag_set(self) -> None:
        for verdict in (ADMISSIBLE, INADMISSIBLE):
            with self.subTest(verdict=verdict.state):
                judge = mock.Mock(return_value=verdict)
                admissions = admission.LocalSnapshotAdmissions(object(), judge=judge)

                (first,) = admissions.judge((_candidate(),))
                (second,) = admissions.judge((_candidate(),))

                self.assertEqual((first.admission, first.fresh), (verdict, True))
                self.assertEqual((second.admission, second.fresh), (verdict, False))
                judge.assert_called_once()

    def test_an_unavailable_verdict_is_judged_again_on_every_read(self) -> None:
        judge = mock.Mock(side_effect=(UNAVAILABLE, ADMISSIBLE, ADMISSIBLE))
        admissions = admission.LocalSnapshotAdmissions(object(), judge=judge)

        self.assertEqual(admissions.judge((_candidate(),))[0].admission, UNAVAILABLE)
        self.assertEqual(admissions.judge((_candidate(),))[0].admission, ADMISSIBLE)
        self.assertEqual(admissions.judge((_candidate(),))[0].admission, ADMISSIBLE)
        self.assertEqual(judge.call_count, 2)

    def test_another_tag_set_for_the_same_image_is_judged_again(self) -> None:
        judge = mock.Mock(side_effect=(ADMISSIBLE, INADMISSIBLE))
        admissions = admission.LocalSnapshotAdmissions(object(), judge=judge)

        admissions.judge((_candidate(),))
        (retagged,) = admissions.judge((_candidate(assistant_id="other-assistant"),))

        self.assertEqual((retagged.admission, retagged.fresh), (INADMISSIBLE, True))
        self.assertEqual(judge.call_count, 2)

    def test_the_cache_keeps_only_the_most_recently_read_verdicts(self) -> None:
        judge = mock.Mock(return_value=ADMISSIBLE)
        admissions = admission.LocalSnapshotAdmissions(object(), judge=judge)
        count = admission.MAX_CACHED_VERDICTS + 1

        admissions.judge(tuple(_candidate(_image_id(index)) for index in range(count)))
        self.assertEqual(judge.call_count, count)
        admissions.judge((_candidate(_image_id(count - 1)),))
        self.assertEqual(judge.call_count, count)
        (evicted,) = admissions.judge((_candidate(_image_id(0)),))

        self.assertTrue(evicted.fresh)
        self.assertEqual(judge.call_count, count + 1)
        self.assertEqual(admissions.judge(()), ())

    def test_judgments_extract_at_most_the_bounded_number_of_images_at_once(self) -> None:
        lock = threading.Lock()
        running = 0
        peak = 0
        released = threading.Event()

        def judge(_client: object, _image_id: str) -> snapshots.Admission:
            nonlocal running, peak
            with lock:
                running += 1
                peak = max(peak, running)
                if running == admission.MAX_CONCURRENT_EXTRACTIONS:
                    released.set()
            released.wait(timeout=5)
            with lock:
                running -= 1
            return ADMISSIBLE

        admissions = admission.LocalSnapshotAdmissions(object(), judge=judge)
        reads = [
            threading.Thread(
                target=admissions.judge,
                args=(tuple(_candidate(_image_id(offset + index)) for index in range(4)),),
            )
            for offset in (0, 100)
        ]
        for read in reads:
            read.start()
        for read in reads:
            read.join(timeout=10)

        self.assertEqual(peak, admission.MAX_CONCURRENT_EXTRACTIONS)

    def test_concurrent_reads_of_one_uncached_image_join_one_judgment(self) -> None:
        started = threading.Event()
        release = threading.Event()
        judge = mock.Mock()

        def judged(_client: object, _image_id: str) -> snapshots.Admission:
            started.set()
            release.wait(timeout=5)
            return INADMISSIBLE

        judge.side_effect = judged
        admissions = admission.LocalSnapshotAdmissions(object(), judge=judge)
        results: list[admission.Judged] = []
        owner = threading.Thread(target=lambda: results.extend(admissions.judge((_candidate(),))))
        owner.start()
        self.assertTrue(started.wait(timeout=5))
        joined = _observe_join(admissions)
        joiner = threading.Thread(target=lambda: results.extend(admissions.judge((_candidate(),))))
        joiner.start()
        self.assertTrue(joined.wait(timeout=5))
        release.set()
        owner.join(timeout=5)
        joiner.join(timeout=5)

        self.assertEqual(sorted(item.fresh for item in results), [False, True])
        self.assertEqual({item.admission for item in results}, {INADMISSIBLE})
        judge.assert_called_once()

    def test_a_failed_judgment_reaches_every_joined_read_and_is_never_remembered(self) -> None:
        started = threading.Event()
        release = threading.Event()
        calls = 0

        def judge(_client: object, _image_id: str) -> snapshots.Admission:
            nonlocal calls
            calls += 1
            if calls == 1:
                started.set()
                release.wait(timeout=5)
                raise RuntimeError("judge failed")
            return ADMISSIBLE

        admissions = admission.LocalSnapshotAdmissions(object(), judge=judge)
        failures: list[BaseException] = []

        def read() -> None:
            try:
                admissions.judge((_candidate(),))
            except RuntimeError as exc:
                failures.append(exc)

        owner = threading.Thread(target=read)
        owner.start()
        self.assertTrue(started.wait(timeout=5))
        joined = _observe_join(admissions)
        joiner = threading.Thread(target=read)
        joiner.start()
        self.assertTrue(joined.wait(timeout=5))
        release.set()
        owner.join(timeout=5)
        joiner.join(timeout=5)

        self.assertEqual([str(failure) for failure in failures], ["judge failed", "judge failed"])
        self.assertEqual(admissions.judge((_candidate(),))[0].admission, ADMISSIBLE)
        self.assertEqual(calls, 2)


class LocalInventoryVerdictTests(unittest.TestCase):
    def _list(self, candidates, admissions):
        controller = SimpleNamespace(
            local_snapshot_inventory=SimpleNamespace(candidates=lambda: candidates),
            local_snapshot_admissions=admissions,
        )
        with mock.patch.object(service.local_audit, "record_request") as audit:
            result = service.list_local_snapshots(controller)
        return result, audit

    def test_every_staged_identity_is_listed_with_its_verdict_and_never_its_content(self) -> None:
        candidates = (_candidate(_image_id(1), "admitted"), _candidate(_image_id(2), "refused"))
        candidates += (_candidate(_image_id(3), "unanswered"),)
        verdicts = {_image_id(1): ADMISSIBLE, _image_id(2): INADMISSIBLE, _image_id(3): UNAVAILABLE}
        admissions = admission.LocalSnapshotAdmissions(object(), judge=lambda _client, image_id: verdicts[image_id])

        result, audit = self._list(candidates, admissions)

        entries = result["assistants"]
        self.assertEqual(
            [(entry["assistant_id"], entry["admission"], entry["reason"]) for entry in entries],
            [
                ("admitted", "admissible", None),
                ("refused", "inadmissible", "declaration-invalid"),
                ("unanswered", "unavailable", None),
            ],
        )
        self.assertTrue(all(http_payload.canonical_local_assistant(entry) == entry for entry in entries))
        self.assertEqual(
            audit.call_args_list,
            [
                mock.call(
                    "local-assistant-inadmissible",
                    result="denied",
                    assistant="refused",
                    detail=f"declaration-invalid:{_image_id(2)}",
                ),
                mock.call(
                    "local-assistant-admission-unavailable",
                    result="error",
                    assistant="unanswered",
                    detail=_image_id(3),
                ),
            ],
        )

    def test_a_cached_refusal_is_audited_once_and_an_unavailable_read_every_time(self) -> None:
        candidates = (_candidate(_image_id(1), "refused"), _candidate(_image_id(2), "unanswered"))
        verdicts = {_image_id(1): INADMISSIBLE, _image_id(2): UNAVAILABLE}
        admissions = admission.LocalSnapshotAdmissions(object(), judge=lambda _client, image_id: verdicts[image_id])

        self._list(candidates, admissions)
        _result, audit = self._list(candidates, admissions)

        self.assertEqual(
            [call.args[0] for call in audit.call_args_list],
            ["local-assistant-admission-unavailable"],
        )

    def test_a_real_staged_snapshot_with_an_unrouted_stored_input_is_listed_inadmissible(self) -> None:
        client, _image, _container = _client(declaration=UNROUTED_STORED_INPUT)
        admissions = admission.LocalSnapshotAdmissions(client)

        result, audit = self._list(snapshots.list_candidates(client), admissions)

        ((entry,),) = (result["assistants"],)
        self.assertEqual((entry["admission"], entry["reason"]), ("inadmissible", "declaration-invalid"))
        audit.assert_called_once()


if __name__ == "__main__":
    unittest.main()
