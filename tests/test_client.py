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
import itertools
import json
import os
import sys
import time
import unittest
import xmlrpc.client
from unittest import mock

from odoo_xmlrpc_auth import cli
from odoo_xmlrpc_auth.client import (
    ArgumentError,
    AuthError,
    Client,
    ConfigError,
    OdooError,
    RequestError,
    ServerProxy,
    _read_config,
    _Secret,
    _send,
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

    def test_a_missing_store_says_how_to_create_it_on_one_line(self):
        # The CLI prints the message after "Error:", on one line.
        self.use_home()
        with self.assertRaisesRegex(
            ConfigError,
            r"\AConfig file not found: [^\n]*/\.config/odoo-xmlrpc-auth/erp\.conf\. "
            r"Create it with a \[odoo\] section, the keys url, db, user, "
            r"password, and the line `api = xmlrpc` or `api = json2`\Z",
        ):
            ServerProxy(profile="erp")

    def test_a_missing_store_chains_no_error(self):
        self.use_home()
        with self.assertRaises(ConfigError) as caught:
            ServerProxy(profile="erp")
        self.assertIsNone(caught.exception.__cause__)
        self.assertIsNone(caught.exception.__context__)

    def test_a_store_that_cannot_be_read_says_why_and_chains_no_error(self):
        # A folder in the file's place: open() raises IsADirectoryError.
        home = self.use_home()
        folder = os.path.join(home, ".config", "odoo-xmlrpc-auth")
        os.makedirs(os.path.join(folder, "erp.conf"))
        os.chmod(folder, 0o700)
        for open_profile in (Client, ServerProxy):
            with self.subTest(open_profile=open_profile.__name__):
                with self.assertRaises(ConfigError) as caught:
                    open_profile(profile="erp")
                self.assertTrue(
                    str(caught.exception).endswith(
                        "erp.conf cannot be read (Is a directory): it must be a "
                        "file this user can read."
                    )
                )
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

    def test_a_host_that_idna_refuses_is_refused_unshown(self):
        # Both transports encode the host with IDNA before its lookup, and
        # its refusal quotes the host: an empty label, a label of 64.
        hosts = {
            "an empty label": "odoo..example.invalid",
            "a leading dot": ".odoo.example.invalid",
            "a label of 64": "a" * 64 + ".example.invalid",
        }
        for case, host in hosts.items():
            with self.subTest(case=case):
                self.make_store_with_url(f"http://{host}:8069")
                error = self.raised(ConfigError, lambda: ServerProxy(profile="erp"))
                self.assertIn(
                    "must name a host with no empty label between its dots, and "
                    "none longer than 63 characters",
                    str(error),
                )
                self.assertNeverShows(error, "example.invalid")
                self.assertNoFrameHolds(error, "example.invalid")
        self.common.authenticate.assert_not_called()

    def test_a_host_ending_in_a_dot_passes(self):
        # The root's empty label, which IDNA takes.
        self.make_store_with_url("https://odoo.example.com.")
        self.common.authenticate.return_value = 7
        ServerProxy(profile="erp")
        self.common.authenticate.assert_called_once()

    def test_no_frame_of_a_refused_url_holds_its_secret(self):
        # A debugger or `pytest -l` prints each frame's locals: the check's
        # frames held the url and its parts.
        urls = {
            "user info": (
                "https://odoo_user_test:odoo_other_secret_test@odoo.example.com"
            ),
            "port that is not a number": (
                "https://odoo_user_test:odoo_other_secret_test"
            ),
            "unsplittable": (
                "https://odoo_user_test:odoo_other_secret_test／@odoo.example.com"
            ),
        }
        for case, url in urls.items():
            with self.subTest(case=case):
                self.make_store_with_url(url)
                error = self.raised(ConfigError, lambda: ServerProxy(profile="erp"))
                self.assertNoFrameHolds(error, SECRET)

    def test_a_url_with_a_port_passes(self):
        self.make_store_with_url("https://odoo.example.com:8069")
        self.common.authenticate.return_value = 7
        ServerProxy(profile="erp")
        xmlrpc.client.ServerProxy.assert_called_once_with(
            "https://odoo.example.com:8069/xmlrpc/2/common", allow_none=True, transport=None
        )


def _quote_sequences(longest):
    """Every sequence of 1 to ``longest`` quotes, each ' or ": the quote
    added before a text at each repr of it, which makes repr write that
    text between double quotes where it can (a '), or between single
    quotes (a ")."""
    return [
        quotes
        for length in range(1, longest + 1)
        for quotes in itertools.product("'\"", repeat=length)
    ]


class SecretTest(unittest.TestCase):
    """The mask of a password covers every form Python's repr and ascii
    write it in, inside a longer text: between the double quotes or the
    single quotes of that text, and as its UTF-8 bytes; and each of them,
    and JSON's, inside any number of reprs of a text that holds it."""

    PASSWORD = "odoo_dummy'pass\\word_é_test"
    # The characters repr escapes, each in a password of its own.
    SPECIALS = {
        "a single quote": "'",
        "a double quote": '"',
        "a backslash": "\\",
        "a newline": "\n",
        "a tab": "\t",
        "a control character": "\x07",
        "a line separator": " ",
    }

    def assertMaskedOnce(self, masked):
        """``masked`` shows *** once, where the password was, and keeps the
        words around it."""
        self.assertNotIn("dummy", masked)
        self.assertNotIn("word", masked)
        self.assertEqual(masked.count("***"), 1)
        self.assertIn("left", masked)
        self.assertIn("right", masked)

    def test_a_repr_of_a_repr_of_the_password_is_masked_at_any_depth(self):
        # Each repr doubles the backslashes of the one inside it, and escapes
        # the quotes of its own kind.
        for case, special in self.SPECIALS.items():
            password = f"dummy{special}word"
            secret = _Secret(password)
            for quotes in _quote_sequences(4):
                text = f"left {password} right"
                for quote in quotes:
                    text = repr(quote + text)
                with self.subTest(case=case, quotes="".join(quotes)):
                    self.assertMaskedOnce(secret.hidden(text))

    def test_reprs_of_each_escaped_form_of_the_password_are_masked(self):
        encoders = {
            "ascii": ascii,
            "UTF-8 bytes": lambda text: repr(text.encode("utf-8")),
            "JSON": json.dumps,
            "JSON outside ASCII": lambda text: json.dumps(text, ensure_ascii=False),
        }
        for case, special in self.SPECIALS.items():
            password = f"dummy{special}word"
            secret = _Secret(password)
            for form, encode in encoders.items():
                for quotes in _quote_sequences(3):
                    text = encode(f"left {password} right")
                    for quote in quotes:
                        text = repr(quote + text)
                    with self.subTest(case=case, form=form, quotes="".join(quotes)):
                        self.assertMaskedOnce(secret.hidden(text))

    def test_each_form_masks_a_text_that_no_other_form_masks(self):
        # Each text holds the password in the form its case names only:
        # no other form of the password matches it.
        cases = {
            "as it is": ("dummy\x07word", lambda text: text, "left *** right"),
            "ascii": ("dummy_é_word", ascii, "'left *** right'"),
            "the repr of its UTF-8 bytes": (
                "dummy_é_word",
                lambda text: repr(text.encode("utf-8")),
                "b'left *** right'",
            ),
            "JSON": ("dummy_é_word", json.dumps, '"left *** right"'),
            "JSON without its escapes": (
                "dummy_é\x01word",
                lambda text: json.dumps(text, ensure_ascii=False),
                '"left *** right"',
            ),
            "its repr": ("dummy_é\x01word", repr, "'left *** right'"),
            "the repr of JSON without its escapes": (
                "dummy\x07 é_word",
                lambda text: repr(json.dumps(text, ensure_ascii=False)),
                "'\"left *** right\"'",
            ),
        }
        for case, (password, encode, masked) in cases.items():
            with self.subTest(case=case):
                text = encode(f"left {password} right")
                self.assertEqual(_Secret(password).hidden(text), masked)

    def test_no_form_is_cut_short_by_a_shorter_one_it_starts_with(self):
        # The form ascii writes of a password that ends in «Ã» (U+00C3),
        # \xc3, starts the repr of its UTF-8 bytes, \xc3\x83: matched in its
        # place, the shorter left the longer's last byte unmasked. «Â» (U+00C2),
        # whose UTF-8 is \xc3\x82, is next to it: its ascii form, \xc2,
        # starts none of its other forms. Each form as it is, twice in a
        # row, and inside a repr.
        cases = {
            "Ã": {
                "as it is, and as JSON without its escapes": "odoo_dummy_Ã",
                "as ascii writes it": "odoo_dummy_\\xc3",
                "as the repr of its UTF-8 bytes": "odoo_dummy_\\xc3\\x83",
                "as JSON": "odoo_dummy_\\u00c3",
            },
            "Â": {
                "as it is, and as JSON without its escapes": "odoo_dummy_Â",
                "as ascii writes it": "odoo_dummy_\\xc2",
                "as the repr of its UTF-8 bytes": "odoo_dummy_\\xc3\\x82",
                "as JSON": "odoo_dummy_\\u00c2",
            },
        }
        for last, forms in cases.items():
            secret = _Secret(f"odoo_dummy_{last}")
            for case, form in forms.items():
                with self.subTest(last=last, case=case):
                    self.assertEqual(secret.hidden(f"left {form} right"), "left *** right")
                    self.assertEqual(
                        secret.hidden(f"left {form}{form} right"), "left ****** right"
                    )
                    self.assertEqual(
                        secret.hidden(repr(f"left {form} right")), "'left *** right'"
                    )

    def test_an_occurrence_whose_start_another_form_takes_is_masked_whole(self):
        # A password that starts with a backslash, then x83, and ends in «Ã»:
        # ascii writes each occurrence as \\x83_dummy_pw_\xc3, and the repr of
        # its UTF-8 bytes, \\x83_dummy_pw_\xc3\x83, matches each one with the
        # \\x83 that starts the next. Tried first, in one alternation of the
        # forms, the longer form took that start, and the rest of the next
        # occurrence showed. Each match of the longer form overlaps the next
        # occurrence: the occurrences show as one ***.
        password = "\\x83_dummy_pw_Ã"
        secret = _Secret(password)
        masked = {1: "'***'", 2: "'***'", 3: "'***'"}
        for times, expected in masked.items():
            with self.subTest(times=times):
                self.assertEqual(secret.hidden(ascii(password * times)), expected)

    def test_occurrences_that_overlap_are_each_masked_whole(self):
        # A search that went on from the end of each match missed an
        # occurrence that starts inside it, and the rest of it showed. Each
        # password ends as it starts, as abcab, aXa and abab do.
        texts = {
            "ab_dummy_ab": "ab_dummy_ab_dummy_ab",
            "a_dummy_a": "a_dummy_a_dummy_a",
            "dummy_dummy_": "dummy_dummy_dummy_",
        }
        for password, text in texts.items():
            secret = _Secret(password)
            with self.subTest(password=password):
                self.assertEqual(secret.hidden(f"left {text} right"), "left *** right")
                self.assertEqual(secret.hidden(repr(f"left {text} right")), "'left *** right'")

    def test_an_occurrence_another_forms_occurrence_runs_into_is_masked_whole(self):
        # A password of U+0083 and _dummy_, twice, written as the repr of its
        # UTF-8 bytes, \xc2\x83_dummy_\xc2\x83_dummy_, then as its repr,
        # \x83_dummy_\x83_dummy_: the first's last \x83_dummy_ and the
        # second's first make an occurrence of the second's form too, which
        # overlaps the second itself. Searched from the end of each match,
        # that form found the first and missed the second, whose last
        # \x83_dummy_ showed.
        secret = _Secret("\x83_dummy_\x83_dummy_")
        text = "left \\xc2\\x83_dummy_\\xc2\\x83_dummy_\\x83_dummy_\\x83_dummy_ right"
        self.assertEqual(secret.hidden(text), "left *** right")
        self.assertEqual(secret.hidden(repr(text)), "'left *** right'")

    def test_reprs_of_json_without_its_escapes_are_masked(self):
        # Such JSON writes a control character as JSON does, and leaves
        # raw one outside ASCII that does not print, which a repr writes
        # as an escape: only the repr of that form stands for both.
        passwords = (
            "dummy\x07mid\xa0word",
            "dummy\x01mid\x7fword",
            "dummy\x07mid\xadword",
            "dummy\x07mid\x85word",
            "dummy\x07 é_word",
            "dummy\x08\xa0é_word",
        )
        for password in passwords:
            secret = _Secret(password)
            for quotes in _quote_sequences(3):
                text = json.dumps(f"left {password} right", ensure_ascii=False)
                for quote in quotes:
                    text = repr(quote + text)
                with self.subTest(password=ascii(password), quotes="".join(quotes)):
                    self.assertMaskedOnce(secret.hidden(text))

    def test_a_password_right_after_a_backslash_is_masked(self):
        # A path before it can end with one, such as C:\. Only a form that
        # starts with a backslash or a quote is matched from a run's start.
        secret = _Secret("dummy'word")
        for text in ("left C:\\dummy'word right", repr("left C:\\dummy'word right")):
            with self.subTest(text=text):
                self.assertMaskedOnce(secret.hidden(text))

    def test_a_password_bordered_by_backslashes_is_masked_at_each_occurrence(self):
        # A run that ends a form takes the whole run after it, with the
        # backslashes the next occurrence starts with, before a quote too.
        for password in ("\\dummy\\word\\", "'dummy_word\\", '"dummy_word\\'):
            secret = _Secret(password)
            for quotes in [()] + _quote_sequences(3):
                text = f"left {password}{password}{password} right"
                for quote in quotes:
                    text = repr(quote + text)
                with self.subTest(password=password, quotes="".join(quotes)):
                    self.assertMaskedOnce(secret.hidden(text))

    def test_right_after_an_occurrence_only_a_whole_one_is_masked(self):
        # Right after an occurrence that ends with no run, the password's
        # text without its leading backslash is not the password.
        self.assertEqual(
            _Secret("\\dummy_word").hidden("\\dummy_worddummy_word"), "***dummy_word"
        )
        # Occurrences that start with a letter, which no run takes, are each
        # masked on their own, also right after a trailing run: they touch,
        # without overlapping.
        self.assertEqual(_Secret("dummy\\word\\").hidden("dummy\\word\\" * 2), "******")

    def test_a_trailing_run_masks_the_backslash_of_an_escape_after_it(self):
        # It cannot tell its own backslashes from the escape's, whose number
        # each repr changes: it masks more, never less.
        secret = _Secret("dummy_word\\")
        self.assertEqual(secret.hidden(repr("dummy_word\\\n")), "'***n'")

    def test_a_long_run_of_backslashes_is_searched_in_linear_time(self):
        # A message of the server's can hold any text. Searched from each of
        # its backslashes, a run of 200,000 takes about half a minute on the
        # laptop the suite was written on; from its start only, milliseconds.
        secret = _Secret("\\dummy")
        text = "\\" * 200_000 + "x"
        start = time.perf_counter()
        self.assertEqual(secret.hidden(text), text)
        self.assertLess(time.perf_counter() - start, 1)

    def test_many_overlapping_occurrences_are_searched_in_linear_time(self):
        # Occurrences that overlap are each found whole by a search that
        # starts again one character after the start of each match (_spans).
        # Searched again on a copy of the text's rest after each match,
        # 200,000 occurrences that each overlap the next take about five
        # seconds on the laptop the suite was written on; searched in the text
        # itself, a tenth of a second.
        secret = _Secret("ab_dummy_ab")
        text = "ab_dummy_" * 200_000 + "ab"
        start = time.perf_counter()
        self.assertEqual(secret.hidden(text), "***")
        self.assertLess(time.perf_counter() - start, 1)

    def test_each_form_repr_and_ascii_write_the_password_in_is_masked(self):
        secret = _Secret(self.PASSWORD)
        texts = {
            "repr, with a double quote": (
                repr('he said "no" to ' + self.PASSWORD),
                """'he said "no" to ***'""",
            ),
            "repr, without": (repr("he said no to " + self.PASSWORD), '"he said no to ***"'),
            "ascii, with a double quote": (
                ascii('he said "no" to ' + self.PASSWORD),
                """'he said "no" to ***'""",
            ),
            "ascii, without": (ascii("he said no to " + self.PASSWORD), '"he said no to ***"'),
            "bytes, with a double quote": (
                repr(b'he said "no" to ' + self.PASSWORD.encode("utf-8")),
                """b'he said "no" to ***'""",
            ),
            "bytes, without": (
                repr(b"he said no to " + self.PASSWORD.encode("utf-8")),
                'b"he said no to ***"',
            ),
        }
        for case, (text, masked) in texts.items():
            with self.subTest(case=case):
                self.assertEqual(secret.hidden(text), masked)


class ClientSurfaceTest(unittest.TestCase):
    def test_call_is_the_one_public_method_that_sends_a_call(self):
        # execute_kw's positional form is the CLI's, for its payloads of 1.x:
        # a function of the module, not a second way to write a call.
        self.assertEqual(
            sorted(name for name in vars(Client) if not name.startswith("_")),
            ["call", "close"],
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
        # The url checks would refuse this url too: the check for the
        # password comes first, and its message says why.
        self.make_store_with(
            "odoo_dummy_password_test", "odoo_db_test", "odoo_user_test", PASSWORD, "xmlrpc"
        )
        error = self.raised(ConfigError, lambda: ServerProxy(profile="erp"))
        self.assertIn("The url of profile 'erp', in ", str(error))
        self.assertIn("holds its password", str(error))
        self.assertNoFrameHolds(error, PASSWORD)

    def test_a_value_holding_the_password_escaped_is_refused(self):
        password = 'odoo_dummy"pass\\word_test'
        # As JSON writes it: \" and \\.
        self.make_store_with(
            "https://odoo.example.com",
            'odoo_dummy\\"pass\\\\word_test',
            "odoo_user_test",
            password,
            "xmlrpc",
        )
        error = self.raised(ConfigError, lambda: ServerProxy(profile="erp"))
        self.assertIn("The db of profile 'erp', in ", str(error))
        self.common.authenticate.assert_not_called()

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


class OneLineTest(StoreTestCase):
    """Each message the library writes itself is one line. A path, the name
    of a type, a code or an encoder's text that holds a line break or
    another control character shows quoted and escaped as repr writes it,
    as a profile's name always does; Odoo's message ends an OdooError as it
    came."""

    XMLRPC_STORE = (
        "[odoo]\n"
        "url = https://odoo.example.com\n"
        "db = odoo_db_test\n"
        "user = odoo_user_test\n"
        "password = odoo_dummy_password_test\n"
        "api = xmlrpc\n"
    )

    def test_a_profile_name_holding_a_newline_is_quoted_escaped(self):
        self.use_home()
        error = self.raised(ConfigError, lambda: Client(profile="erp/\nnext"))
        self.assertEqual(str(error), "Invalid profile name: 'erp/\\nnext'")
        error = self.raised(ConfigError, lambda: Client(profile="erp\nnext"))
        self.assertRegex(
            str(error),
            r"\AConfig file not found: '[^\n]*/erp\\nnext\.conf'\. Create it with "
            r"a \[odoo\] section, the keys url, db, user, password, and the line "
            r"`api = xmlrpc` or `api = json2`\Z",
        )

    def test_each_message_naming_a_profiles_file_quotes_its_path_escaped(self):
        # Every check of the store after its file is found, for a profile
        # whose name holds a newline: the path of its file holds it too.
        path = r"'[^\n]*/erp\\nnext\.conf'"
        store = self.XMLRPC_STORE
        stores = {
            "not INI": ("odoo_dummy_password_test\n", rf"\A{path} is not a valid INI file\. "),
            "no section": ("[other]\nkey = value\n", rf"\AMissing \[odoo\] section in {path}\Z"),
            "a key missing": (
                "[odoo]\nurl = https://odoo.example.com\n",
                rf"\AMissing keys in {path}: db, user, password\Z",
            ),
            "no api line": (
                store.replace("api = xmlrpc\n", ""),
                rf"\AProfile 'erp\\nnext' has no api line: add [^\n]* section of {path}\Z",
            ),
            "another api": (
                store.replace("api = xmlrpc", "api = soap"),
                rf"\AThe api line of profile 'erp\\nnext' must be [^\n]*, in {path}\. ",
            ),
            "a user holding the password": (
                store.replace("user = odoo_user_test", "user = odoo_dummy_password_test"),
                rf"\AThe user of profile 'erp\\nnext', in {path}, holds its password\. ",
            ),
            "a url with another scheme": (
                store.replace("https://", "ftp://"),
                rf"\AThe url in {path} must start with ",
            ),
            "a json2 password that is no API key": (
                store.replace("api = xmlrpc", "api = json2"),
                rf"\AThe password of profile 'erp\\nnext', in {path}, must be an Odoo ",
            ),
            "a json2 db with a space": (
                store.replace("api = xmlrpc", "api = json2")
                .replace("odoo_dummy_password_test", KEY)
                .replace("db = odoo_db_test", "db = odoo db test"),
                rf"\AThe db of profile 'erp\\nnext', in {path}, must be one line ",
            ),
        }
        for case, (text, message) in stores.items():
            with self.subTest(case=case):
                self.make_store(text, profile="erp\nnext")
                error = self.raised(ConfigError, lambda: Client(profile="erp\nnext"))
                self.assertRegex(str(error), message)
                self.assertNotIn("\n", str(error))
        with self.subTest(case="open to others"):
            self.make_store(store, profile="erp\nnext")
            os.chmod(os.path.join(self._store_folder, "erp\nnext.conf"), 0o644)
            error = self.raised(ConfigError, lambda: Client(profile="erp\nnext"))
            self.assertRegex(
                str(error),
                rf"\A{path} is accessible by others\. Fix with: chmod 600 {path}\Z",
            )
        with self.subTest(case="a folder in the file's place"):
            os.remove(os.path.join(self._store_folder, "erp\nnext.conf"))
            os.makedirs(os.path.join(self._store_folder, "erp\nnext.conf"))
            error = self.raised(ConfigError, lambda: Client(profile="erp\nnext"))
            self.assertRegex(
                str(error),
                rf"\A{path} cannot be read \(Is a directory\): it must be a file this "
                r"user can read\.\Z",
            )

    def test_a_path_holding_a_newline_is_quoted_escaped(self):
        # The store's folder is under HOME, whose path holds the newline.
        home = self.use_home(prefix="home\nnext")
        error = self.raised(ConfigError, lambda: Client(profile="erp"))
        self.assertRegex(
            str(error), r"\AConfig file not found: '[^\n]*home\\nnext[^\n]*/erp\.conf'\. "
        )
        folder = os.path.join(home, ".config", "odoo-xmlrpc-auth")
        os.makedirs(folder)
        os.chmod(folder, 0o755)
        error = self.raised(ConfigError, lambda: Client(profile="erp"))
        self.assertRegex(
            str(error),
            r"\A'[^\n]*home\\nnext[^\n]*/odoo-xmlrpc-auth' is accessible by others\. "
            r"Fix with: chmod 700 '[^\n]*home\\nnext[^\n]*/odoo-xmlrpc-auth'\Z",
        )

    def test_a_type_name_or_a_code_holding_a_newline_is_quoted_escaped(self):
        weird = type("Weird\nError", (OSError,), {})

        def fail():
            # An OSError of two arguments takes the first as its errno.
            raise weird("5\nsix", "Weird failure")

        error = self.raised(RequestError, lambda: _send("erp", fail))
        self.assertEqual(
            str(error),
            "A request of profile 'erp' failed: 'Weird\\nError', code '5\\nsix'. "
            "Its message is not shown, as it can quote the profile's values.",
        )

    def test_a_reason_naming_a_type_with_a_newline_is_quoted_escaped(self):
        # xmlrpc.client refuses a subclass of a type it knows, and json any
        # type it does not, each naming the type in its text.
        self.make_store(self.XMLRPC_STORE)
        self.make_store(
            "[odoo]\n"
            "url = https://odoo.example.com\n"
            "db = odoo_db_test\n"
            "user = odoo_user_test\n"
            f"password = {KEY}\n"
            "api = json2\n",
            profile="erp-json2",
        )
        refusals = {
            "erp": (
                type("Weird\nValue", (int,), {"__module__": "values"})(5),
                "A call of profile 'erp' cannot be sent: "
                "\"cannot marshal <class 'values.Weird\\nValue'> objects\"",
            ),
            "erp-json2": (
                type("Weird\nValue", (), {})(),
                "A call of profile 'erp-json2' cannot be sent: "
                "'Object of type Weird\\nValue is not JSON serializable'",
            ),
        }
        for profile, (value, message) in refusals.items():
            with self.subTest(profile=profile):
                with Client(profile=profile) as odoo:
                    error = self.raised(
                        ArgumentError,
                        lambda: odoo.call("res.partner", "write", [7], vals=value),
                    )
                self.assertEqual(str(error), message)

    def test_a_store_that_cannot_be_read_for_an_error_with_no_reason_names_its_type(self):
        # An OSError with no errno has no strerror: its type's name says why.
        self.make_store(self.XMLRPC_STORE)
        weird = type("Weird\nError", (OSError,), {})
        with mock.patch("odoo_xmlrpc_auth.client.open", side_effect=weird(), create=True):
            error = self.raised(ConfigError, lambda: Client(profile="erp"))
        self.assertRegex(
            str(error),
            r"\A[^\n]*/erp\.conf cannot be read \('Weird\\nError'\): it must be a "
            r"file this user can read\.\Z",
        )

    def test_odoos_code_holding_a_newline_is_quoted_escaped_and_its_message_kept(self):
        self.make_store(self.XMLRPC_STORE)
        self.common.authenticate.return_value = 7
        fault = xmlrpc.client.Fault("4\nfour", "Access denied\non res.partner")
        with mock.patch.object(xmlrpc.client.Transport, "request", side_effect=fault):
            with Client(profile="erp") as odoo:
                error = self.raised(OdooError, lambda: odoo.call("res.partner", "read", [7]))
        self.assertEqual(
            str(error),
            "Odoo answered a call of profile 'erp' with an error (code '4\\nfour'): "
            "Access denied\non res.partner",
        )


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

    def test_a_missing_profile_prints_one_line(self):
        self.use_home()
        argv = ["odoo-xmlrpc-auth", "--profile", "erp", '{"model": "res.partner", "method": "search"}']
        stderr = io.StringIO()
        with mock.patch.object(sys, "argv", argv):
            with contextlib.redirect_stderr(stderr):
                with self.assertRaises(SystemExit) as caught:
                    cli.main()
        self.assertEqual(caught.exception.code, 1)
        self.assertRegex(
            stderr.getvalue(),
            r"\AError: Config file not found: [^\n]*/erp\.conf\. Create it with a "
            r"\[odoo\] section, the keys url, db, user, password, and the line "
            r"`api = xmlrpc` or `api = json2`\n\Z",
        )

    def run_cli(self, profile, payload='{"model": "res.partner", "method": "search"}'):
        """Run the CLI on ``payload`` for ``profile``, a call it refuses;
        return its exit code and its stderr."""
        argv = ["odoo-xmlrpc-auth", "--profile", profile, payload]
        stderr = io.StringIO()
        with mock.patch.object(sys, "argv", argv):
            with contextlib.redirect_stderr(stderr):
                with self.assertRaises(SystemExit) as caught:
                    cli.main()
        return caught.exception.code, stderr.getvalue()

    def test_a_profile_name_holding_a_newline_prints_one_line(self):
        self.use_home()
        code, err = self.run_cli("erp\nnext")
        self.assertEqual(code, 1)
        self.assertRegex(
            err,
            r"\AError: Config file not found: '[^\n]*/erp\\nnext\.conf'\. Create it "
            r"[^\n]*\n\Z",
        )

    def test_a_path_holding_a_newline_prints_one_line(self):
        self.use_home(prefix="home\nnext")
        code, err = self.run_cli("erp")
        self.assertEqual(code, 1)
        self.assertRegex(
            err,
            r"\AError: Config file not found: '[^\n]*home\\nnext[^\n]*/erp\.conf'\. "
            r"Create it [^\n]*\n\Z",
        )

    def test_a_payload_key_holding_a_newline_prints_one_line(self):
        self.use_home()
        code, err = self.run_cli(
            "erp", '{"model": "res.partner", "method": "search", "kw\\narg": 1}'
        )
        self.assertEqual(
            (code, err), (1, "Error: unknown keys in the JSON payload: 'kw\\narg'\n")
        )


if __name__ == "__main__":
    unittest.main()
