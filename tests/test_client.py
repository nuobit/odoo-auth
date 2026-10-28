"""Tests of odoo_xmlrpc_auth.client: no network, no real store.

The store is built under a temporary HOME, with dummy values only, and the
endpoint that authenticates is replaced, so no test can reach a server. A
store that cannot be read must not show its text: the error that says so
carries none of it, in its message, its repr or a chained exception.

Run from the repository root:
    PYTHONPATH=src python3 -m unittest discover -s tests
"""

import os
import tempfile
import unittest
from unittest import mock

from odoo_xmlrpc_auth.client import ConfigError, ServerProxy

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


if __name__ == "__main__":
    unittest.main()
