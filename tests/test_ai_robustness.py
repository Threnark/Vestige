"""Tests for the AI client fixes (batch 3 of the review):

  * a connection that is dropped or cut short (the server closes it, resets it, or sends half a reply) is a
    TEMPORARY problem and is retried, like a timeout. It used to escape as a raw Python error and stop the
    whole stage;
  * a reply cut off at the length limit is asked for again with MORE ROOM (it used to be asked again with
    the same room, which is cut off in the same place), and a cut-off Anthropic reply is no longer mistaken
    for a complete one;
  * a certificate the computer doesn't trust is reported at once instead of being retried for 14 seconds.

No real network or API key is used: the servers here live on this computer.
"""
import contextlib
import http.client
import io
import json
import socket
import ssl
import struct
import sys
import threading
import types
import unittest
import urllib.error
import urllib.request
from unittest import mock

import ai_providers as ai
from prompts.classification_prompt import CLASSIFY_TOOL, ENUM_FALLBACKS, ESSENTIAL_KEYS
from tests.test_providers import GOOD, FakeServer, chat_reply

GOOD_REPLY = json.dumps({"choices": [{"message": {"content": json.dumps(GOOD)}, "finish_reason": "stop"}],
                         "usage": {"prompt_tokens": 11, "completion_tokens": 7}})


class MisbehavingServer:
    """A server that misbehaves on purpose. For connection number n it does script[n-1] (the last item
    repeats):  "close" (hang up without a word),  "reset" (hang up the rude way, with a TCP reset),
    "truncate" (promise a long reply, send a little, hang up),  or "ok" (answer properly)."""

    def __init__(self, *script):
        self.script, self.connections = list(script), 0
        self._sock = socket.socket()
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(5)
        self.url = f"http://127.0.0.1:{self._sock.getsockname()[1]}/v1"
        threading.Thread(target=self._accept, daemon=True).start()

    def close(self):
        self._sock.close()

    def _accept(self):
        while True:
            try:
                client, _ = self._sock.accept()
            except OSError:
                return
            self.connections += 1
            action = self.script[min(self.connections - 1, len(self.script) - 1)]
            threading.Thread(target=self._handle, args=(client, action), daemon=True).start()

    @staticmethod
    def _read_request(client) -> None:
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = client.recv(65536)
            if not chunk:
                return
            data += chunk
        head, _, rest = data.partition(b"\r\n\r\n")
        length = next((int(line.split(b":")[1]) for line in head.split(b"\r\n")
                       if line.lower().startswith(b"content-length")), 0)
        while len(rest) < length:
            rest += client.recv(65536)

    def _handle(self, client, action):
        with client:
            self._read_request(client)
            if action == "close":
                return
            if action == "reset":
                client.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
                return
            if action == "truncate":
                client.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 1000\r\n\r\n"
                               b'{"choices"')
                return
            body = GOOD_REPLY.encode()
            client.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\n"
                           + f"Content-Length: {len(body)}\r\n\r\n".encode() + body)


def structured(provider, **kwargs):
    return provider.structured_call("SYS", "USER", CLASSIFY_TOOL, ESSENTIAL_KEYS, ENUM_FALLBACKS, **kwargs)


class DroppedConnections(unittest.TestCase):
    def setUp(self):
        self.sleeps = []

    def provider(self, server) -> ai.OpenAICompatibleProvider:
        provider = ai.OpenAICompatibleProvider(server.url, "llama3.1:8b", is_local=True, timeout=5)
        provider._sleep = self.sleeps.append
        return provider

    def serve(self, *script) -> MisbehavingServer:
        server = MisbehavingServer(*script)
        self.addCleanup(server.close)
        return server

    def ask(self, server):
        with contextlib.redirect_stdout(io.StringIO()) as log:
            result = structured(self.provider(server))
        return result, log.getvalue()

    def test_a_server_that_hangs_up_without_answering_is_retried(self):
        server = self.serve("close", "ok")
        result, log = self.ask(server)
        self.assertEqual(result[0]["group_id"], "spotify.com")
        self.assertEqual(server.connections, 2)
        self.assertEqual(self.sleeps, [2.0])
        self.assertIn("closed the connection", log)

    def test_a_connection_reset_is_retried(self):
        server = self.serve("reset", "ok")
        self.assertEqual(self.ask(server)[0][0]["group_id"], "spotify.com")
        self.assertEqual(server.connections, 2)

    def test_a_reply_that_stops_part_way_is_retried(self):
        server = self.serve("truncate", "ok")
        result, log = self.ask(server)
        self.assertEqual(result[0]["group_id"], "spotify.com")
        self.assertIn("cut off part-way", log)

    def test_it_keeps_trying_through_several_drops(self):
        server = self.serve("close", "reset", "truncate", "ok")
        self.assertEqual(self.ask(server)[0][0]["group_id"], "spotify.com")
        self.assertEqual(server.connections, 4)
        self.assertEqual(self.sleeps, [2.0, 4.0, 8.0])

    def test_a_server_that_never_stays_connected_ends_in_a_plain_error_not_a_raw_one(self):
        server = self.serve("close")
        with self.assertRaises(ai.ProviderConnectionError) as caught:
            self.ask(server)
        self.assertIn("kept failing", str(caught.exception))
        self.assertEqual(server.connections, ai.AIProvider.MAX_ATTEMPTS)

    def test_the_test_connection_button_also_survives_a_drop(self):
        server = self.serve("close", "ok")
        with contextlib.redirect_stdout(io.StringIO()):
            ok, message = self.provider(server).test_connection()
        self.assertTrue(ok, message)


class Certificates(unittest.TestCase):
    def provider(self):
        provider = ai.OpenAICompatibleProvider("https://api.example.com/v1", "m", api_key="sk-secret-123")
        self.sleeps = []
        provider._sleep = self.sleeps.append
        return provider

    def request(self):
        return urllib.request.Request("https://api.example.com/v1/chat/completions", data=b"{}", method="POST")

    def test_a_certificate_the_computer_does_not_trust_is_not_retried(self):
        failure = urllib.error.URLError(ssl.SSLCertVerificationError(1, "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed"))
        with mock.patch.object(ai.urllib.request, "urlopen", side_effect=failure), \
                self.assertRaises(ai.ProviderConnectionError) as caught:
            self.provider()._open(self.request())
        self.assertIn("Secure connection failed", str(caught.exception))

    def test_it_is_reported_at_once_not_after_fourteen_seconds_of_retrying(self):
        provider = self.provider()
        failure = urllib.error.URLError(ssl.SSLCertVerificationError(1, "certificate verify failed"))
        with mock.patch.object(ai.urllib.request, "urlopen", side_effect=failure), \
                self.assertRaises(ai.ProviderConnectionError) as caught:
            provider._with_retries(lambda: provider._open(self.request()))
        self.assertEqual(self.sleeps, [])
        self.assertNotIn("kept failing", str(caught.exception))

    def test_a_secure_connection_that_was_dropped_is_retried(self):
        with mock.patch.object(ai.urllib.request, "urlopen", side_effect=ssl.SSLEOFError(8, "EOF occurred in violation of protocol")), \
                self.assertRaises(ai._Retryable):
            self.provider()._open(self.request())

    def test_other_secure_connection_errors_are_still_reported_not_retried(self):
        with mock.patch.object(ai.urllib.request, "urlopen", side_effect=ssl.SSLError(1, "wrong version number")), \
                self.assertRaises(ai.ProviderConnectionError):
            self.provider()._open(self.request())

    def test_an_error_reply_that_is_cut_short_is_retried_too(self):
        failure = urllib.error.HTTPError("https://api.example.com/v1", 500, "boom", {}, io.BytesIO(b""))
        failure.read = mock.Mock(side_effect=http.client.IncompleteRead(b"par", 100))
        with mock.patch.object(ai.urllib.request, "urlopen", side_effect=failure), self.assertRaises(ai._Retryable):
            self.provider()._open(self.request())

    def test_the_key_never_appears_in_the_message(self):
        failure = urllib.error.URLError(ssl.SSLCertVerificationError(1, "certificate verify failed"))
        with mock.patch.object(ai.urllib.request, "urlopen", side_effect=failure), \
                self.assertRaises(ai.ProviderConnectionError) as caught:
            self.provider()._open(self.request())
        self.assertNotIn("sk-secret-123", str(caught.exception))


CUT_OFF = chat_reply('{"results": [{"group_id": "spotify', finish="length")
TOO_LARGE = (400, json.dumps({"error": {"message": "max_tokens is too large: 8192. This model supports at most "
                                                   "4096 completion tokens."}}), {})


class CutOffReplies(unittest.TestCase):
    def setUp(self):
        self.servers = []

    def tearDown(self):
        for server in self.servers:
            server.close()

    def server(self, *script) -> FakeServer:
        server = FakeServer([(lambda b, h, step=step: step) for step in script])
        self.servers.append(server)
        return server

    def ask(self, server, **kwargs):
        provider = ai.OpenAICompatibleProvider(server.url, "llama3.1:8b", is_local=True)
        provider._sleep = lambda s: None
        with contextlib.redirect_stdout(io.StringIO()) as log:
            try:
                return structured(provider, **kwargs), log.getvalue()
            except Exception as exc:
                exc.log = log.getvalue()
                raise

    def limits(self, server, name="max_tokens") -> list:
        return [request[name] for request in server.requests]

    def test_a_cut_off_reply_is_asked_for_again_with_more_room(self):
        server = self.server(CUT_OFF, chat_reply(json.dumps(GOOD)))
        result, log = self.ask(server)
        self.assertEqual(result[0]["group_id"], "spotify.com")
        self.assertEqual(self.limits(server), [3000, 8192])
        self.assertIn("cut off at 3,000 tokens", log)
        self.assertIn("8,192", log)

    def test_the_question_is_not_changed_when_only_the_room_was_the_problem(self):
        server = self.server(CUT_OFF, chat_reply(json.dumps(GOOD)))
        self.ask(server)
        first, second = (request["messages"][1]["content"] for request in server.requests)
        self.assertEqual(first, second)
        self.assertNotIn("NOTE", second)

    def test_it_climbs_a_step_at_a_time_and_then_gives_up_with_advice(self):
        server = self.server(CUT_OFF)
        with self.assertRaises(ai.ProviderFormatError) as caught:
            self.ask(server)
        self.assertEqual(self.limits(server), [3000, 8192, 16384])
        message = str(caught.exception)
        self.assertIn("cut off", message)
        self.assertIn("16,384", message)
        self.assertIn("smaller batch", message)

    def test_a_reply_already_at_the_largest_size_is_not_asked_for_again(self):
        server = self.server(CUT_OFF)
        with self.assertRaises(ai.ProviderFormatError):
            self.ask(server, max_tokens=16384)
        self.assertEqual(self.limits(server), [16384])

    def test_a_server_that_refuses_the_bigger_size_ends_with_advice_not_a_crash(self):
        server = self.server(CUT_OFF, TOO_LARGE)
        with self.assertRaises(ai.ProviderFormatError) as caught:
            self.ask(server)
        self.assertEqual(self.limits(server), [3000, 8192])
        self.assertIn("cut off", str(caught.exception))
        self.assertIn("3,000", str(caught.exception))             # the largest size that WAS accepted
        self.assertIn("smaller batch", str(caught.exception))

    def test_a_refusal_of_the_size_we_were_given_is_still_an_ordinary_error(self):
        server = self.server(TOO_LARGE)
        with self.assertRaises(ai.ProviderLimitError):
            self.ask(server)
        self.assertEqual(len(server.requests), 1)

    def test_an_unusable_reply_is_still_asked_again_with_feedback_and_the_same_room(self):
        server = self.server(chat_reply("not json at all"), chat_reply(json.dumps(GOOD)))
        self.ask(server)
        self.assertEqual(self.limits(server), [3000, 3000])
        self.assertIn("NOTE:", server.requests[1]["messages"][1]["content"])

    def test_a_cut_off_reply_and_an_unusable_one_share_the_same_number_of_tries(self):
        server = self.server(CUT_OFF, chat_reply("not json"), chat_reply("still not json"))
        with self.assertRaises(ai.ProviderFormatError):
            self.ask(server)
        self.assertEqual(len(server.requests), 3)

    def test_the_bigger_size_uses_the_parameter_name_this_server_wants(self):
        server = self.server(CUT_OFF, chat_reply(json.dumps(GOOD)))
        provider = ai.OpenAICompatibleProvider(server.url, "gpt-5-mini", is_local=True)
        provider._sleep = lambda s: None
        provider._token_param = "max_completion_tokens"
        with contextlib.redirect_stdout(io.StringIO()):
            structured(provider)
        self.assertEqual(self.limits(server, "max_completion_tokens"), [3000, 8192])


class AnthropicCutOffReplies(unittest.TestCase):
    """The same, against a stand-in for Anthropic's SDK."""

    def setUp(self):
        sdk = types.ModuleType("anthropic")

        class APIError(Exception):
            status_code = None

        class APIConnectionError(APIError):
            pass

        class RateLimitError(APIError):
            status_code = 429

        class BadRequestError(APIError):
            status_code = 400

        sdk.APIError, sdk.APIConnectionError = APIError, APIConnectionError
        sdk.RateLimitError, sdk.BadRequestError = RateLimitError, BadRequestError
        self.sdk, self.calls, self.script = sdk, [], []
        test = self

        class Messages:
            def create(self, **kwargs):
                test.calls.append(kwargs)
                step = test.script[min(len(test.calls) - 1, len(test.script) - 1)]
                if isinstance(step, Exception):
                    raise step
                return step

        class Client:
            def __init__(self, **kwargs):
                self.messages = Messages()

        sdk.Anthropic = Client
        saved = sys.modules.get("anthropic")
        sys.modules["anthropic"] = sdk
        self.addCleanup(lambda: sys.modules.__setitem__("anthropic", saved) if saved else sys.modules.pop("anthropic", None))

    def response(self, payload, stop_reason="end_turn"):
        block = types.SimpleNamespace(type="tool_use", name=CLASSIFY_TOOL["name"], input=payload)
        return types.SimpleNamespace(content=[block], stop_reason=stop_reason,
                                     usage=types.SimpleNamespace(input_tokens=5, output_tokens=3))

    def ask(self):
        provider = ai.AnthropicProvider("key")
        provider._sleep = lambda s: None
        with contextlib.redirect_stdout(io.StringIO()):
            return structured(provider)

    def test_a_cut_off_reply_is_asked_for_again_with_more_room(self):
        self.script = [self.response({"results": []}, stop_reason="max_tokens"), self.response(GOOD)]
        self.assertEqual(self.ask()[0]["group_id"], "spotify.com")
        self.assertEqual([call["max_tokens"] for call in self.calls], [3000, 8192])

    def test_what_was_finished_before_the_cut_is_not_used_as_if_it_were_everything(self):
        """The old code took the partial tool result as the answer and silently lost every group after the cut."""
        partial = {"results": [GOOD["results"][0]]}
        self.script = [self.response(partial, stop_reason="max_tokens"), self.response(GOOD)]
        self.ask()
        self.assertEqual(len(self.calls), 2, "the partial answer must not have been accepted")

    def test_a_complete_reply_is_unaffected(self):
        self.script = [self.response(GOOD)]
        self.assertEqual(self.ask()[0]["group_id"], "spotify.com")
        self.assertEqual(len(self.calls), 1)

    def test_a_model_that_refuses_the_bigger_size_ends_with_advice(self):
        refusal = self.sdk.BadRequestError("max_tokens: 8192 > 4096, which is the maximum allowed number of "
                                           "output tokens for this model")
        self.script = [self.response({"results": []}, stop_reason="max_tokens"), refusal]
        with self.assertRaises(ai.ProviderFormatError) as caught:
            self.ask()
        self.assertIn("smaller batch", str(caught.exception))
        self.assertEqual([call["max_tokens"] for call in self.calls], [3000, 8192])

    def test_a_refusal_of_the_size_we_were_given_is_an_ordinary_error(self):
        self.script = [self.sdk.BadRequestError("max_tokens: 3000 > 2048, which is the maximum allowed")]
        with self.assertRaises(ai.ProviderLimitError):
            self.ask()
        self.assertEqual(len(self.calls), 1)

    def test_other_bad_requests_are_still_ordinary_provider_errors(self):
        self.script = [self.sdk.BadRequestError("messages: roles must alternate")]
        with self.assertRaises(ai.ProviderError) as caught:
            self.ask()
        self.assertNotIsInstance(caught.exception, ai.ProviderLimitError)


if __name__ == "__main__":
    unittest.main()
