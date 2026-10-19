from __future__ import annotations

import json
import io
import os
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from cuoti_app import create_app
from cuoti_app.config import build_config
from cuoti_app.services.ai import DeepSeekServiceError, deepseek_api_call
from cuoti_app.services.ai_budgeting import resolve_model_context_capabilities
from cuoti_app.services.ai_providers import (
    MONETWL_BROWSER_USER_AGENT,
    OpenAICompatibleProvider,
)
from cuoti_app.services.settings import (
    KEYRING_SERVICE,
    SettingsError,
    SettingsService,
    _default_keyring_backend,
)


class FakeKeyring:
    def __init__(self):
        self.values = {}
        self.fail_on_password = None

    def get_password(self, service, username):
        return self.values.get((service, username))

    def set_password(self, service, username, password):
        if password == self.fail_on_password:
            raise RuntimeError("simulated keyring failure")
        self.values[(service, username)] = password

    def delete_password(self, service, username):
        self.values.pop((service, username), None)


class FakeResponse:
    headers = {"x-request-id": "request-test"}

    def __init__(self, content="OK", model="deepseek-test", finish_reason="stop", usage=None):
        self.payload = {
            "choices": [{"message": {"content": content}, "finish_reason": finish_reason}],
            "model": model,
            "usage": usage or {"total_tokens": 1},
        }

    def read(self, size=-1):
        if not hasattr(self, "stream"):
            self.stream = io.BytesIO(json.dumps(self.payload).encode("utf-8"))
        return self.stream.read(size)


class SSEResponse:
    """A minimal line-oriented streaming response for provider tests."""

    headers = {"x-request-id": "request-stream"}

    def __init__(self, body: str):
        self._stream = io.BytesIO(body.encode("utf-8"))

    def readline(self):
        return self._stream.readline()

    def read(self, size=-1):
        return self._stream.read(size)


def request_header(request, name: str):
    """Read a request header regardless of urllib's key capitalization."""

    for key, value in request.headers.items():
        if key.casefold() == name.casefold():
            return value
    return None


@unittest.skipUnless(os.name == "nt", "Windows credential backend only")
class DefaultKeyringTests(unittest.TestCase):
    def test_default_keyring_backend_avoids_fail_backend_on_windows(self):
        backend = _default_keyring_backend()
        self.assertIsNotNone(backend)
        self.assertNotEqual(type(backend).__module__, "keyring.backends.fail")
        self.assertTrue(callable(getattr(backend, "get_password", None)))


class AISettingsTests(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict(os.environ, {}, clear=True)
        self.environment.start()
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.settings_path = self.root / "settings.json"
        self.settings_path.write_text("{}\n", encoding="utf-8")
        self.app = create_app(
            {
                "TESTING": True,
                "SETTINGS_PATH": str(self.settings_path),
                "DATABASE": str(self.root / "unused.db"),
            }
        )
        self.keyring = FakeKeyring()
        self.app.extensions["ai_settings_service"] = SettingsService(
            self.app, keyring_backend=self.keyring
        )
        self.client = self.app.test_client()

    def tearDown(self):
        self.tempdir.cleanup()
        self.environment.stop()

    def test_provider_short_flash_id_uses_published_capabilities(self):
        capabilities = resolve_model_context_capabilities("deepseek-flash")
        self.assertEqual(capabilities.context_window_tokens, 1_000_000)
        self.assertEqual(capabilities.max_output_tokens, 384_000)

    def test_interactive_session_error_explains_how_to_store_credentials(self):
        class InteractiveSessionError(Exception):
            winerror = 1312

        class FailingKeyring(FakeKeyring):
            def set_password(self, service, username, password):
                raise InteractiveSessionError()

        self.app.extensions["ai_settings_service"] = SettingsService(
            self.app, keyring_backend=FailingKeyring()
        )
        response = self.client.put(
            "/api/v2/settings/ai",
            json={"api_key_action": "replace", "api_key": "test-key"},
        )
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.get_json()["code"], "KEYRING_UNAVAILABLE")
        self.assertIn("交互式桌面会话", response.get_json()["error"])

    def test_put_stores_secret_only_in_keyring_and_hot_updates_config(self):
        secret = "private-test-key"
        response = self.client.put(
            "/api/v2/settings/ai",
            json={
                "provider": "openai_compatible",
                "base_url": "https://api.example.test/v1/chat/completions",
                "quality_model": "quality-v1",
                "fast_model": "fast-v1",
                "assignments": {"classification": "quality"},
                "api_key_action": "replace",
                "api_key": secret,
            },
        )
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        body = response.get_data(as_text=True)
        self.assertNotIn(secret, body)
        payload = response.get_json()
        self.assertTrue(payload["has_api_key"])
        self.assertEqual(payload["credential_source"], "windows_keyring")
        self.assertEqual(payload["version"], 1)
        self.assertEqual(payload["assignments"]["classification"], "quality")
        self.assertEqual(
            self.keyring.get_password(KEYRING_SERVICE, "deepseek"), secret
        )

        saved_text = self.settings_path.read_text(encoding="utf-8")
        self.assertNotIn(secret, saved_text)
        saved = json.loads(saved_text)
        profile = saved["ai"]["profiles"]["deepseek"]
        self.assertEqual(profile["quality_model"], "quality-v1")
        self.assertEqual(self.app.config["DEEPSEEK_MODEL"], "quality-v1")
        reloaded = build_config({"SETTINGS_PATH": str(self.settings_path)})
        self.assertEqual(reloaded["DEEPSEEK_MODEL"], "quality-v1")
        self.assertEqual(
            reloaded["DEEPSEEK_URL"],
            "https://api.example.test/v1/chat/completions",
        )
        self.assertEqual(list(self.root.glob(".settings.json.*.tmp")), [])

        stale = self.client.put(
            "/api/v2/settings/ai",
            json={"version": 0, "api_key_action": "keep"},
        )
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(stale.get_json()["code"], "SETTINGS_CONFLICT")

    def test_get_never_returns_key_and_environment_fields_are_read_only(self):
        with patch.dict(
            os.environ,
            {
                "DEEPSEEK_API_KEY": "environment-secret",
                "DEEPSEEK_MODEL": "environment-model",
            },
        ):
            response = self.client.get("/api/v2/settings/ai")
            self.assertEqual(response.status_code, 200)
            self.assertNotIn("environment-secret", response.get_data(as_text=True))
            payload = response.get_json()
            self.assertEqual(payload["credential_source"], "environment")
            self.assertIn("api_key", payload["managed_fields"])
            self.assertIn("quality_model", payload["managed_fields"])

            blocked = self.client.put(
                "/api/v2/settings/ai",
                json={"quality_model": "ui-model", "api_key_action": "keep"},
            )
            self.assertEqual(blocked.status_code, 409)
            self.assertEqual(blocked.get_json()["code"], "MANAGED_BY_ENVIRONMENT")

            allowed = self.client.put(
                "/api/v2/settings/ai",
                json={"provider": "openai_compatible", "api_key_action": "keep"},
            )
            self.assertEqual(allowed.status_code, 200)
            self.assertNotIn(
                "environment-model", self.settings_path.read_text(encoding="utf-8")
            )

    def test_reset_restores_preset_and_preserves_credential(self):
        self.keyring.set_password(KEYRING_SERVICE, "deepseek", "preserved-key")
        changed = self.client.put(
            "/api/v2/settings/ai",
            json={
                "version": 0,
                "base_url": "https://api.example.test/v1/chat/completions",
                "quality_model": "custom-quality",
                "fast_model": "custom-fast",
                "assignments": {"classification": "quality"},
            },
        )
        response = self.client.post(
            "/api/v2/settings/ai/reset",
            json={"version": changed.get_json()["version"]},
        )
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["base_url"], "https://api.deepseek.com/v1/chat/completions")
        self.assertEqual(payload["assignments"]["classification"], "fast")
        self.assertTrue(payload["has_api_key"])
        self.assertEqual(
            self.keyring.get_password(KEYRING_SERVICE, "deepseek"), "preserved-key"
        )

    def test_credential_delete_requires_separate_confirmation(self):
        self.keyring.set_password(KEYRING_SERVICE, "deepseek", "delete-me")
        rejected = self.client.delete(
            "/api/v2/settings/ai/credential", json={"version": 0}
        )
        self.assertEqual(rejected.status_code, 400)
        self.assertEqual(rejected.get_json()["code"], "CONFIRMATION_REQUIRED")
        response = self.client.delete(
            "/api/v2/settings/ai/credential",
            json={"version": 0, "confirm": True},
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.get_json()["has_api_key"])
        self.assertIsNone(self.keyring.get_password(KEYRING_SERVICE, "deepseek"))

    def test_migrate_legacy_moves_key_and_preserves_other_settings(self):
        secret = "legacy-private-key"
        self.settings_path.write_text(
            json.dumps(
                {"deepseek_api_key": secret, "xelatex_timeout": 45},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        response = self.client.post("/api/v2/settings/ai/migrate-legacy")
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.assertTrue(response.get_json()["migrated"])
        self.assertNotIn(secret, response.get_data(as_text=True))
        saved = json.loads(self.settings_path.read_text(encoding="utf-8"))
        self.assertNotIn("deepseek_api_key", saved)
        self.assertEqual(saved["xelatex_timeout"], 45)
        self.assertEqual(
            self.keyring.get_password(KEYRING_SERVICE, "deepseek"), secret
        )

    def test_keyring_replace_rolls_back_when_atomic_settings_save_fails(self):
        service = self.app.extensions["ai_settings_service"]
        self.keyring.set_password(KEYRING_SERVICE, "deepseek", "previous-key")
        before = self.settings_path.read_text(encoding="utf-8")
        failure = SettingsError("SETTINGS_IO", "模拟保存失败", status_code=500)

        with patch.object(service, "_write_document", side_effect=failure):
            response = self.client.put(
                "/api/v2/settings/ai",
                json={
                    "api_key_action": "replace",
                    "api_key": "replacement-key",
                    "version": 0,
                },
            )

        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.get_json()["code"], "SETTINGS_IO")
        self.assertEqual(
            self.keyring.get_password(KEYRING_SERVICE, "deepseek"), "previous-key"
        )
        self.assertEqual(self.settings_path.read_text(encoding="utf-8"), before)
        self.assertNotIn("replacement-key", response.get_data(as_text=True))

    def test_legacy_migration_restores_previous_key_when_save_fails(self):
        service = self.app.extensions["ai_settings_service"]
        self.keyring.set_password(KEYRING_SERVICE, "deepseek", "previous-key")
        self.settings_path.write_text(
            json.dumps({"deepseek_api_key": "legacy-key", "settings_revision": 3}),
            encoding="utf-8",
        )
        failure = SettingsError("SETTINGS_IO", "模拟保存失败", status_code=500)

        with patch.object(service, "_write_document", side_effect=failure):
            response = self.client.post("/api/v2/settings/ai/migrate-legacy")

        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.get_json()["code"], "SETTINGS_IO")
        self.assertEqual(
            self.keyring.get_password(KEYRING_SERVICE, "deepseek"), "previous-key"
        )
        saved = json.loads(self.settings_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["deepseek_api_key"], "legacy-key")
        self.assertEqual(saved["settings_revision"], 3)

    def test_failed_keyring_rollback_returns_distinct_sanitized_error(self):
        service = self.app.extensions["ai_settings_service"]
        self.keyring.values[(KEYRING_SERVICE, "deepseek")] = "previous-key"
        self.keyring.fail_on_password = "previous-key"
        failure = SettingsError("SETTINGS_IO", "模拟保存失败", status_code=500)

        with patch.object(service, "_write_document", side_effect=failure):
            response = self.client.put(
                "/api/v2/settings/ai",
                json={
                    "api_key_action": "replace",
                    "api_key": "replacement-key",
                    "version": 0,
                },
            )

        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.get_json()["code"], "SETTINGS_ROLLBACK_FAILED")
        body = response.get_data(as_text=True)
        self.assertNotIn("previous-key", body)
        self.assertNotIn("replacement-key", body)

    @patch("cuoti_app.services.ai_providers.direct_urlopen")
    def test_connection_uses_saved_key_without_returning_it(self, urlopen):
        secret = "keyring-test-key"
        self.keyring.set_password(KEYRING_SERVICE, "deepseek", secret)
        urlopen.return_value = FakeResponse()
        response = self.client.post("/api/v2/settings/ai/test")
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.assertTrue(response.get_json()["ok"])
        self.assertNotIn(secret, response.get_data(as_text=True))
        request = urlopen.call_args.args[0]
        self.assertEqual(request.headers["Authorization"], f"Bearer {secret}")

    @patch("cuoti_app.services.ai_providers.direct_urlopen")
    def test_legacy_facade_uses_keyring_provider(self, urlopen):
        self.keyring.set_password(KEYRING_SERVICE, "deepseek", "facade-key")
        urlopen.return_value = FakeResponse(content=" facade result ")
        with self.app.app_context():
            result = deepseek_api_call(
                [{"role": "user", "content": "test"}], max_attempts=1
            )
        self.assertEqual(result, "facade result")

    @patch("cuoti_app.services.ai_providers.direct_urlopen")
    def test_task_assignment_selects_configured_model_role(self, urlopen):
        self.keyring.set_password(KEYRING_SERVICE, "deepseek", "facade-key")
        self.settings_path.write_text(
            json.dumps(
                {
                    "ai": {
                        "active_profile": "deepseek",
                        "profiles": {
                            "deepseek": {
                                "provider": "openai_compatible",
                                "base_url": "https://api.example.test/v1/chat/completions",
                                "quality_model": "quality-v2",
                                "fast_model": "fast-v2",
                                "capabilities": {"context_window": 128000, "max_output_tokens": 64000},
                            }
                        },
                        "assignments": {"classification": "quality"},
                    }
                }
            ),
            encoding="utf-8",
        )
        urlopen.return_value = FakeResponse(content="ok", model="quality-v2")
        with self.app.app_context():
            deepseek_api_call(
                [{"role": "user", "content": "test"}],
                task="classification",
                max_attempts=1,
            )
        request = urlopen.call_args.args[0]
        body = json.loads(request.data)
        self.assertEqual(body["model"], "quality-v2")

    @patch("cuoti_app.services.ai_providers.direct_urlopen")
    def test_business_call_enables_thinking_and_expands_truncated_output(self, urlopen):
        self.keyring.set_password(KEYRING_SERVICE, "deepseek", "facade-key")
        urlopen.side_effect = [
            FakeResponse(content='{"ok":false}', finish_reason="length"),
            FakeResponse(
                content='{"ok":true}', finish_reason="stop",
                usage={"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30,
                       "completion_tokens_details": {"reasoning_tokens": 7}},
            ),
        ]
        with self.app.app_context():
            result = deepseek_api_call(
                [{"role": "user", "content": "test"}],
                task="problem_analysis", max_attempts=1,
            )
        self.assertEqual(result, '{"ok":true}')
        first=json.loads(urlopen.call_args_list[0].args[0].data)
        second=json.loads(urlopen.call_args_list[1].args[0].data)
        self.assertEqual(first["max_tokens"], 64_000)
        self.assertEqual(second["max_tokens"], 128_000)
        self.assertEqual(first["thinking"], {"type": "enabled"})
        self.assertEqual(first["reasoning_effort"], "high")
        self.assertEqual(first["response_format"], {"type": "json_object"})

    @patch("cuoti_app.services.ai_providers.direct_urlopen")
    def test_job_freezes_model_but_next_job_reads_updated_setting(self, urlopen):
        self.keyring.set_password(KEYRING_SERVICE, "deepseek", "facade-key")
        urlopen.side_effect = lambda *_args, **_kwargs: FakeResponse(content="ok")
        with self.app.app_context():
            deepseek_api_call([{"role":"user","content":"x"}], task="exam_generation", freeze_id="job-a", max_attempts=1)
            before=json.loads(urlopen.call_args.args[0].data)["model"]
            document=json.loads(self.settings_path.read_text(encoding="utf-8"))
            document.setdefault("ai",{}).setdefault("profiles",{}).setdefault("deepseek",{})["quality_model"]="new-quality"
            document["ai"]["profiles"]["deepseek"]["capabilities"] = {"context_window": 128000, "max_output_tokens": 64000}
            self.settings_path.write_text(json.dumps(document),encoding="utf-8")
            deepseek_api_call([{"role":"user","content":"x"}], task="exam_generation", freeze_id="job-a", max_attempts=1)
            frozen=json.loads(urlopen.call_args.args[0].data)["model"]
            deepseek_api_call([{"role":"user","content":"x"}], task="exam_generation", freeze_id="job-b", max_attempts=1)
            updated=json.loads(urlopen.call_args.args[0].data)["model"]
        self.assertEqual(frozen,before)
        self.assertEqual(updated,"new-quality")

    @patch("cuoti_app.services.ai_providers.direct_urlopen")
    def test_provider_errors_do_not_include_credentials(self, urlopen):
        secret = "must-not-leak"
        self.keyring.set_password(KEYRING_SERVICE, "deepseek", secret)
        urlopen.side_effect = urllib.error.HTTPError(
            "https://api.deepseek.com", 401, "unauthorized", {}, None
        )
        with self.app.app_context(), self.assertRaises(DeepSeekServiceError) as raised:
            deepseek_api_call([{"role": "user", "content": "test"}], max_attempts=1)
        self.assertNotIn(secret, str(raised.exception))

    def test_monetwl_profile_is_builtin_without_becoming_active(self):
        payload = self.client.get("/api/v2/settings/ai").get_json()
        self.assertEqual(payload["active_profile"], "deepseek")
        profile = payload["profiles"]["monetwl"]
        self.assertEqual(profile["name"], "GPT（Monetwl）")
        self.assertEqual(profile["connection_type"], "newapi_channel_conn")
        self.assertEqual(profile["provider"], "openai_compatible")
        self.assertEqual(profile["wire_api"], "chat_completions")
        self.assertEqual(profile["base_url"], "https://gateway-two.example/v1")
        self.assertEqual(profile["quality_model"], "gpt-6.1-sol")
        self.assertEqual(profile["fast_model"], "gpt-6-luna")
        self.assertEqual(profile["reasoning_effort"], "")
        self.assertEqual(profile["dns_mode"], "proxy")
        self.assertEqual(profile["dns_servers"], ["223.5.5.5", "119.29.29.29"])
        self.assertEqual(
            profile["capabilities"],
            {
                "json_object": False,
                "thinking": False,
                "reasoning_effort": False,
                "context_window": 0,
                "max_output_tokens": 0,
            },
        )
        self.assertFalse(profile["has_api_key"])

    def test_switching_to_monetwl_saves_preset_without_writing_a_key(self):
        response = self.client.put(
            "/api/v2/settings/ai",
            json={
                "active_profile": "monetwl",
                "version": 0,
                "api_key_action": "keep",
            },
        )
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        payload = response.get_json()
        self.assertEqual(payload["active_profile"], "monetwl")
        self.assertEqual(payload["base_url"], "https://gateway-two.example/v1")
        self.assertEqual(payload["quality_model"], "gpt-6.1-sol")
        self.assertEqual(payload["fast_model"], "gpt-6-luna")
        self.assertEqual(payload["reasoning_effort"], "")
        self.assertEqual(payload["dns_mode"], "proxy")
        self.assertFalse(payload["has_api_key"])
        stored = json.loads(self.settings_path.read_text(encoding="utf-8"))
        self.assertEqual(stored["ai"]["active_profile"], "monetwl")
        saved = stored["ai"]["profiles"]["monetwl"]
        self.assertEqual(saved["base_url"], "https://gateway-two.example/v1")
        self.assertNotIn("api_key", saved)
        self.assertNotIn("deepseek_api_key", stored)

    @patch("cuoti_app.services.ai_providers.direct_urlopen")
    def test_monetwl_roles_reach_provider_with_published_limits(self, urlopen):
        self.keyring.set_password(KEYRING_SERVICE, "monetwl", "monetwl-key")
        self.settings_path.write_text(
            json.dumps(
                {"ai": {"active_profile": "monetwl", "profiles": {"monetwl": {}}}}
            ),
            encoding="utf-8",
        )

        # Fast role resolves to gpt-6-luna and reaches provider.complete (no
        # "stream" key), carrying the browser User-Agent for the Monetwl host.
        urlopen.return_value = FakeResponse(content="ok", model="gpt-6-luna")
        with self.app.app_context():
            deepseek_api_call(
                [{"role": "user", "content": "x"}],
                task="classification",
                max_attempts=1,
            )
        request = urlopen.call_args.args[0]
        body = json.loads(request.data)
        self.assertEqual(body["model"], "gpt-6-luna")
        self.assertNotIn("stream", body)
        self.assertTrue(
            request.full_url.startswith("https://gateway-two.example/v1/chat/completions")
        )
        self.assertEqual(
            request_header(request, "User-Agent"), MONETWL_BROWSER_USER_AGENT
        )

        # Quality role resolves to gpt-6.1-sol.
        urlopen.reset_mock()
        urlopen.return_value = FakeResponse(content='{"ok": true}', model="gpt-6.1-sol")
        with self.app.app_context():
            deepseek_api_call(
                [{"role": "user", "content": "x"}],
                task="problem_analysis",
                max_attempts=1,
            )
        self.assertEqual(
            json.loads(urlopen.call_args.args[0].data)["model"], "gpt-6.1-sol"
        )

        # An explicit smaller profile limit still wins over the published value.
        capabilities = resolve_model_context_capabilities(
            "gpt-6-luna",
            configured_context_window=1000,
            configured_max_output_tokens=500,
        )
        self.assertEqual(
            (capabilities.context_window_tokens, capabilities.max_output_tokens),
            (1000, 500),
        )


class MonetwlBrowserUserAgentTests(unittest.TestCase):
    """Only the exact Monetwl host receives a browser User-Agent."""

    def _provider(self, base_url: str) -> OpenAICompatibleProvider:
        return OpenAICompatibleProvider(
            base_url,
            "secret-key",
            dns_mode="proxy",
            dns_servers=["223.5.5.5", "119.29.29.29"],
        )

    @patch("cuoti_app.services.ai_providers.direct_urlopen")
    def test_monetwl_model_list_and_stream_and_complete_send_browser_ua(self, urlopen):
        provider = self._provider("https://gateway-two.example/v1")

        models_response = FakeResponse()
        models_response.payload = {"data": [{"id": "gpt-6-luna"}]}
        urlopen.return_value = models_response
        provider.list_models(timeout=5)
        self.assertEqual(
            request_header(urlopen.call_args.args[0], "User-Agent"),
            MONETWL_BROWSER_USER_AGENT,
        )

        urlopen.reset_mock()
        urlopen.return_value = SSEResponse(
            'data: {"choices":[{"delta":{"content":"OK"},"finish_reason":null}]}\n\n'
            'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
            "data: [DONE]\n\n"
        )
        events = list(
            provider.stream_complete(
                [{"role": "user", "content": "hi"}],
                model="gpt-6-luna",
                max_attempts=1,
            )
        )
        self.assertEqual(events[-1].kind, "completed")
        self.assertEqual(
            request_header(urlopen.call_args.args[0], "User-Agent"),
            MONETWL_BROWSER_USER_AGENT,
        )

        urlopen.reset_mock()
        urlopen.return_value = FakeResponse(content="OK", model="gpt-6-luna")
        provider.complete(
            [{"role": "user", "content": "hi"}],
            model="gpt-6-luna",
            max_attempts=1,
        )
        self.assertEqual(
            request_header(urlopen.call_args.args[0], "User-Agent"),
            MONETWL_BROWSER_USER_AGENT,
        )

    @patch("cuoti_app.services.ai_providers.direct_urlopen")
    def test_non_monetwl_hosts_keep_requests_unchanged(self, urlopen):
        for base_url in (
            "https://api.deepseek.com/v1",
            "https://notgateway-two.example/v1",
            "https://gateway-two.example.evil.test/v1",
            "https://api.example.test/gateway-two.example/v1",
        ):
            provider = self._provider(base_url)
            response = FakeResponse()
            response.payload = {"data": [{"id": "some-model"}]}
            urlopen.return_value = response
            provider.list_models(timeout=5)
            request = urlopen.call_args.args[0]
            self.assertIsNone(
                request_header(request, "User-Agent"), msg=base_url
            )
            self.assertEqual(
                request_header(request, "Authorization"), "Bearer secret-key"
            )

    @patch("cuoti_app.services.ai_providers.direct_urlopen")
    def test_monetwl_host_match_ignores_case(self, urlopen):
        provider = self._provider("https://gateway-two.example/v1")
        response = FakeResponse()
        response.payload = {"data": [{"id": "gpt-6-luna"}]}
        urlopen.return_value = response
        provider.list_models(timeout=5)
        self.assertEqual(
            request_header(urlopen.call_args.args[0], "User-Agent"),
            MONETWL_BROWSER_USER_AGENT,
        )


if __name__ == "__main__":
    unittest.main()
