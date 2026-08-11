from __future__ import annotations

import unittest
from unittest import mock

import docker
from local_assistant_fixture import hosted_spec

from assistant import spec as assistant_registry
from install import artifact as assistant_artifact

AUTH_CONFIG = {"username": "registry-reader", "password": "x" * 20}
AUTH = type("_Auth", (), {"docker_auth_config": lambda self: AUTH_CONFIG})()
TEST_IMAGE = "ghcr.io/example/example-assistant@sha256:" + ("a" * 64)


class _Image:
    def __init__(
        self,
        *,
        repo_digests: list[str] | None = None,
        labels: dict[str, str] | None = None,
        image_id: str = "sha256:" + "b" * 64,
    ) -> None:
        self.id = image_id
        self.attrs = {"RepoDigests": repo_digests, "Config": {"Labels": labels}}


class _Images:
    def __init__(self, image: _Image | None, *, missing_once: bool = False) -> None:
        self.image = image
        self.missing_once = missing_once
        self.gets: list[str] = []
        self.pulls: list[str] = []

    def get(self, image_ref: str) -> _Image:
        self.gets.append(image_ref)
        if self.missing_once:
            self.missing_once = False
            raise docker.errors.ImageNotFound("missing")
        if self.image is None:
            raise docker.errors.ImageNotFound("missing")
        return self.image

    def pull(
        self,
        image_ref: str,
        *,
        auth_config: dict[str, str],
    ) -> _Image | None:
        if auth_config != AUTH_CONFIG:
            raise AssertionError("private registry credentials were not forwarded")
        self.pulls.append(image_ref)
        return self.image


def _assistant_image(
    *,
    digest: str = TEST_IMAGE,
    assistant_id: str = "shimpz-cloudflare",
    source_digest: str = "sha256:" + ("c" * 64),
) -> _Image:
    return _Image(
        repo_digests=[digest],
        labels={
            "org.shimpz.assistant.id": assistant_id,
            "org.shimpz.source.digest": source_digest,
        },
    )


class AssistantArtifactTests(unittest.TestCase):
    def test_publication_fixture_carries_the_verified_runtime_contract(self) -> None:
        spec = hosted_spec(TEST_IMAGE)
        self.assertEqual(spec.image, TEST_IMAGE)
        self.assertTrue(assistant_registry.is_digest_image(spec.image))
        self.assertEqual(spec.allowed_hosts, ("api.cloudflare.com",))
        self.assertEqual(
            dict(spec.required_image_labels),
            {
                "org.shimpz.assistant.id": "shimpz-cloudflare",
                "org.shimpz.source.digest": "sha256:" + ("c" * 64),
            },
        )
        self.assertFalse(hasattr(spec.contract, "rpc_command"))
        self.assertEqual(set(spec.contract.actions), {"list-zones", "list-dns-records"})
        self.assertEqual(spec.contract.integrations["cloudflare"].provider, "cloudflare")
        self.assertEqual(
            spec.contract.integrations["cloudflare"].scopes,
            ("dns.read", "offline_access", "zone.read"),
        )
        self.assertTrue(all(action.integrations == ("cloudflare",) for action in spec.contract.actions.values()))
        self.assertTrue(all(not hasattr(action, "approval") for action in spec.contract.actions.values()))

    def test_missing_digest_is_pulled_by_the_exact_registry_reference_then_rechecked(self) -> None:
        spec = hosted_spec(TEST_IMAGE)
        images = _Images(_assistant_image(), missing_once=True)
        self.assertEqual(
            assistant_artifact.ensure_digest_artifact(images, spec, AUTH),
            "sha256:" + "b" * 64,
        )
        self.assertEqual(images.gets, [spec.image, spec.image])
        self.assertEqual(images.pulls, [spec.image])

    def test_digest_or_assistant_label_mismatch_is_refused_without_a_pull(self) -> None:
        spec = hosted_spec(TEST_IMAGE)
        mismatches = (
            _assistant_image(digest="ghcr.io/theshimpz/shimpz-assistant@sha256:" + "c" * 64),
            _assistant_image(assistant_id="other-assistant"),
            _assistant_image(source_digest="sha256:" + ("d" * 64)),
        )
        for image in mismatches:
            with self.subTest(attrs=image.attrs):
                images = _Images(image)
                with self.assertRaises(assistant_artifact.ImageTrustError):
                    assistant_artifact.ensure_digest_artifact(images, spec, AUTH)
                self.assertEqual(images.pulls, [])

    def test_tag_backed_artifact_is_not_eligible_for_registry_pull(self) -> None:
        spec = hosted_spec(TEST_IMAGE)
        object.__setattr__(spec, "image", "ghcr.io/example/example-assistant:latest")
        images = _Images(_assistant_image())
        self.assertFalse(assistant_registry.is_digest_image(spec.image))
        with self.assertRaises(assistant_artifact.ImageTrustError):
            assistant_artifact.ensure_digest_artifact(images, spec, AUTH)
        self.assertEqual(images.gets, [])
        self.assertEqual(images.pulls, [])

    def test_cloudflare_action_input_and_output_contracts_are_closed(self) -> None:
        spec = hosted_spec(TEST_IMAGE)
        assert spec.contract is not None
        action = spec.contract.actions["list-zones"]
        request = {"page": 1, "per_page": 25}
        self.assertEqual(assistant_registry.validate_action_payload(action, "input", request), request)
        zones = {
            "zones": [],
            "pagination": {"page": 1, "per_page": 25, "count": 0, "total_count": 0, "total_pages": 0},
        }
        self.assertEqual(assistant_registry.validate_action_payload(action, "output", zones), zones)
        for payload in ({"page": 1, "per_page": 25, "shell": "id"}, {"page": 0, "per_page": 25}, []):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                assistant_registry.validate_action_payload(action, "input", payload)

    def test_docker_lookup_and_pull_failures_are_redacted(self) -> None:
        spec = hosted_spec(TEST_IMAGE)
        images = mock.Mock()
        images.get.side_effect = docker.errors.APIError("private transport detail")
        with self.assertRaisesRegex(assistant_artifact.ImageTrustError, "unavailable"):
            assistant_artifact.ensure_digest_artifact(images, spec, AUTH)

        images.get.side_effect = docker.errors.ImageNotFound("missing")
        images.pull.side_effect = docker.errors.APIError("private registry detail")
        with self.assertRaisesRegex(assistant_artifact.ImageTrustError, "unavailable"):
            assistant_artifact.ensure_digest_artifact(images, spec, AUTH)

    def test_pull_that_does_not_materialize_the_digest_fails_closed(self) -> None:
        spec = hosted_spec(TEST_IMAGE)
        images = _Images(None)

        with self.assertRaisesRegex(assistant_artifact.ImageTrustError, "unavailable"):
            assistant_artifact.ensure_digest_artifact(images, spec, AUTH)
        self.assertEqual(images.pulls, [TEST_IMAGE])


if __name__ == "__main__":
    unittest.main()
