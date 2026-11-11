"""
ai_providers.py
---------------
The ONLY place in the program that talks to an AI service. Everything else
(Module C sign-up classification, Module E email-change detection) asks for
"structured results" through the small interface below and doesn't care which
service answers.

Two adapters cover essentially everything:

  AnthropicProvider
      Anthropic's own API. Uses *forced tool use*, which makes Claude return
      JSON that matches the schema, so there's nothing to parse by hand.

  OpenAICompatibleProvider
      The de-facto standard "chat completions" API. Spoken by OpenAI, Google
      Gemini, Groq, OpenRouter, Together, Mistral, DeepSeek, xAI ... and by
      every local server (Ollama, LM Studio, llama.cpp). One adapter, a base
      URL, an optional key and a model name. Uses only the standard library
      (urllib), so it adds no dependency.

Because services differ in how well they enforce JSON, every reply goes through
the same safety net:
  1. a ladder of increasingly lenient request formats (see _request),
  2. tolerant JSON extraction (handles ``` fences, <think> blocks, stray text),
  3. validation against the very same JSON schema we asked for,
  4. a couple of automatic retries that tell the model what was wrong.

PRIVACY NOTES
  * An API key is only ever held in memory and only ever sent to the host the
    user typed. It is removed from any error text before it can be displayed.
  * Cloud providers must use https. Plain http is only allowed for
    local servers (and only that makes sense on localhost).
  * `privacy_summary()` tells the UI, in plain words, where the data goes.
"""

from __future__ import annotations

import http.client
import ipaddress
import json
import re
import socket
import ssl
import time
import urllib.error
import urllib.request
from urllib.parse import urlparse

from models import (
    DEFAULT_ANTHROPIC_MODEL, KIND_ANTHROPIC, KIND_OPENAI, MODE_LOCAL, ProviderSettings,
)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class ProviderError(Exception):
    """Base class. The message is written for end users, not programmers."""


class ProviderConfigError(ProviderError):
    """Something is missing or wrong in the settings (no key, bad URL, ...)."""


class ProviderAuthError(ProviderError):
    """The service rejected the API key / permission."""


class ProviderConnectionError(ProviderError):
    """Couldn't reach the service (offline, server not running, timeout)."""


class ProviderFormatError(ProviderError):
    """The AI answered, but not in a usable shape (invalid JSON, wrong fields)."""


class ReplyCutOff(ProviderFormatError):
    """The AI stopped because it reached the reply-length limit (max_tokens), not because it had finished,
    so what it sent is incomplete. Asking again with more room usually fixes this, and structured_call does
    exactly that."""


class ProviderLimitError(ProviderError):
    """The server refused the reply length that was asked for because it is more than this model allows."""


class _Retryable(Exception):
    """Internal: a temporary failure worth retrying (rate limit, 5xx, timeout, dropped connection)."""

    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


# ---------------------------------------------------------------------------
# Where does the data go? (used for the privacy label in the UI)
# ---------------------------------------------------------------------------

def is_loopback_host(host: str) -> bool:
    """True for localhost / 127.x / ::1 - i.e. this very computer."""
    h = (host or "").strip("[]").lower()
    if not h:
        return False
    if h == "localhost" or h.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(h).is_loopback
    except ValueError:
        return False


def is_private_host(host: str) -> bool:
    """True for addresses on a home/office network (192.168.x.x, 10.x.x.x ...)."""
    try:
        return ipaddress.ip_address((host or "").strip("[]")).is_private
    except ValueError:
        return False


def privacy_summary(settings: ProviderSettings) -> tuple:
    """Return (level, text) describing honestly where email text will go.

    level: "local"  - stays on this computer
           "lan"    - goes to another machine on your own network
           "remote" - the 'local' address is actually a server elsewhere!
           "cloud"  - sent over the internet to the chosen company
    """
    if settings.mode == MODE_LOCAL:
        host = urlparse(settings.base_url if "//" in settings.base_url else "//" + settings.base_url).hostname or ""
        if not host:
            return "local", "Enter the address of your local AI server."
        if is_loopback_host(host):
            return "local", "Nothing leaves this computer: the AI runs here on your machine."
        if is_private_host(host):
            return "lan", (f"This address ({host}) is another device on your network. Email text will "
                           f"travel over your local network to it, but not over the internet.")
        return "remote", (f"WARNING: {host} is not on this computer or your local network. "
                          f"Email text will be sent to that server over the internet.")
    if settings.kind == KIND_ANTHROPIC:
        host = "api.anthropic.com"
    else:
        host = urlparse(settings.base_url if "//" in settings.base_url else "//" + settings.base_url).hostname or "your chosen provider"
    return "cloud", (f"Sender, subject and a short text snippet of the emails each chosen pipeline shortlists "
                     f"(for receipts, also the money amounts in them) are sent over the internet to {host}. "
                     f"Your API key is sent there too. Nothing else is sent.")


def destination_of(settings: ProviderSettings) -> str:
    """Where email text would be sent, as a short label that can be compared: 'api.anthropic.com', or the
    server's 'host' / 'host:port'. The interface remembers WHICH destination a person agreed to, so pointing
    Vestige at a different server asks again instead of riding on an answer given for another one."""
    if settings.mode != MODE_LOCAL and settings.kind == KIND_ANTHROPIC:
        return "api.anthropic.com"
    raw = settings.base_url or ""
    parsed = urlparse(raw if "//" in raw else "//" + raw)
    host = (parsed.hostname or "").lower()
    try:
        port = parsed.port
    except ValueError:
        port = None
    return f"{host}:{port}" if host and port else host


# ---------------------------------------------------------------------------
# Tolerant JSON handling
# ---------------------------------------------------------------------------

_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)


def _first_balanced(text: str):
    """Yield every top-level {...} or [...] block found in `text` (string- and
    escape-aware), so we can pull JSON out of a chatty reply."""
    i, n = 0, len(text)
    while i < n:
        if text[i] in "{[":
            depth, in_str, esc, j = 0, False, False, i
            while j < n:
                ch = text[j]
                if in_str:
                    if esc:
                        esc = False
                    elif ch == "\\":
                        esc = True
                    elif ch == '"':
                        in_str = False
                elif ch == '"':
                    in_str = True
                elif ch in "{[":
                    depth += 1
                elif ch in "}]":
                    depth -= 1
                    if depth == 0:
                        yield text[i:j + 1]
                        i = j
                        break
                j += 1
        i += 1


def extract_json(text: str):
    """Turn an AI reply into a Python object, or raise ProviderFormatError."""
    t = _THINK_RE.sub("", text or "").strip()          # reasoning models: <think>..</think>
    if "<think>" in t.lower():                         # an unfinished think block
        t = t[: t.lower().index("<think>")].strip()
    if not t:
        raise ProviderFormatError("the reply was empty")
    try:
        return json.loads(t)
    except ValueError:
        pass
    for fenced in _FENCE_RE.findall(t):
        try:
            return json.loads(fenced.strip())
        except ValueError:
            continue
    for block in _first_balanced(t):
        try:
            return json.loads(block)
        except ValueError:
            continue
    raise ProviderFormatError("the reply wasn't valid JSON")


# ---------------------------------------------------------------------------
# Validating a reply against the schema we asked for
# ---------------------------------------------------------------------------

def _norm_enum(value: str) -> str:
    return re.sub(r"[\s\-]+", "_", value.strip().lower())


def _coerce(value, schema: dict, key: str, fallbacks: dict):
    """Coerce one value to the schema's type, leniently (small local models
    often send "true" for true, or a number as a string)."""
    t = schema.get("type")
    if t == "boolean":
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        if isinstance(value, str):
            return value.strip().lower() in ("true", "yes", "y", "1")
        return False
    if t == "integer":
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return 0
    if t == "number":
        try:
            num = float(value)
        except (TypeError, ValueError):
            num = 0.0
        return min(1.0, max(0.0, num)) if key == "confidence" else num
    # string (possibly with an enum)
    text = "" if value is None else str(value)
    allowed = schema.get("enum")
    if allowed:
        if text in allowed:
            return text
        wanted = _norm_enum(text)
        for option in allowed:
            if _norm_enum(option) == wanted:
                return option
        if key in fallbacks:
            return fallbacks[key]
        raise ProviderFormatError(f"'{key}' must be one of {allowed}, got '{text}'")
    return text


def _default_for(schema: dict, key: str, fallbacks: dict):
    t = schema.get("type")
    if t == "boolean":
        return False
    if t in ("integer", "number"):
        return 0
    if schema.get("enum"):
        return fallbacks.get(key, schema["enum"][-1])
    return ""


def validate_object_list(data, schema: dict, essential=(), fallbacks=None, list_key: str = "results") -> list:
    """Check/clean a reply shaped like {"results": [ {...}, {...} ]}.

    * `schema` is the same JSON schema we sent to the AI.
    * Items missing an `essential` key are dropped (the caller treats that
      group as unanswered and it is simply tried again next run).
    * Other missing keys get a harmless default instead of failing the batch.
    * Unknown enum values map to `fallbacks[key]` when given.
    Raises ProviderFormatError if nothing usable came back, which triggers the
    automatic retry in AIProvider.structured_call.
    """
    fallbacks = fallbacks or {}
    if isinstance(data, list):
        data = {list_key: data}
    if not isinstance(data, dict):
        raise ProviderFormatError("expected a JSON object")
    items = data.get(list_key)
    if items is None:
        lists = [v for v in data.values() if isinstance(v, list)]
        items = lists[0] if len(lists) == 1 else None
    if not isinstance(items, list):
        raise ProviderFormatError(f"expected a '{list_key}' list")

    item_schema = schema["properties"][list_key]["items"]
    props = item_schema.get("properties", {})
    cleaned = []
    for item in items:
        if not isinstance(item, dict) or any(k not in item for k in essential):
            continue
        out = {}
        for key, sub in props.items():
            if key in item:
                out[key] = _coerce(item[key], sub, key, fallbacks)
            else:
                out[key] = _default_for(sub, key, fallbacks)
        cleaned.append(out)
    if not cleaned:
        raise ProviderFormatError("no usable result items were returned")
    return cleaned


# ---------------------------------------------------------------------------
# Base class
# ---------------------------------------------------------------------------

class AIProvider:
    """Interface every adapter implements."""

    is_local = False
    MAX_ATTEMPTS = 4        # tries for temporary network/server problems
    FORMAT_RETRIES = 2      # extra tries when the reply isn't valid JSON, or was cut off
    # Reply sizes (in tokens) tried, in turn, when a reply is cut off at the limit it was given. Steps, not
    # doubling: 8,192 is accepted by nearly every model, so a model with a smaller maximum still gets the
    # first retry, and only the last step can be refused.
    TOKEN_STEPS = (8192, 16384)

    def __init__(self):
        self.last_usage = None          # (input_tokens, output_tokens) of the latest call, if known
        self._sleep = time.sleep        # replaced in tests so they don't actually wait

    # -- subclasses implement these ------------------------------------
    def _request(self, system_prompt: str, user_message: str, tool: dict, max_tokens: int):
        """Send one request, return the decoded JSON object (or raise)."""
        raise NotImplementedError

    def _ping(self) -> str:
        """Make the cheapest possible real request; return a short success note."""
        raise NotImplementedError

    def list_models(self) -> list:
        return []

    # -- shared behaviour ------------------------------------------------
    def _log(self, message: str) -> None:
        print(message)                  # the GUI captures print() into its log box

    def _with_retries(self, fn):
        """Run `fn`, retrying temporary failures with growing delays."""
        delay = 2.0
        for attempt in range(1, self.MAX_ATTEMPTS + 1):
            try:
                return fn()
            except _Retryable as exc:
                if attempt == self.MAX_ATTEMPTS:
                    raise ProviderConnectionError(
                        f"The AI service kept failing ({exc}). Check your connection and try again."
                    ) from None
                wait = min(60.0, exc.retry_after if exc.retry_after else delay)
                self._log(f"  temporary problem ({exc}); waiting {wait:.0f}s then retrying "
                          f"({attempt}/{self.MAX_ATTEMPTS - 1})")
                self._sleep(wait)
                delay *= 2

    def _bigger_limit(self, limit: int):
        """The next reply size to try after one of `limit` tokens was cut off, or None if there is none."""
        return next((step for step in self.TOKEN_STEPS if step > limit), None)

    def structured_call(self, system_prompt: str, user_message: str, tool: dict,
                        essential=(), fallbacks=None, max_tokens: int = 3000) -> list:
        """Ask for structured results and return them as a validated list of
        dicts. `tool` is {"name", "description", "input_schema"} where the
        schema is {"type":"object","properties":{"results":{"type":"array",...}}}.

        Two different things can go wrong with a reply, and they are handled differently:
          * it is NOT USABLE (not valid JSON, wrong fields): ask again and say what was wrong;
          * it was CUT OFF at the length limit (the model ran out of room, which is common with models that
            "think" before answering): the content was fine as far as it went, so ask the same thing
            again with more room. Asking again with the same room would only be cut off in the same place.
        """
        note = ""
        last_error = None
        limit = max_tokens
        previous_limit = max_tokens
        for attempt in range(self.FORMAT_RETRIES + 1):
            message = user_message + ("\n\n" + note if note else "")
            try:
                data = self._with_retries(lambda: self._request(system_prompt, message, tool, limit))
                return validate_object_list(data, tool["input_schema"], essential, fallbacks)
            except ReplyCutOff as exc:
                last_error = exc
                bigger = self._bigger_limit(limit)
                if bigger is None or attempt == self.FORMAT_RETRIES:
                    break
                self._log(f"  the AI's reply was cut off at {limit:,} tokens; asking again with "
                          f"room for {bigger:,} ({attempt + 1}/{self.FORMAT_RETRIES})")
                previous_limit, limit, note = limit, bigger, ""        # nothing was wrong with the content
            except ProviderLimitError as exc:
                if limit == max_tokens:
                    raise                      # the size we were told to use was refused: a real setup problem
                # We asked for more room because a reply was cut off, and this model won't allow it.
                last_error = exc
                limit = previous_limit
                break
            except ProviderFormatError as exc:
                last_error = exc
                if attempt < self.FORMAT_RETRIES:
                    self._log(f"  the AI's reply wasn't usable ({exc}); asking again "
                              f"({attempt + 1}/{self.FORMAT_RETRIES})")
                note = (f"NOTE: your previous reply could not be used because: {exc}. "
                        f"Reply again with ONLY the JSON object that matches the schema, "
                        f"with one result for every item given, and nothing else.")
        if isinstance(last_error, (ReplyCutOff, ProviderLimitError)):
            raise ProviderFormatError(
                f"The AI's reply was cut off before it finished, even with room for {limit:,} tokens. "
                f"Use a smaller batch size so each reply is shorter (or a model that can write longer replies)."
            )
        raise ProviderFormatError(
            f"The AI didn't return usable output after {self.FORMAT_RETRIES + 1} tries ({last_error}). "
            f"Try a smaller batch size, or a more capable model."
        )

    def test_connection(self) -> tuple:
        """(ok, message) for the 'Test connection' button. Never raises."""
        try:
            return True, self._ping()
        except ProviderError as exc:
            return False, str(exc)
        except Exception as exc:                          # defensive: never crash the UI
            return False, f"Unexpected problem: {exc}"


# ---------------------------------------------------------------------------
# Anthropic adapter
# ---------------------------------------------------------------------------

class AnthropicProvider(AIProvider):
    """Claude via Anthropic's SDK, using forced tool use for guaranteed JSON."""

    def __init__(self, api_key: str, model: str = DEFAULT_ANTHROPIC_MODEL, timeout: int = 120):
        super().__init__()
        try:
            import anthropic                       # imported lazily: only needed for this provider
        except ImportError:
            raise ProviderConfigError(
                "The 'anthropic' package isn't installed. Run: pip install anthropic "
                "(or choose a different provider)."
            ) from None
        self._sdk = anthropic
        self.model = model or DEFAULT_ANTHROPIC_MODEL
        self._api_key = api_key
        # max_retries=0: WE do the retrying (so the log shows what's happening).
        self._client = anthropic.Anthropic(api_key=api_key, timeout=timeout, max_retries=0)

    def __repr__(self) -> str:
        return f"AnthropicProvider(model={self.model!r})"

    def _translate(self, exc: Exception):
        """Map SDK exceptions onto our own error types."""
        sdk = self._sdk
        status = getattr(exc, "status_code", None)
        if isinstance(exc, sdk.RateLimitError):
            raise _Retryable("rate limited") from None
        if isinstance(exc, sdk.APIConnectionError):      # includes timeouts
            raise _Retryable("connection problem") from None
        if status in (401, 403):
            raise ProviderAuthError("Anthropic rejected the API key. Check that it is correct and active.") from None
        if status == 404:
            raise ProviderError(f"Anthropic says the model '{self.model}' wasn't found. "
                                f"Check the model name.") from None
        if status == 400 and "max_tokens" in str(exc).lower():
            raise ProviderLimitError(f"Anthropic won't allow that reply length for '{self.model}': "
                                     f"{str(exc)[:200]}") from None
        if status is not None and status >= 500:
            raise _Retryable(f"server error {status}") from None
        raise ProviderError(f"Anthropic returned an error: {str(exc)[:300]}") from None

    def _create(self, **kwargs):
        try:
            return self._client.messages.create(**kwargs)
        except (self._sdk.APIError, self._sdk.APIConnectionError) as exc:
            self._translate(exc)

    def _request(self, system_prompt, user_message, tool, max_tokens):
        response = self._create(
            model=self.model,
            max_tokens=max_tokens,
            # cache_control: the long, identical system prompt is billed at a
            # fraction of the normal input price after the first request.
            system=[{"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user_message}],
            tools=[{"name": tool["name"], "description": tool["description"],
                    "input_schema": tool["input_schema"]}],
            tool_choice={"type": "tool", "name": tool["name"]},     # force the structured answer
        )
        usage = getattr(response, "usage", None)
        if usage is not None:
            self.last_usage = (getattr(usage, "input_tokens", None), getattr(usage, "output_tokens", None))
        if getattr(response, "stop_reason", None) == "max_tokens":
            # Out of room part-way through the answer. The tool_use block that comes back is whatever was
            # finished by then: using it would quietly lose every result after the cut.
            raise ReplyCutOff("the reply was cut off before it finished (too long)")
        for block in response.content:
            if getattr(block, "type", "") == "tool_use" and block.name == tool["name"]:
                return block.input
        raise ProviderFormatError("the model didn't return the structured result")

    def _ping(self) -> str:
        self._with_retries(lambda: self._create(
            model=self.model, max_tokens=8, messages=[{"role": "user", "content": "Reply with: OK"}]))
        return f"Connected. Model '{self.model}' answered."


# ---------------------------------------------------------------------------
# Generic OpenAI-compatible adapter (cloud services AND local servers)
# ---------------------------------------------------------------------------

_JSON_INSTRUCTIONS = (
    "RESPONSE FORMAT: reply with ONE JSON object and nothing else - no markdown fences, "
    "no commentary. It must follow this JSON Schema exactly (use these exact property "
    "names):\n{schema}"
)


class OpenAICompatibleProvider(AIProvider):
    def __init__(self, base_url: str, model: str, api_key: str = "", timeout: int = 120,
                 is_local: bool = False):
        super().__init__()
        self.is_local = is_local
        self.model = (model or "").strip()
        self._api_key = (api_key or "").strip()
        self.timeout = timeout
        self.base_url = self._normalize_url(base_url, is_local)

        # What this server turned out NOT to support, learned from its errors.
        self._unsupported_modes: set = set()
        self._token_param = "max_tokens"       # newer OpenAI models want max_completion_tokens
        self._send_temperature = True          # some models reject an explicit temperature

        try:
            import certifi                       # a good CA bundle also inside packaged apps
            self._ssl = ssl.create_default_context(cafile=certifi.where())
        except Exception:
            self._ssl = ssl.create_default_context()

    def __repr__(self) -> str:                   # never include the key
        return f"OpenAICompatibleProvider(base_url={self.base_url!r}, model={self.model!r}, local={self.is_local})"

    # -- url / transport ---------------------------------------------------
    @staticmethod
    def _normalize_url(url: str, is_local: bool) -> str:
        raw = (url or "").strip()
        if not raw:
            raise ProviderConfigError("Enter the provider's base URL (for example https://api.openai.com/v1).")
        if "://" not in raw:
            raw = ("http://" if is_local else "https://") + raw
        parsed = urlparse(raw)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ProviderConfigError(f"'{url}' isn't a valid web address.")
        if parsed.scheme == "http" and not is_local and not is_loopback_host(parsed.hostname):
            # Never send an API key (or email text) unencrypted across the internet.
            raise ProviderConfigError(
                "Cloud providers must use https:// so your key and data are encrypted in transit. "
                "(Plain http:// is only allowed for a local AI server.)")
        cleaned = raw.rstrip("/")
        for suffix in ("/chat/completions", "/completions"):   # users sometimes paste the full endpoint
            if cleaned.endswith(suffix):
                cleaned = cleaned[: -len(suffix)]
        return cleaned

    def _scrub(self, text: str) -> str:
        """Remove the API key from any text before it can reach a log or dialog."""
        if self._api_key and self._api_key in text:
            text = text.replace(self._api_key, "***")
        return text

    def _headers(self) -> dict:
        headers = {"Content-Type": "application/json", "Accept": "application/json",
                   "User-Agent": "Vestige/3"}
        return headers

    # What a connection that was cut mid-conversation looks like to Python. urllib only wraps errors from
    # the connecting/sending stage (as URLError); a server that closes the connection while we wait for the
    # answer, or part-way through it, raises these directly. They are temporary, like a timeout.
    _DROPPED = (http.client.HTTPException, ConnectionError)

    @staticmethod
    def _describe_drop(exc: Exception) -> str:
        if isinstance(exc, http.client.RemoteDisconnected):
            return "the server closed the connection without answering"
        if isinstance(exc, http.client.IncompleteRead):
            return "the server's reply was cut off part-way"
        if isinstance(exc, ConnectionResetError):
            return "the connection was reset"
        return f"the connection was dropped ({type(exc).__name__})"

    def _open(self, req: urllib.request.Request):
        """Perform one HTTP call. Returns (status, text, headers) for any HTTP
        response (including errors); raises for network-level problems."""
        if self._api_key:
            # add_unredirected_header: the key is NOT forwarded if the server
            # redirects us to a different host.
            req.add_unredirected_header("Authorization", f"Bearer {self._api_key}")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout, context=self._ssl) as resp:
                return resp.status, resp.read().decode("utf-8", errors="replace"), resp.headers
        except urllib.error.HTTPError as exc:
            try:
                text = exc.read().decode("utf-8", errors="replace")
            except self._DROPPED as drop:
                raise _Retryable(self._describe_drop(drop)) from None
            except (socket.timeout, TimeoutError):
                raise _Retryable("the request timed out") from None
            return exc.code, text, exc.headers
        except urllib.error.URLError as exc:
            reason = exc.reason
            if isinstance(reason, ssl.SSLCertVerificationError):
                # Waiting can't fix a certificate the computer doesn't trust (a company proxy, a wrong
                # date on this computer), so this is reported at once instead of retried.
                raise ProviderConnectionError(f"Secure connection failed: {reason}") from None
            if isinstance(reason, ConnectionRefusedError) or "refused" in str(reason).lower():
                hint = (" Is your local AI running? (For Ollama, open the Ollama app or run 'ollama serve'.)"
                        if self.is_local else "")
                raise ProviderConnectionError(f"Couldn't connect to {self.base_url}.{hint}") from None
            if isinstance(reason, (socket.timeout, TimeoutError)) or "timed out" in str(reason).lower():
                raise _Retryable("the request timed out") from None
            raise _Retryable(f"network problem: {reason}") from None
        except (socket.timeout, TimeoutError):
            raise _Retryable("the request timed out") from None
        except ssl.SSLError as exc:
            if isinstance(exc, (ssl.SSLEOFError, ssl.SSLZeroReturnError)):
                raise _Retryable("the secure connection was dropped") from None      # a drop, not a bad certificate
            raise ProviderConnectionError(f"Secure connection failed: {exc}") from None
        except self._DROPPED as exc:
            raise _Retryable(self._describe_drop(exc)) from None

    def _post(self, path: str, body: dict):
        req = urllib.request.Request(
            self.base_url + path, data=json.dumps(body).encode("utf-8"),
            headers=self._headers(), method="POST")
        return self._open(req)

    def _get(self, path: str):
        req = urllib.request.Request(self.base_url + path, headers=self._headers(), method="GET")
        return self._open(req)

    def _check_status(self, status: int, text: str, headers) -> None:
        """Raise for statuses that are never recoverable by changing the request."""
        if status in (401, 403):
            raise ProviderAuthError(
                "The server rejected the API key (or you don't have access to this model). "
                "Check the key, and that it belongs to this provider.")
        if status == 429 or status >= 500:
            retry_after = None
            try:
                retry_after = float(headers.get("Retry-After")) if headers and headers.get("Retry-After") else None
            except ValueError:
                pass
            raise _Retryable(f"HTTP {status}", retry_after)

    # -- chat requests -------------------------------------------------------
    def _chat(self, body: dict):
        """POST /chat/completions with automatic parameter fix-ups.
        Returns (status, payload_dict_or_error_text)."""
        for _ in range(3):
            status, text, headers = self._post("/chat/completions", body)
            if status == 200:
                try:
                    return 200, json.loads(text)
                except ValueError:
                    raise ProviderFormatError("the server's reply wasn't valid JSON") from None
            self._check_status(status, text, headers)
            lowered = text.lower()
            # Newer OpenAI models renamed max_tokens -> max_completion_tokens.
            if "max_completion_tokens" in lowered and "max_tokens" in body:
                body["max_completion_tokens"] = body.pop("max_tokens")
                self._token_param = "max_completion_tokens"
                continue
            # Some models only accept their default temperature.
            if "temperature" in lowered and "temperature" in body:
                del body["temperature"]
                self._send_temperature = False
                continue
            return status, self._scrub(text)
        return status, self._scrub(text)

    def _build_body(self, messages: list, mode: str, tool: dict, max_tokens: int) -> dict:
        body = {"model": self.model, "messages": messages, self._token_param: max_tokens}
        if self._send_temperature:
            body["temperature"] = 0           # classification should be as repeatable as possible
        if mode == "json_schema":
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": tool["name"], "schema": tool["input_schema"]},
            }
        elif mode == "json_object":
            body["response_format"] = {"type": "json_object"}
        return body

    @staticmethod
    def _mentions_token_limit(error_text: str) -> bool:
        lowered = error_text.lower()
        return any(w in lowered for w in ("max_tokens", "max_completion_tokens", "context length",
                                          "context window", "context size", "too many tokens"))

    @staticmethod
    def _mentions_format(error_text: str) -> bool:
        lowered = error_text.lower()
        return any(w in lowered for w in
                   ("response_format", "json_schema", "json_object", "structured", "schema", "grammar"))

    def _friendly_http_error(self, status: int, text: str) -> str:
        snippet = text.strip().replace("\n", " ")[:300]
        if status == 404:
            return (f"The server says '{self.model}' (or the URL) wasn't found. Check the model name"
                    f"{' (for Ollama run: ollama list)' if self.is_local else ''} and the base URL. "
                    f"Server said: {snippet}")
        return f"The AI server returned an error (HTTP {status}): {snippet}"

    def _parse_reply(self, payload: dict):
        try:
            choice = payload["choices"][0]
            message = choice.get("message") or {}
        except (KeyError, IndexError, TypeError, AttributeError):
            raise ProviderFormatError("the server's reply had no answer in it") from None
        usage = payload.get("usage") or {}
        if usage:
            self.last_usage = (usage.get("prompt_tokens"), usage.get("completion_tokens"))
        content = message.get("content")
        if isinstance(content, list):                       # some servers return content parts
            content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
        if choice.get("finish_reason") == "length":
            raise ReplyCutOff("the reply was cut off before it finished (too long)")
        return extract_json(content or "")

    def _request(self, system_prompt, user_message, tool, max_tokens):
        system = system_prompt + "\n\n" + _JSON_INSTRUCTIONS.format(
            schema=json.dumps(tool["input_schema"], separators=(",", ":")))
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user_message}]

        # The ladder: strictest format first, then more lenient ones. We
        # remember what the server rejected so later calls skip straight to a
        # format that works.
        for mode in ("json_schema", "json_object", "prompt_only"):
            if mode in self._unsupported_modes:
                continue
            status, payload = self._chat(self._build_body(messages, mode, tool, max_tokens))
            if status == 200:
                return self._parse_reply(payload)
            if mode != "prompt_only" and self._mentions_format(str(payload)):
                self._unsupported_modes.add(mode)
                self._log(f"  this server doesn't support '{mode}' output; using a simpler format")
                continue
            if status == 400 and self._mentions_token_limit(str(payload)):
                raise ProviderLimitError(self._friendly_http_error(status, str(payload)))
            raise ProviderError(self._friendly_http_error(status, str(payload)))
        raise ProviderError("The server rejected every request format we tried.")

    def _ping(self) -> str:
        if not self.model:
            raise ProviderConfigError("Enter a model name first.")
        body = {"model": self.model, self._token_param: 16,
                "messages": [{"role": "user", "content": "Reply with: OK"}]}
        status, payload = self._with_retries(lambda: self._chat(body))
        if status != 200:
            raise ProviderError(self._friendly_http_error(status, str(payload)))
        where = "your local AI" if self.is_local else urlparse(self.base_url).hostname
        return f"Connected to {where}. Model '{self.model}' answered."

    def list_models(self) -> list:
        """Ask the server which models it offers (powers the 'Find models' button)."""
        status, text, headers = self._with_retries(lambda: self._get("/models"))
        if status != 200:
            raise ProviderError(self._friendly_http_error(status, self._scrub(text)))
        try:
            data = json.loads(text)
        except ValueError:
            raise ProviderError("The server returned a model list we couldn't read.") from None
        entries = data.get("data") if isinstance(data, dict) else data
        names = []
        for entry in entries or []:
            name = entry.get("id") if isinstance(entry, dict) else entry
            if isinstance(name, str):
                names.append(name)
        return sorted(set(names))


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def check_settings(settings: ProviderSettings, require_model: bool = True) -> None:
    """Raise ProviderConfigError, in plain words, if something the settings need is missing. Nothing is built
    and no connection is made, so it is cheap enough to call as the person types."""
    if settings.mode == MODE_LOCAL:
        if not settings.base_url.strip():
            raise ProviderConfigError("Enter the address of your local AI server "
                                      "(Ollama's is http://localhost:11434/v1).")
        if require_model and not settings.model.strip():
            raise ProviderConfigError("Enter the model name your local server should use "
                                      "(use 'Find models' to see what's installed).")
        return
    if not settings.api_key.strip():
        raise ProviderConfigError("Enter your API key first.")
    if settings.kind == KIND_OPENAI:
        if require_model and not settings.model.strip():
            raise ProviderConfigError("Enter the model name from your provider's documentation "
                                      "(or use 'Find models').")
    elif settings.kind != KIND_ANTHROPIC:
        raise ProviderConfigError(f"Unknown provider type: {settings.kind}")


def create_provider(settings: ProviderSettings, require_model: bool = True) -> AIProvider:
    """Build the right adapter from the user's settings, with friendly errors.
    `require_model=False` is used only by "Find models", which runs before a model is chosen."""
    check_settings(settings, require_model)
    if settings.mode == MODE_LOCAL:
        # Local models are slow; give them much longer before giving up.
        return OpenAICompatibleProvider(settings.base_url, settings.model, settings.api_key,
                                        timeout=max(settings.timeout_seconds, 300), is_local=True)
    if settings.kind == KIND_ANTHROPIC:
        return AnthropicProvider(settings.api_key.strip(), settings.model.strip() or DEFAULT_ANTHROPIC_MODEL,
                                 timeout=settings.timeout_seconds)
    return OpenAICompatibleProvider(settings.base_url, settings.model, settings.api_key,
                                    timeout=settings.timeout_seconds, is_local=False)
