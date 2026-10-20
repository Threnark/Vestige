"""Tests for ai_providers: JSON handling, validation, the OpenAI-compatible
adapter (against a fake localhost server), the Anthropic adapter (against a
stub SDK) and the privacy summary. No real network or API key is used."""
import json
import sys
import threading
import types
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

import ai_providers as ai
from models import MODE_CLOUD, MODE_LOCAL, KIND_ANTHROPIC, KIND_OPENAI, ProviderSettings
from prompts.classification_prompt import CLASSIFY_TOOL, ENUM_FALLBACKS, ESSENTIAL_KEYS

GOOD = {"results": [{"group_id": "spotify.com", "is_signup": True, "service_name": "Spotify",
                     "category": "entertainment_streaming", "signup_method": "native_password",
                     "confidence": 0.9, "representative_sample_index": 0, "reasoning": "welcome email"}]}


def chat_reply(content, finish="stop"):
    return 200, json.dumps({"choices": [{"message": {"content": content}, "finish_reason": finish}],
                            "usage": {"prompt_tokens": 11, "completion_tokens": 7}}), {}


class FakeServer:
    """A tiny OpenAI-style server. `script` is a list of callables
    (request_body, headers) -> (status, text, extra_headers); the last one repeats."""

    def __init__(self, script):
        self.script, self.requests, self.headers_seen = script, [], []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):          # keep test output quiet
                pass

            def _reply(self, status, text, extra):
                data = text.encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                for k, v in (extra or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.requests.append(body)
                outer.headers_seen.append(dict(self.headers))
                step = outer.script[min(len(outer.requests) - 1, len(outer.script) - 1)]
                self._reply(*step(body, self.headers))

            def do_GET(self):
                outer.headers_seen.append(dict(self.headers))
                self._reply(200, json.dumps({"data": [{"id": "llama3.1:8b"}, {"id": "qwen2.5:14b"}]}), {})

        self.httpd = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_port}/v1"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def call(provider):
    provider._sleep = lambda s: None
    return provider.structured_call("SYS", "USER", CLASSIFY_TOOL, ESSENTIAL_KEYS, ENUM_FALLBACKS)


class JsonHandling(unittest.TestCase):
    def test_extract_json_variants(self):
        self.assertEqual(ai.extract_json('{"a": 1}'), {"a": 1})
        self.assertEqual(ai.extract_json('Sure!\n```json\n{"a": 1}\n```\nDone'), {"a": 1})
        self.assertEqual(ai.extract_json('<think>hmm {"x":0}</think>{"a": 2}'), {"a": 2})
        self.assertEqual(ai.extract_json('blah blah {"a": {"b": "}"}} trailing'), {"a": {"b": "}"}})
        with self.assertRaises(ai.ProviderFormatError):
            ai.extract_json("no json here")
        with self.assertRaises(ai.ProviderFormatError):
            ai.extract_json("<think>never finished")

    def test_validate_coerces_and_defaults(self):
        data = {"results": [{"group_id": "x.com", "is_signup": "true", "confidence": "7",
                             "category": "Social Media", "signup_method": "carrier pigeon"}]}
        item = ai.validate_object_list(data, CLASSIFY_TOOL["input_schema"], ESSENTIAL_KEYS, ENUM_FALLBACKS)[0]
        self.assertIs(item["is_signup"], True)
        self.assertEqual(item["confidence"], 1.0)                 # clamped
        self.assertEqual(item["category"], "social_media")        # normalised
        self.assertEqual(item["signup_method"], "unknown")        # fallback
        self.assertEqual(item["service_name"], "")                # default for a missing key

    def test_items_missing_essentials_are_dropped(self):
        data = [{"group_id": "a.com", "is_signup": False}, {"is_signup": True}]
        out = ai.validate_object_list(data, CLASSIFY_TOOL["input_schema"], ESSENTIAL_KEYS, ENUM_FALLBACKS)
        self.assertEqual([o["group_id"] for o in out], ["a.com"])

    def test_nothing_usable_raises(self):
        with self.assertRaises(ai.ProviderFormatError):
            ai.validate_object_list({"results": []}, CLASSIFY_TOOL["input_schema"], ESSENTIAL_KEYS)


class OpenAICompatible(unittest.TestCase):
    def setUp(self):
        self.servers = []

    def tearDown(self):
        for s in self.servers:
            s.close()

    def server(self, *script):
        s = FakeServer(list(script))
        self.servers.append(s)
        return s

    def local(self, s, **kw):
        return ai.OpenAICompatibleProvider(s.url, "llama3.1:8b", is_local=True, **kw)

    def test_happy_path_uses_json_schema_and_sends_no_auth_header_when_local(self):
        s = self.server(lambda b, h: chat_reply(json.dumps(GOOD)))
        p = self.local(s)
        out = call(p)
        self.assertEqual(out[0]["service_name"], "Spotify")
        self.assertEqual(s.requests[0]["response_format"]["type"], "json_schema")
        self.assertNotIn("Authorization", s.headers_seen[0])
        self.assertEqual(s.requests[0]["temperature"], 0)
        self.assertIn("JSON Schema", s.requests[0]["messages"][0]["content"])   # schema is also in the prompt
        self.assertEqual(p.last_usage, (11, 7))

    def test_ladder_falls_back_and_remembers(self):
        reject = lambda text: (lambda b, h: (400, json.dumps({"error": text}), {}))
        s = self.server(reject("response_format json_schema is not supported"),
                        reject("json_object response_format not supported"),
                        lambda b, h: chat_reply(json.dumps(GOOD)))
        p = self.local(s)
        call(p)
        self.assertEqual([r.get("response_format", {}).get("type") for r in s.requests],
                         ["json_schema", "json_object", None])
        self.assertEqual(p._unsupported_modes, {"json_schema", "json_object"})
        call(p)                                                    # now goes straight to the working format
        self.assertIsNone(s.requests[-1].get("response_format"))
        self.assertEqual(len(s.requests), 4)

    def test_fenced_and_thinking_replies_are_understood(self):
        s = self.server(lambda b, h: chat_reply("<think>reasoning...</think>\n```json\n" + json.dumps(GOOD) + "\n```"))
        self.assertEqual(call(self.local(s))[0]["group_id"], "spotify.com")

    def test_invalid_json_is_retried_with_feedback(self):
        s = self.server(lambda b, h: chat_reply("Sorry, I can't"), lambda b, h: chat_reply(json.dumps(GOOD)))
        out = call(self.local(s))
        self.assertEqual(out[0]["group_id"], "spotify.com")
        self.assertIn("could not be used", s.requests[1]["messages"][1]["content"])

    def test_gives_up_after_repeated_garbage(self):
        s = self.server(lambda b, h: chat_reply("nope"))
        with self.assertRaises(ai.ProviderFormatError):
            call(self.local(s))
        self.assertEqual(len(s.requests), 3)                      # 1 try + 2 retries

    def test_truncated_reply_is_reported(self):
        s = self.server(lambda b, h: chat_reply('{"results": [', finish="length"))
        with self.assertRaises(ai.ProviderFormatError) as cm:
            call(self.local(s))
        self.assertIn("cut off", str(cm.exception))

    def test_auth_error_is_not_retried(self):
        s = self.server(lambda b, h: (401, '{"error":"bad key"}', {}))
        with self.assertRaises(ai.ProviderAuthError):
            call(self.local(s))
        self.assertEqual(len(s.requests), 1)

    def test_rate_limit_then_success(self):
        s = self.server(lambda b, h: (429, "slow down", {"Retry-After": "0"}),
                        lambda b, h: chat_reply(json.dumps(GOOD)))
        self.assertEqual(call(self.local(s))[0]["group_id"], "spotify.com")
        self.assertEqual(len(s.requests), 2)

    def test_persistent_server_error_becomes_connection_error(self):
        s = self.server(lambda b, h: (503, "down", {}))
        with self.assertRaises(ai.ProviderConnectionError):
            call(self.local(s))
        self.assertEqual(len(s.requests), ai.AIProvider.MAX_ATTEMPTS)

    def test_max_completion_tokens_switch(self):
        s = self.server(lambda b, h: (400, json.dumps({"error": "Unsupported parameter: 'max_tokens'. "
                                                       "Use 'max_completion_tokens' instead."}), {}),
                        lambda b, h: chat_reply(json.dumps(GOOD)))
        p = self.local(s)
        call(p)
        self.assertIn("max_tokens", s.requests[0])
        self.assertIn("max_completion_tokens", s.requests[1])
        self.assertNotIn("max_tokens", s.requests[1])

    def test_temperature_is_dropped_if_rejected(self):
        s = self.server(lambda b, h: (400, '{"error":"temperature does not support 0 with this model"}', {}),
                        lambda b, h: chat_reply(json.dumps(GOOD)))
        call(self.local(s))
        self.assertNotIn("temperature", s.requests[1])

    def test_unknown_model_gives_a_helpful_message(self):
        s = self.server(lambda b, h: (404, '{"error":"model not found"}', {}))
        with self.assertRaises(ai.ProviderError) as cm:
            call(self.local(s))
        self.assertIn("ollama list", str(cm.exception))

    def test_connection_refused_message_for_local(self):
        p = ai.OpenAICompatibleProvider("http://127.0.0.1:9/v1", "m", is_local=True)
        with self.assertRaises(ai.ProviderConnectionError) as cm:
            call(p)
        self.assertIn("local AI running", str(cm.exception))

    def test_key_is_sent_and_never_leaks_into_errors(self):
        secret = "sk-SECRET-123"
        s = self.server(lambda b, h: (400, json.dumps({"error": f"bad request for key {secret}"}), {}))
        p = ai.OpenAICompatibleProvider(s.url, "m", api_key=secret, is_local=False)
        with self.assertRaises(ai.ProviderError) as cm:
            call(p)
        self.assertEqual(s.headers_seen[0]["Authorization"], f"Bearer {secret}")
        self.assertNotIn(secret, str(cm.exception))
        self.assertNotIn(secret, repr(p))

    def test_list_models_and_ping(self):
        s = self.server(lambda b, h: chat_reply("OK"))
        p = self.local(s)
        self.assertEqual(p.list_models(), ["llama3.1:8b", "qwen2.5:14b"])
        ok, msg = p.test_connection()
        self.assertTrue(ok, msg)
        self.assertIn("your local AI", msg)

    def test_url_rules(self):
        with self.assertRaises(ai.ProviderConfigError):
            ai.OpenAICompatibleProvider("http://api.example.com/v1", "m")          # cloud over plain http
        with self.assertRaises(ai.ProviderConfigError):
            ai.OpenAICompatibleProvider("", "m")
        with self.assertRaises(ai.ProviderConfigError):
            ai.OpenAICompatibleProvider("ftp://x.com", "m")
        p = ai.OpenAICompatibleProvider("api.openai.com/v1/chat/completions/", "m")
        self.assertEqual(p.base_url, "https://api.openai.com/v1")
        p = ai.OpenAICompatibleProvider("localhost:11434/v1", "m", is_local=True)
        self.assertEqual(p.base_url, "http://localhost:11434/v1")


class AnthropicStub(unittest.TestCase):
    def setUp(self):
        sdk = types.ModuleType("anthropic")

        class APIError(Exception):
            status_code = None

        class APIConnectionError(APIError):
            pass

        class RateLimitError(APIError):
            status_code = 429

        class AuthenticationError(APIError):
            status_code = 401

        sdk.APIError, sdk.APIConnectionError = APIError, APIConnectionError
        sdk.RateLimitError, sdk.AuthenticationError = RateLimitError, AuthenticationError
        self.sdk, self.calls, self.script = sdk, [], []
        test = self

        class Messages:
            def create(self, **kw):
                test.calls.append(kw)
                step = test.script[min(len(test.calls) - 1, len(test.script) - 1)]
                if isinstance(step, Exception):
                    raise step
                return step

        class Client:
            def __init__(self, **kw):
                self.messages = Messages()

        sdk.Anthropic = Client
        self._saved = sys.modules.get("anthropic")
        sys.modules["anthropic"] = sdk

    def tearDown(self):
        if self._saved is None:
            sys.modules.pop("anthropic", None)
        else:
            sys.modules["anthropic"] = self._saved

    def response(self, payload):
        block = types.SimpleNamespace(type="tool_use", name=CLASSIFY_TOOL["name"], input=payload)
        return types.SimpleNamespace(content=[block], usage=types.SimpleNamespace(input_tokens=5, output_tokens=3))

    def test_forced_tool_use_and_caching(self):
        self.script = [self.response(GOOD)]
        p = ai.AnthropicProvider("key", "claude-haiku-4-5-20251001")
        out = call(p)
        self.assertEqual(out[0]["service_name"], "Spotify")
        kw = self.calls[0]
        self.assertEqual(kw["tool_choice"], {"type": "tool", "name": CLASSIFY_TOOL["name"]})
        self.assertEqual(kw["system"][0]["cache_control"], {"type": "ephemeral"})
        self.assertEqual(p.last_usage, (5, 3))

    def test_rate_limit_is_retried_and_auth_is_not(self):
        self.script = [self.sdk.RateLimitError("slow"), self.response(GOOD)]
        call(ai.AnthropicProvider("key"))
        self.assertEqual(len(self.calls), 2)
        self.calls.clear()
        self.script = [self.sdk.AuthenticationError("nope")]
        with self.assertRaises(ai.ProviderAuthError):
            call(ai.AnthropicProvider("key"))
        self.assertEqual(len(self.calls), 1)

    def test_missing_package_is_a_friendly_error(self):
        sys.modules["anthropic"] = None            # makes `import anthropic` raise ImportError
        with self.assertRaises(ai.ProviderConfigError):
            ai.AnthropicProvider("key")


class FactoryAndPrivacy(unittest.TestCase):
    def test_privacy_levels(self):
        self.assertEqual(ai.privacy_summary(ProviderSettings(mode=MODE_LOCAL, kind=KIND_OPENAI,
                                                             base_url="http://localhost:11434/v1"))[0], "local")
        self.assertEqual(ai.privacy_summary(ProviderSettings(mode=MODE_LOCAL, kind=KIND_OPENAI,
                                                             base_url="http://192.168.1.20:11434/v1"))[0], "lan")
        self.assertEqual(ai.privacy_summary(ProviderSettings(mode=MODE_LOCAL, kind=KIND_OPENAI,
                                                             base_url="https://ollama.com/v1"))[0], "remote")
        level, text = ai.privacy_summary(ProviderSettings(mode=MODE_CLOUD, kind=KIND_ANTHROPIC))
        self.assertEqual(level, "cloud")
        self.assertIn("api.anthropic.com", text)

    def test_factory_validation(self):
        with self.assertRaises(ai.ProviderConfigError):
            ai.create_provider(ProviderSettings(mode=MODE_CLOUD, kind=KIND_ANTHROPIC, api_key=""))
        with self.assertRaises(ai.ProviderConfigError):
            ai.create_provider(ProviderSettings(mode=MODE_CLOUD, kind=KIND_OPENAI, api_key="k",
                                                base_url="https://api.openai.com/v1", model=""))
        with self.assertRaises(ai.ProviderConfigError):
            ai.create_provider(ProviderSettings(mode=MODE_LOCAL, kind=KIND_OPENAI, base_url="", model="m"))
        p = ai.create_provider(ProviderSettings(mode=MODE_LOCAL, kind=KIND_OPENAI,
                                                base_url="http://localhost:11434/v1", model="llama3.1:8b"))
        self.assertTrue(p.is_local)
        self.assertGreaterEqual(p.timeout, 300)

    def test_loopback_detection(self):
        for host in ("localhost", "127.0.0.1", "::1", "[::1]", "foo.localhost"):
            self.assertTrue(ai.is_loopback_host(host), host)
        for host in ("example.com", "192.168.1.1", "", "8.8.8.8"):
            self.assertFalse(ai.is_loopback_host(host), host)


if __name__ == "__main__":
    unittest.main()
