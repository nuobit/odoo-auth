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
import sys
import unittest
import xmlrpc.client
from unittest import mock

from odoo_xmlrpc_auth import cli
from odoo_xmlrpc_auth.client import (
    AuthError,
    Client,
    ConfigError,
    RequestError,
    ServerProxy,
    _read_config,
)
from support import KEY, PASSWORD, StoreMixin

# A secret that is not the store's password, in a url line (UrlTest).
SECRET = "odoo_other_secret_test"


class StoreTestCase(StoreMixin, unittest.TestCase):
    def setUp(self):
        # Only the proxy that authenticates is replaced: ServerProxy subclasses
        # the real class, bound when the module was imported.
        patcher = mock.patch("xmlrpc.client.ServerProxy")
        self.common = patcher.start().return_value.__enter__.return_value
        self.addCleanup(patcher.stop)


class ServerProxyTest(StoreTestCase):
    def test_the_proxy_authenticates_with_the_profile(self):
        self.make_store(
            "[odoo]\n"
            "url = https://odoo.example.com/\n"
            "db = odoo_db_test\n"
            "user = odoo_user_test\n"
            "password = odoo_dummy_password_test\n"
            "api = xmlrpc\n"
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
            r"Create it with a \[odoo\] section, the keys url, db, user, "
            r"password, and the line `api = xmlrpc` or `api = json2`$",
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
    that is wrong can hold a secret.

    A url holding the profile's own password is refused before these checks
    (PublicFieldTest): the urls here hold another secret, SECRET, such as
    another account's password."""

    def make_store_with_url(self, url):
        self.make_store(
            "[odoo]\n"
            f"url = {url}\n"
            "db = odoo_db_test\n"
            "user = odoo_user_test\n"
            "password = odoo_dummy_password_test\n"
            "api = xmlrpc\n"
        )

    def test_a_url_with_a_password_is_refused_unshown(self):
        self.make_store_with_url(
            "https://odoo_user_test:odoo_other_secret_test@odoo.example.com"
        )
        with self.assertRaises(ConfigError) as caught:
            ServerProxy(profile="erp")
        self.assertIn("must have no user or password before its host", str(caught.exception))
        self.assertNeverShows(caught.exception, SECRET)
        self.common.authenticate.assert_not_called()

    def test_a_url_that_runs_onto_the_next_line_is_refused_unshown(self):
        # An indented line is read as part of the value above it.
        self.make_store_with_url("https://odoo.example.com\n  odoo_other_secret_test")
        with self.assertRaises(ConfigError) as caught:
            ServerProxy(profile="erp")
        self.assertIn(
            "must be one line of printable ASCII without spaces", str(caught.exception)
        )
        self.assertNeverShows(caught.exception, SECRET)

    def test_a_url_with_a_space_is_refused_unshown(self):
        self.make_store_with_url("https://odoo.example.com/ odoo_other_secret_test")
        with self.assertRaises(ConfigError) as caught:
            ServerProxy(profile="erp")
        self.assertIn(
            "must be one line of printable ASCII without spaces", str(caught.exception)
        )
        self.assertNeverShows(caught.exception, SECRET)

    def test_a_url_outside_ascii_is_refused_unshown(self):
        # http.client cannot encode such a path, and its error keeps the url.
        self.make_store_with_url("https://odoo.example.com/odoo_other_secret_test_€")
        with self.assertRaises(ConfigError) as caught:
            ServerProxy(profile="erp")
        self.assertIn(
            "must be one line of printable ASCII without spaces", str(caught.exception)
        )
        self.assertNeverShows(caught.exception, SECRET)
        self.common.authenticate.assert_not_called()

    def test_a_url_with_a_port_that_is_not_a_number_is_refused_unshown(self):
        # A host left out reads the user as the host and the password as
        # its port.
        self.make_store_with_url("https://odoo_user_test:odoo_other_secret_test")
        with self.assertRaises(ConfigError) as caught:
            ServerProxy(profile="erp")
        self.assertIn("cannot be parsed as a URL", str(caught.exception))
        self.assertNeverShows(caught.exception, SECRET)

    def test_a_url_line_holding_a_secret_is_refused_unshown(self):
        self.make_store_with_url("odoo_other_secret_test")
        with self.assertRaises(ConfigError) as caught:
            ServerProxy(profile="erp")
        self.assertIn("must start with http:// or https://", str(caught.exception))
        self.assertNeverShows(caught.exception, SECRET)

    def test_a_user_and_password_on_the_url_line_are_refused_unshown(self):
        # "odoouser:" reads as a scheme.
        self.make_store_with_url("odoouser:odoo_other_secret_test")
        with self.assertRaises(ConfigError) as caught:
            ServerProxy(profile="erp")
        self.assertIn("must start with http:// or https://", str(caught.exception))
        self.assertNeverShows(caught.exception, SECRET)

    def test_a_url_with_another_scheme_is_refused(self):
        self.make_store_with_url("ftp://odoo.example.com")
        with self.assertRaisesRegex(
            ConfigError,
            r"The url in .*erp\.conf must start with http:// or https:// and "
            r"name a host$",
        ):
            ServerProxy(profile="erp")

    def test_a_url_with_no_host_is_refused_unshown(self):
        self.make_store_with_url("https:///odoo_other_secret_test")
        with self.assertRaises(ConfigError) as caught:
            ServerProxy(profile="erp")
        self.assertIn("and name a host", str(caught.exception))
        self.assertNeverShows(caught.exception, SECRET)

    def test_a_url_that_cannot_be_split_is_refused_unshown(self):
        # NFKC turns the fullwidth solidus of the password into "/": urlsplit
        # refuses the url, quoting its user info, the password with it.
        self.make_store_with_url(
            "https://odoo_user_test:odoo_other_secret_test／@odoo.example.com"
        )
        with self.assertRaises(ConfigError) as caught:
            ServerProxy(profile="erp")
        self.assertIn("cannot be parsed as a URL", str(caught.exception))
        self.assertNeverShows(caught.exception, SECRET)

    def test_a_url_with_a_backslash_is_refused_unshown(self):
        # requests ends the host at the backslash: the calls would go to
        # evil.example, where urllib.parse reads the profile's origin as the
        # whole name.
        self.make_store_with_url("https://evil.example\\.odoo.example.com")
        with self.assertRaises(ConfigError) as caught:
            ServerProxy(profile="erp")
        self.assertIn("must hold no backslash", str(caught.exception))
        self.assertNeverShows(caught.exception, "evil.example")
        self.common.authenticate.assert_not_called()

    def test_a_url_with_port_0_is_refused(self):
        # requests drops port 0 and goes to the scheme's default port.
        self.make_store_with_url("https://odoo.example.com:0")
        with self.assertRaisesRegex(
            ConfigError,
            r"The url in .*erp\.conf must name a port from 1 to 65535, if it "
            r"names one$",
        ):
            ServerProxy(profile="erp")
        self.common.authenticate.assert_not_called()

    def test_a_url_with_a_port_passes(self):
        self.make_store_with_url("https://odoo.example.com:8069")
        self.common.authenticate.return_value = 7
        ServerProxy(profile="erp")
        xmlrpc.client.ServerProxy.assert_called_once_with(
            "https://odoo.example.com:8069/xmlrpc/2/common", allow_none=True
        )


class ApiLineTest(StoreTestCase):
    """Every profile says which API its calls go through, with the line
    `api = xmlrpc` or `api = json2`; there is no default."""

    def make_store_with_api_line(self, line, password="odoo_dummy_password_test"):
        self.make_store(
            "[odoo]\n"
            "url = https://odoo.example.com\n"
            "db = odoo_db_test\n"
            "user = odoo_user_test\n"
            f"password = {password}\n"
            f"{line}"
        )

    def test_a_profile_without_an_api_line_is_refused_before_any_call(self):
        self.make_store_with_api_line("")
        for open_profile in (Client, ServerProxy):
            with self.subTest(open_profile=open_profile.__name__):
                with self.assertRaisesRegex(
                    ConfigError,
                    r"^Profile 'erp' has no api line: add `api = xmlrpc` or "
                    r"`api = json2` to the \[odoo\] section of .*/erp\.conf$",
                ):
                    open_profile(profile="erp")
        self.common.authenticate.assert_not_called()

    def test_another_value_is_refused_unshown(self):
        for value in ("JSON2", "xml-rpc", "odoo_dummy_password_test"):
            with self.subTest(value=value):
                self.make_store_with_api_line(f"api = {value}\n")
                with self.assertRaises(ConfigError) as caught:
                    Client(profile="erp")
                self.assertRegex(
                    str(caught.exception),
                    r"^The api line of profile 'erp' must be `api = xmlrpc` or "
                    r"`api = json2`, in .*/erp\.conf\. Its value is not shown\.$",
                )
                self.assertNeverShows(caught.exception, value)

    def test_api_xmlrpc_opens_the_proxy_and_the_client(self):
        self.make_store_with_api_line("api = xmlrpc\n")
        self.common.authenticate.return_value = 7
        ServerProxy(profile="erp")
        with Client(profile="erp") as odoo:
            self.assertEqual(odoo.api, "xmlrpc")

    def test_api_json2_opens_the_client(self):
        self.make_store_with_api_line("api = json2\n", password=KEY)
        with Client(profile="erp") as odoo:
            self.assertEqual(odoo.api, "json2")

    def test_the_proxy_refuses_a_json2_profile_and_names_the_client(self):
        self.make_store_with_api_line("api = json2\n", password=KEY)
        with self.assertRaisesRegex(
            ConfigError,
            r"^Profile 'erp' has `api = json2`, and ServerProxy speaks XML-RPC "
            r"only: call it through odoo_xmlrpc_auth\.client\.Client"
            r"\(profile='erp'\)$",
        ):
            ServerProxy(profile="erp")
        self.common.authenticate.assert_not_called()


class Json2StoreTest(StoreTestCase):
    """JSON-2 sends the password and the db in headers, which refuse some
    values with an error that quotes them: a json2 profile whose values
    cannot go there is refused before any call, without showing them."""

    def make_json2_store(self, password=KEY, db="odoo_db_test"):
        self.make_store(
            "[odoo]\n"
            "url = https://odoo.example.com\n"
            f"db = {db}\n"
            "user = odoo_user_test\n"
            f"password = {password}\n"
            "api = json2\n"
        )

    def test_a_password_that_is_not_an_api_key_is_refused_unshown(self):
        for password in (PASSWORD, KEY.upper(), KEY + "0", KEY[:-1] + "é"):
            with self.subTest(password=password):
                self.make_json2_store(password=password)
                with self.assertRaises(ConfigError) as caught:
                    Client(profile="erp")
                self.assertIn(
                    "must be an Odoo API key, 40 lowercase hexadecimal digits",
                    str(caught.exception),
                )
                self.assertNeverShows(caught.exception, password)

    def test_a_db_that_cannot_go_in_a_header_is_refused_unshown(self):
        # An indented line is read as part of the value above it.
        for db in ("odoo db test", "odoo_db_test_é", "odoo_db_test\n  next"):
            with self.subTest(db=db):
                self.make_json2_store(db=db)
                with self.assertRaises(ConfigError) as caught:
                    Client(profile="erp")
                self.assertIn(
                    "must be one line of printable ASCII without spaces, as "
                    "JSON-2 sends it in a header",
                    str(caught.exception),
                )
                self.assertNeverShows(caught.exception, db.split("\n")[0])


class PublicFieldTest(StoreTestCase):
    """A caller may print a profile's url, db, user and api: a profile in
    which one of them holds the password, or the API key, is refused before
    any call, with a message that names the profile and the field."""

    def make_store_with(self, url, db, user, password, api):
        self.make_store(
            "[odoo]\n"
            f"url = {url}\n"
            f"db = {db}\n"
            f"user = {user}\n"
            f"password = {password}\n"
            f"api = {api}\n"
        )

    def test_a_value_holding_the_password_is_refused_unshown(self):
        # Answers written on the wrong line.
        profiles = {
            "url": ("https://odoo_dummy_password_test.example.com", "odoo_db_test", "odoo_user_test"),
            "db": ("https://odoo.example.com", "odoo_dummy_password_test", "odoo_user_test"),
            "user": ("https://odoo.example.com", "odoo_db_test", "odoo_dummy_password_test"),
        }
        for key, (url, db, user) in profiles.items():
            with self.subTest(key=key):
                self.make_store_with(url, db, user, PASSWORD, "xmlrpc")
                error = self.raised(ConfigError, lambda: ServerProxy(profile="erp"))
                self.assertIn(f"The {key} of profile 'erp', in ", str(error))
                self.assertIn("holds its password", str(error))
                self.assertNeverShows(error, PASSWORD)
                self.assertNoFrameHolds(error, PASSWORD)
        self.common.authenticate.assert_not_called()

    def test_a_url_line_holding_the_password_is_refused_for_holding_it(self):
        # The url checks would refuse this url too, in a frame that holds it:
        # the check for the password comes first.
        self.make_store_with(
            "odoo_dummy_password_test", "odoo_db_test", "odoo_user_test", PASSWORD, "xmlrpc"
        )
        error = self.raised(ConfigError, lambda: ServerProxy(profile="erp"))
        self.assertIn("The url of profile 'erp', in ", str(error))
        self.assertIn("holds its password", str(error))
        self.assertNoFrameHolds(error, PASSWORD)

    def test_a_url_holding_the_api_key_is_refused_unshown(self):
        self.make_store_with(
            f"https://{KEY}.example.com", "odoo_db_test", "odoo_user_test", KEY, "json2"
        )
        error = self.raised(ConfigError, lambda: Client(profile="erp"))
        self.assertIn("The url of profile 'erp', in ", str(error))
        self.assertNeverShows(error, KEY)
        self.assertNoFrameHolds(error, KEY)

    def test_a_password_that_occurs_in_a_value_by_chance_is_refused(self):
        # It fails closed. The message is not checked for the password: the
        # file's path it names holds such a short one too.
        cases = {
            "url": ("https://lab.example.com", "ab"),
            "api": ("https://odoo.example.com", "rpc"),
        }
        for key, (url, password) in cases.items():
            with self.subTest(key=key):
                self.make_store_with(url, "odoo_db_test", "odoo_user_test", password, "xmlrpc")
                error = self.raised(ConfigError, lambda: Client(profile="erp"))
                self.assertIn(f"The {key} of profile 'erp', in ", str(error))
                self.assertIn(f"occurs in the {key}", str(error))


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
            "api = xmlrpc\n"
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


class ScriptsTest(StoreTestCase):
    """Scripts that log in to Odoo's web themselves, to download a report,
    read a profile's values through _read_config, or the password a
    ServerProxy keeps: both stay in clear for them."""

    def setUp(self):
        super().setUp()
        self.make_store(
            "[odoo]\n"
            "url = https://odoo.example.com/\n"
            "db = odoo_db_test\n"
            "user = odoo_user_test\n"
            "password = odoo_dummy_password_test\n"
            "api = xmlrpc\n"
        )

    def test_read_config_gives_the_values_with_the_password_in_clear(self):
        self.assertEqual(
            _read_config("erp"),
            {
                "url": "https://odoo.example.com",
                "db": "odoo_db_test",
                "user": "odoo_user_test",
                "password": "odoo_dummy_password_test",
                "api": "xmlrpc",
            },
        )

    def test_read_config_gives_every_key_of_the_section_as_1_2_0_did(self):
        # A script reads a key of its own the section holds.
        self.make_store(
            "[odoo]\n"
            "url = https://odoo.example.com//\n"
            "db = odoo_db_test\n"
            "user = odoo_user_test\n"
            "password = odoo_dummy_password_test\n"
            "api = xmlrpc\n"
            "report_folder = invoices\n",
            profile="with-more",
        )
        self.assertEqual(
            _read_config("with-more"),
            {
                "url": "https://odoo.example.com",
                "db": "odoo_db_test",
                "user": "odoo_user_test",
                "password": "odoo_dummy_password_test",
                "api": "xmlrpc",
                "report_folder": "invoices",
            },
        )

    def test_read_config_refuses_a_profile_the_library_refuses(self):
        self.make_store(
            "[odoo]\n"
            "url = https://odoo.example.com\n"
            "db = odoo_db_test\n"
            "user = odoo_user_test\n"
            "password = odoo_dummy_password_test\n",
            profile="no-api",
        )
        with self.assertRaisesRegex(ConfigError, "^Profile 'no-api' has no api line"):
            _read_config("no-api")

    def test_the_proxy_keeps_the_password_in_clear(self):
        self.common.authenticate.return_value = 7
        proxy = ServerProxy(profile="erp")
        self.assertEqual(
            (proxy._db, proxy._uid, proxy._password),
            ("odoo_db_test", 7, "odoo_dummy_password_test"),
        )


class CliTest(StoreTestCase):
    def test_a_failure_of_the_transport_prints_one_line_without_the_url(self):
        self.make_store(
            "[odoo]\n"
            "url = https://odoo.example.com\n"
            "db = odoo_db_test\n"
            "user = odoo_user_test\n"
            "password = odoo_dummy_password_test\n"
            "api = xmlrpc\n"
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
