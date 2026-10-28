"""Tests of odoo_xmlrpc_auth.client: no network, no real store.

The store is built under a temporary HOME, with dummy values only, and the
endpoint that authenticates is replaced, so no test can reach a server. A
store that cannot be read must not show its text: the error that says so
carries none of it, in its message, its repr or a chained exception.

A call after the authentication is stopped at xmlrpc.client's Transport,
which raises what the test gives it. A refused connection is the
ConnectionRefusedError(111, 'Connection refused') that xmlrpc.client raised
for 127.0.0.1 port 1 on 2026-10-28; a ProtocolError is what xmlrpc.client
raises for an HTTP status other than 200, its url being the host and the
path it called.

Run from the repository root:
    PYTHONPATH=src python3 -m unittest discover -s tests
"""

import contextlib
import io
import os
import sys
import tempfile
import unittest
import xmlrpc.client
from unittest import mock

from odoo_xmlrpc_auth import cli
from odoo_xmlrpc_auth.client import AuthError, ConfigError, RequestError, ServerProxy

PASSWORD = "odoo_dummy_password_test"


class StoreTestCase(unittest.TestCase):
    def setUp(self):
        # Only the proxy that authenticates is replaced: ServerProxy subclasses
        # the real class, bound when the module was imported.
        patcher = mock.patch("xmlrpc.client.ServerProxy")
        self.common = patcher.start().return_value.__enter__.return_value
        self.addCleanup(patcher.stop)

    def use_home(self):
        home = tempfile.TemporaryDirectory()
        self.addCleanup(home.cleanup)
        patcher = mock.patch.dict(os.environ, {"HOME": home.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        return home.name

    def make_store(self, text, encoding="utf-8"):
        folder = os.path.join(self.use_home(), ".config", "odoo-xmlrpc-auth")
        os.makedirs(folder)
        os.chmod(folder, 0o700)
        path = os.path.join(folder, "erp.conf")
        with open(path, "w", encoding=encoding) as f:
            f.write(text)
        os.chmod(path, 0o600)

    def assertNeverShows(self, error, value):
        """``value`` is in no text ``error`` carries: not in its message, not
        in its repr, and no exception is chained to it."""
        self.assertNotIn(value, str(error))
        self.assertNotIn(value, repr(error))
        self.assertIsNone(error.__cause__)
        self.assertIsNone(error.__context__)


class ServerProxyTest(StoreTestCase):
    def test_the_proxy_authenticates_with_the_profile(self):
        self.make_store(
            "[odoo]\n"
            "url = https://odoo.example.com/\n"
            "db = odoo_db_test\n"
            "user = odoo_user_test\n"
            "password = odoo_dummy_password_test\n"
        )
        self.common.authenticate.return_value = 7
        ServerProxy(profile="erp")
        self.common.authenticate.assert_called_once_with(
            "odoo_db_test", "odoo_user_test", "odoo_dummy_password_test", {}
        )

    def test_a_missing_store_says_how_to_create_it(self):
        self.use_home()
        with self.assertRaisesRegex(
            ConfigError,
            r"Config file not found: .*/\.config/odoo-xmlrpc-auth/erp\.conf\n"
            r"Create it with a \[odoo\] section and keys: "
            r"url, db, user, password",
        ):
            ServerProxy(profile="erp")

    def test_a_missing_store_chains_no_error(self):
        self.use_home()
        with self.assertRaises(ConfigError) as caught:
            ServerProxy(profile="erp")
        self.assertIsNone(caught.exception.__cause__)
        self.assertIsNone(caught.exception.__context__)

    def test_a_store_without_a_section_never_shows_its_lines(self):
        self.make_store("password = odoo_dummy_password_test\n")
        with self.assertRaises(ConfigError) as caught:
            ServerProxy(profile="erp")
        self.assertIn("erp.conf is not a valid INI file", str(caught.exception))
        self.assertNeverShows(caught.exception, PASSWORD)

    def test_a_line_that_is_not_key_value_never_shows_its_lines(self):
        self.make_store(
            "[odoo]\n"
            "url = https://odoo.example.com\n"
            "odoo_dummy_password_test\n"
        )
        with self.assertRaises(ConfigError) as caught:
            ServerProxy(profile="erp")
        self.assertIn("erp.conf is not a valid INI file", str(caught.exception))
        self.assertNeverShows(caught.exception, PASSWORD)

    def test_a_store_that_is_not_utf8_never_shows_its_bytes(self):
        # Latin-1 writes "é" as a byte UTF-8 cannot decode. The decoding
        # error keeps the bytes it read, the password among them, in its repr.
        self.make_store(
            "[odoo]\n"
            "password = odoo_dummy_password_test\n"
            "note = é\n",
            encoding="latin-1",
        )
        with self.assertRaises(ConfigError) as caught:
            ServerProxy(profile="erp")
        self.assertIn("erp.conf is not a valid INI file", str(caught.exception))
        self.assertNeverShows(caught.exception, PASSWORD)


class UrlTest(StoreTestCase):
    """A url xmlrpc.client would refuse, or keep with a password in it, is
    refused when the store is loaded: their errors quote the url, the repr of
    a ProtocolError or a ServerProxy quotes its user info, and a url line
    that is wrong can hold the password."""

    def make_store_with_url(self, url):
        self.make_store(
            "[odoo]\n"
            f"url = {url}\n"
            "db = odoo_db_test\n"
            "user = odoo_user_test\n"
            "password = odoo_dummy_password_test\n"
        )

    def test_a_url_with_a_password_is_refused_unshown(self):
        self.make_store_with_url(
            "https://odoo_user_test:odoo_dummy_password_test@odoo.example.com"
        )
        with self.assertRaises(ConfigError) as caught:
            ServerProxy(profile="erp")
        self.assertIn("must have no user or password before its host", str(caught.exception))
        self.assertNeverShows(caught.exception, PASSWORD)
        self.common.authenticate.assert_not_called()

    def test_a_url_that_runs_onto_the_next_line_is_refused_unshown(self):
        # An indented line is read as part of the value above it.
        self.make_store_with_url("https://odoo.example.com\n  odoo_dummy_password_test")
        with self.assertRaises(ConfigError) as caught:
            ServerProxy(profile="erp")
        self.assertIn(
            "must be one line of printable ASCII without spaces", str(caught.exception)
        )
        self.assertNeverShows(caught.exception, PASSWORD)

    def test_a_url_with_a_space_is_refused_unshown(self):
        self.make_store_with_url("https://odoo.example.com/ odoo_dummy_password_test")
        with self.assertRaises(ConfigError) as caught:
            ServerProxy(profile="erp")
        self.assertIn(
            "must be one line of printable ASCII without spaces", str(caught.exception)
        )
        self.assertNeverShows(caught.exception, PASSWORD)

    def test_a_url_outside_ascii_is_refused_unshown(self):
        # http.client cannot encode such a path, and its error keeps the url.
        self.make_store_with_url("https://odoo.example.com/odoo_dummy_password_test_€")
        with self.assertRaises(ConfigError) as caught:
            ServerProxy(profile="erp")
        self.assertIn(
            "must be one line of printable ASCII without spaces", str(caught.exception)
        )
        self.assertNeverShows(caught.exception, PASSWORD)
        self.common.authenticate.assert_not_called()

    def test_a_url_with_a_port_that_is_not_a_number_is_refused_unshown(self):
        # A host left out reads the user as the host and the password as
        # its port.
        self.make_store_with_url("https://odoo_user_test:odoo_dummy_password_test")
        with self.assertRaises(ConfigError) as caught:
            ServerProxy(profile="erp")
        self.assertIn("cannot be parsed as a URL", str(caught.exception))
        self.assertNeverShows(caught.exception, PASSWORD)

    def test_a_url_line_holding_the_password_is_refused_unshown(self):
        self.make_store_with_url("odoo_dummy_password_test")
        with self.assertRaises(ConfigError) as caught:
            ServerProxy(profile="erp")
        self.assertIn("must start with http:// or https://", str(caught.exception))
        self.assertNeverShows(caught.exception, PASSWORD)

    def test_a_user_and_password_on_the_url_line_are_refused_unshown(self):
        # "odoouser:" reads as a scheme.
        self.make_store_with_url("odoouser:odoo_dummy_password_test")
        with self.assertRaises(ConfigError) as caught:
            ServerProxy(profile="erp")
        self.assertIn("must start with http:// or https://", str(caught.exception))
        self.assertNeverShows(caught.exception, PASSWORD)

    def test_a_url_with_another_scheme_is_refused(self):
        self.make_store_with_url("ftp://odoo.example.com")
        with self.assertRaisesRegex(
            ConfigError,
            r"The url in .*erp\.conf must start with http:// or https:// and "
            r"name a host$",
        ):
            ServerProxy(profile="erp")

    def test_a_url_with_no_host_is_refused_unshown(self):
        self.make_store_with_url("https:///odoo_dummy_password_test")
        with self.assertRaises(ConfigError) as caught:
            ServerProxy(profile="erp")
        self.assertIn("and name a host", str(caught.exception))
        self.assertNeverShows(caught.exception, PASSWORD)

    def test_a_url_that_cannot_be_split_is_refused_unshown(self):
        # NFKC turns the fullwidth solidus of the password into "/": urlsplit
        # refuses the url, quoting its user info, the password with it.
        self.make_store_with_url(
            "https://odoo_user_test:odoo_dummy_password_test／@odoo.example.com"
        )
        with self.assertRaises(ConfigError) as caught:
            ServerProxy(profile="erp")
        self.assertIn("cannot be parsed as a URL", str(caught.exception))
        self.assertNeverShows(caught.exception, PASSWORD)

    def test_a_url_with_a_port_passes(self):
        self.make_store_with_url("https://odoo.example.com:8069")
        self.common.authenticate.return_value = 7
        ServerProxy(profile="erp")
        xmlrpc.client.ServerProxy.assert_called_once_with(
            "https://odoo.example.com:8069/xmlrpc/2/common", allow_none=True
        )


class CallTest(StoreTestCase):
    """A failure of the transport, in the authentication or in a call, is a
    RequestError that names the profile and quotes nothing of the store."""

    def setUp(self):
        super().setUp()
        self.make_store(
            "[odoo]\n"
            "url = https://odoo.example.com\n"
            "db = odoo_db_test\n"
            "user = odoo_user_test\n"
            "password = odoo_dummy_password_test\n"
        )

    def fail_each_call_with(self, error):
        """Every call after the authentication raises ``error`` in xmlrpc.client's
        transport, as if the server could not be reached."""
        patcher = mock.patch.object(xmlrpc.client.Transport, "request", side_effect=error)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_refused_authentication_names_the_profile_only(self):
        self.common.authenticate.return_value = False
        with self.assertRaises(AuthError) as caught:
            ServerProxy(profile="erp")
        self.assertEqual(str(caught.exception), "Authentication failed for profile 'erp'")
        self.assertNeverShows(caught.exception, "odoo_user_test")
        self.assertNeverShows(caught.exception, "odoo_db_test")

    def test_a_refused_connection_is_a_request_error_with_its_errno(self):
        self.common.authenticate.side_effect = ConnectionRefusedError(
            111, "Connection refused"
        )
        with self.assertRaises(RequestError) as caught:
            ServerProxy(profile="erp")
        self.assertEqual(
            str(caught.exception),
            "A request of profile 'erp' failed: ConnectionRefusedError, code 111. "
            "Its message is not shown, as it can quote the profile's values.",
        )
        self.assertIsInstance(caught.exception, ConnectionRefusedError)

    def test_an_http_status_in_the_authentication_never_shows_the_url(self):
        self.common.authenticate.side_effect = xmlrpc.client.ProtocolError(
            "odoo.example.com/xmlrpc/2/common", 502, "Bad Gateway", {}
        )
        with self.assertRaises(RequestError) as caught:
            ServerProxy(profile="erp")
        self.assertNeverShows(caught.exception, "odoo.example.com")

    def test_a_protocol_error_keeps_its_status_for_except_protocol_error(self):
        self.common.authenticate.side_effect = xmlrpc.client.ProtocolError(
            "odoo.example.com/xmlrpc/2/common", 502, "Bad Gateway", {}
        )
        with self.assertRaises(xmlrpc.client.ProtocolError) as caught:
            ServerProxy(profile="erp")
        self.assertEqual(caught.exception.errcode, 502)
        self.assertEqual(caught.exception.url, "")
        self.assertEqual(caught.exception.errmsg, "")

    def test_an_http_status_in_a_call_never_shows_the_url(self):
        self.common.authenticate.return_value = 7
        proxy = ServerProxy(profile="erp")
        self.fail_each_call_with(xmlrpc.client.ProtocolError(
            "odoo.example.com/xmlrpc/2/object", 503, "Service Unavailable", {}
        ))
        with self.assertRaises(RequestError) as caught:
            proxy.execute_kw("res.partner", "read", [[1]])
        self.assertEqual(caught.exception.code, 503)
        self.assertNeverShows(caught.exception, "odoo.example.com")

    def test_an_odoo_fault_in_a_call_passes(self):
        # The server's own answer: its text is what a caller debugs with.
        self.common.authenticate.return_value = 7
        proxy = ServerProxy(profile="erp")
        self.fail_each_call_with(xmlrpc.client.Fault(2, "Record does not exist"))
        with self.assertRaises(xmlrpc.client.Fault) as caught:
            proxy.execute_kw("res.partner", "read", [[1]])
        self.assertNotIsInstance(caught.exception, RequestError)
        self.assertEqual(caught.exception.faultString, "Record does not exist")


class CliTest(StoreTestCase):
    def test_a_failure_of_the_transport_prints_one_line_without_the_url(self):
        self.make_store(
            "[odoo]\n"
            "url = https://odoo.example.com\n"
            "db = odoo_db_test\n"
            "user = odoo_user_test\n"
            "password = odoo_dummy_password_test\n"
        )
        self.common.authenticate.side_effect = xmlrpc.client.ProtocolError(
            "odoo.example.com/xmlrpc/2/common", 502, "Bad Gateway", {}
        )
        argv = ["odoo-xmlrpc-auth", "--profile", "erp", '{"model": "res.partner", "method": "search"}']
        stderr = io.StringIO()
        with mock.patch.object(sys, "argv", argv):
            with contextlib.redirect_stderr(stderr):
                with self.assertRaises(SystemExit) as caught:
                    cli.main()
        self.assertEqual(caught.exception.code, 1)
        self.assertEqual(
            stderr.getvalue(),
            "Error: A request of profile 'erp' failed: ProtocolError, code 502. "
            "Its message is not shown, as it can quote the profile's values.\n",
        )


if __name__ == "__main__":
    unittest.main()
