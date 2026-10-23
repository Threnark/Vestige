"""Security tests.

* The mail (IMAP) connection must verify the server's certificate and host name before
  any password is sent. `imaplib.IMAP4_SSL` does NOT do this on its own, so these tests
  guard against the check ever being lost, including a real TLS handshake against a
  local server that presents an untrusted (self-signed) certificate.
* Nothing in the program may switch certificate checking off.
* Files that hold email details are private to their owner on macOS and Linux.

No internet access is needed. The real-handshake test needs the `openssl` command line
tool to make a throw-away certificate and is skipped when it isn't installed.
"""
import glob
import imaplib
import os
import re
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

import db
import module_a_imap as imap_mod
import paths
from models import Account
from session import Session

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _account(server="mail.example.com", port=993):
    return Account(name="test", provider="other", email="me@example.com",
                   imap_server=server, imap_port=port, password="app-password")


# ---------------------------------------------------------------------------
# TLS settings
# ---------------------------------------------------------------------------
class TlsContextTests(unittest.TestCase):
    def test_context_checks_certificate_and_host_name(self):
        ctx = imap_mod._tls_context()
        self.assertEqual(ctx.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(ctx.check_hostname)
        self.assertGreaterEqual(ctx.minimum_version, ssl.TLSVersion.TLSv1_2)

    def test_context_still_verifies_when_certifi_is_missing(self):
        with mock.patch.dict(sys.modules, {"certifi": None}):      # makes `import certifi` fail
            ctx = imap_mod._tls_context()
        self.assertEqual(ctx.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(ctx.check_hostname)

    def test_connect_passes_the_verifying_context_to_imaplib(self):
        fake = mock.Mock()
        with mock.patch.object(imap_mod.imaplib, "IMAP4_SSL", return_value=fake) as ctor:
            imap_mod.connect(_account())
        ctx = ctor.call_args.kwargs.get("ssl_context")
        self.assertIsNotNone(ctx, "IMAP4_SSL must always be given an explicit ssl_context")
        self.assertEqual(ctx.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(ctx.check_hostname)
        fake.login.assert_called_once()          # a verified server gets the login as before


class CertificateFailureTests(unittest.TestCase):
    def _cert_error(self):
        exc = ssl.SSLCertVerificationError(1, "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed")
        exc.verify_message = "self-signed certificate"
        return exc

    def test_untrusted_server_is_refused_with_a_clear_message_and_no_login(self):
        with mock.patch.object(imap_mod.imaplib, "IMAP4_SSL", side_effect=self._cert_error()):
            with self.assertRaises(imap_mod.AccountLoginError) as ctx:
                imap_mod.connect(_account())
        message = str(ctx.exception)
        self.assertIn("could not verify", message)
        self.assertIn("self-signed certificate", message)
        self.assertIn("password was NOT sent", message)
        self.assertNotIn("app-password", message)

    def test_certificate_failure_is_not_retried(self):
        with mock.patch.object(imap_mod.imaplib, "IMAP4_SSL", side_effect=self._cert_error()) as ctor, \
                mock.patch.object(imap_mod.time, "sleep") as sleep:
            with self.assertRaises(imap_mod.AccountLoginError):
                imap_mod._retry(imap_mod.connect, _account())
        self.assertEqual(ctor.call_count, 1)
        sleep.assert_not_called()


@unittest.skipUnless(shutil.which("openssl"), "the openssl command line tool is needed to make a test certificate")
class RealHandshakeTests(unittest.TestCase):
    """A real TLS server with a self-signed certificate: it must be refused."""

    @classmethod
    def setUpClass(cls):
        cls.folder = tempfile.mkdtemp(prefix="vestige_tls_")
        cert, key = os.path.join(cls.folder, "cert.pem"), os.path.join(cls.folder, "key.pem")
        try:
            subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", key,
                            "-out", cert, "-days", "2", "-subj", "/CN=localhost"],
                           check=True, capture_output=True, timeout=60)
        except (subprocess.SubprocessError, OSError) as exc:      # e.g. openssl without a config file
            shutil.rmtree(cls.folder, ignore_errors=True)
            raise unittest.SkipTest(f"could not make a test certificate: {exc}")
        server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server_ctx.load_cert_chain(cert, key)
        cls.sock = socket.socket()
        cls.sock.bind(("127.0.0.1", 0))
        cls.sock.listen(5)
        cls.sock.settimeout(0.2)
        cls.port = cls.sock.getsockname()[1]
        cls.stop = threading.Event()

        def serve():
            while not cls.stop.is_set():
                try:
                    raw, _ = cls.sock.accept()
                except OSError:
                    continue
                try:
                    with server_ctx.wrap_socket(raw, server_side=True) as tls:
                        tls.sendall(b"* OK test server ready\r\n")
                        tls.settimeout(1)
                        try:
                            tls.recv(1024)
                        except OSError:
                            pass
                except (ssl.SSLError, OSError):
                    pass                                   # the client walked away: expected

        cls.thread = threading.Thread(target=serve, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.stop.set()
        cls.thread.join(timeout=3)
        cls.sock.close()
        shutil.rmtree(cls.folder, ignore_errors=True)

    def test_control_the_server_really_is_reachable_without_verification(self):
        """If this failed, the test below could pass for the wrong reason."""
        lax = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        lax.check_hostname = False
        lax.verify_mode = ssl.CERT_NONE
        with socket.create_connection(("127.0.0.1", self.port), timeout=5) as raw:
            with lax.wrap_socket(raw, server_hostname="localhost") as tls:
                self.assertIn(b"OK", tls.recv(100))

    def test_vestige_refuses_the_self_signed_server_before_sending_anything(self):
        account = _account(server="127.0.0.1", port=self.port)
        with self.assertRaises(imap_mod.AccountLoginError) as ctx:
            imap_mod.connect(account)
        self.assertIn("could not verify", str(ctx.exception))


# ---------------------------------------------------------------------------
# Regression guard: nobody may switch certificate checking off later
# ---------------------------------------------------------------------------
class NoUnverifiedTlsTests(unittest.TestCase):
    FORBIDDEN = ("CERT_NONE", "_create_unverified_context", "check_hostname = False",
                 "check_hostname=False", "verify=False")

    def _sources(self):
        files = glob.glob(os.path.join(ROOT, "*.py")) + glob.glob(os.path.join(ROOT, "prompts", "*.py"))
        self.assertTrue(files)
        for path in files:
            with open(path, encoding="utf-8") as handle:
                yield os.path.basename(path), handle.read()

    def test_no_code_turns_certificate_checking_off(self):
        for name, text in self._sources():
            for pattern in self.FORBIDDEN:
                self.assertNotIn(pattern, text, f"{name} contains {pattern!r}")

    def test_every_imap_ssl_connection_is_given_a_context(self):
        for name, text in self._sources():
            for match in re.finditer(r"IMAP4_SSL\(", text):
                if text[max(0, match.start() - 12):match.start()].endswith("class "):
                    continue
                call = text[match.start():match.start() + 250]
                self.assertIn("ssl_context=", call, f"{name}: IMAP4_SSL(...) without an ssl_context")


# ---------------------------------------------------------------------------
# Files that hold email details are private to their owner (macOS / Linux)
# ---------------------------------------------------------------------------
@unittest.skipIf(os.name == "nt", "file modes only apply on macOS and Linux")
class PrivateFileTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.mkdtemp(prefix="vestige_perm_")
        self.addCleanup(shutil.rmtree, self.folder, ignore_errors=True)
        self.old_umask = os.umask(0o022)                  # the common default: files come out world-readable
        self.addCleanup(os.umask, self.old_umask)

    @staticmethod
    def mode(path):
        return os.stat(path).st_mode & 0o777

    def test_new_database_file_is_owner_only(self):
        path = os.path.join(self.folder, "accounts.db")
        db.open_database(path).close()
        self.assertEqual(self.mode(path), 0o600)

    def test_existing_database_file_is_tightened(self):
        path = os.path.join(self.folder, "accounts.db")
        db.open_database(path).close()
        os.chmod(path, 0o644)
        db.open_database(path).close()
        self.assertEqual(self.mode(path), 0o600)

    def test_write_mode_makes_a_private_folder_and_file(self):
        data = os.path.join(self.folder, "Vestige_DATA")
        session = Session()
        path = session.enable_write_mode(data)
        session.conn.close()
        self.assertEqual(self.mode(data), 0o700)
        self.assertEqual(self.mode(path), 0o600)

    def test_an_existing_folder_is_left_as_the_user_made_it(self):
        data = os.path.join(self.folder, "Existing")
        os.mkdir(data, 0o755)
        os.chmod(data, 0o755)
        ok, _ = paths.check_writable(data, create=True)
        self.assertTrue(ok)
        self.assertEqual(self.mode(data), 0o755)

    def test_in_memory_database_creates_no_file(self):
        before = set(os.listdir(self.folder))
        db.prepare_private_file(None)
        db.prepare_private_file(":memory:")
        db.open_database(None).close()
        self.assertEqual(set(os.listdir(self.folder)), before)


if __name__ == "__main__":
    unittest.main()
