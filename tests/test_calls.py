"""Tests of Client over both APIs, against stub servers on 127.0.0.1.

XmlRpcOdoo and Json2Odoo (support.py) stand for Odoo: they answer from a
table each test fills and record what they get, so that a test checks what
the client sent, and where. Nothing reaches a real server or a real store.

Run from the repository root:
    PYTHONPATH=src python3 -m unittest discover -s tests
"""

import contextlib
import gzip
import io
import json
import os
import sys
import unittest
import xmlrpc.client
from unittest import mock

import requests

from odoo_xmlrpc_auth import cli
from odoo_xmlrpc_auth.client import (
    AuthError,
    Client,
    ConfigError,
    OdooError,
    RedirectError,
    RequestError,
    ServerProxy,
    _origin,
)
from support import (
    DB,
    KEY,
    PASSWORD,
    UID,
    USER,
    Answer,
    HttpRecorder,
    Json2Odoo,
    RawXmlRpc,
    StoreMixin,
    XmlRpcOdoo,
    free_port,
    odoo_error,
    xmlrpc_answer,
)


class StubTestCase(StoreMixin, unittest.TestCase):
    def setUp(self):
        self.use_home()

    def start(self, stub):
        self.addCleanup(stub.stop)
        return stub

    def make_profile(self, url, api, password, profile="erp"):
        self.make_store(
            "[odoo]\n"
            f"url = {url}\n"
            f"db = {DB}\n"
            f"user = {USER}\n"
            f"password = {password}\n"
            f"api = {api}\n",
            profile=profile,
        )

    def make_client(self, url, api, password, profile="erp"):
        self.make_profile(url, api, password, profile)
        client = Client(profile=profile)
        self.addCleanup(client.close)
        return client


class XmlRpcClientTest(StubTestCase):
    def setUp(self):
        super().setUp()
        self.odoo = self.start(XmlRpcOdoo())
        self.client = self.make_client(self.odoo.url, "xmlrpc", PASSWORD)

    def test_a_method_of_records_gets_the_ids_as_its_one_positional_argument(self):
        self.odoo.answers[("res.partner", "read")] = [{"id": 7, "name": "Example"}]
        result = self.client.call("res.partner", "read", [7], fields=["name"])
        self.assertEqual(result, [{"id": 7, "name": "Example"}])
        self.assertEqual(
            self.odoo.calls,
            [
                {
                    "model": "res.partner",
                    "method": "read",
                    "args": [[7]],
                    "kwargs": {"fields": ["name"]},
                }
            ],
        )

    def test_a_method_of_the_model_gets_no_positional_argument(self):
        self.odoo.answers[("res.partner", "search_read")] = [{"id": 7}]
        self.client.call(
            "res.partner", "search_read", domain=[["is_company", "=", True]], fields=["name"]
        )
        self.assertEqual(self.odoo.calls[0]["args"], [])
        self.assertEqual(
            self.odoo.calls[0]["kwargs"],
            {"domain": [["is_company", "=", True]], "fields": ["name"]},
        )

    def test_ids_as_a_tuple_or_empty_go_as_a_list(self):
        self.odoo.answers[("res.partner", "unlink")] = True
        self.client.call("res.partner", "unlink", (7, 8))
        self.client.call("res.partner", "unlink", [])
        self.assertEqual([call["args"] for call in self.odoo.calls], [[[7, 8]], [[]]])

    def test_a_nil_the_server_sends_comes_back_as_none(self):
        # Odoo itself fails such a reply (it marshals without allow_none):
        # this checks only that the client takes a nil it is sent.
        self.odoo.answers[("res.partner", "message_unsubscribe")] = None
        self.assertIsNone(self.client.call("res.partner", "message_unsubscribe", [7]))

    def test_the_answer_comes_back_as_the_server_sent_it(self):
        # Odoo's call_kw sends records as their ids; the stub sends ids, and
        # this checks only that the client hands them back as they came.
        self.odoo.answers[("res.partner", "copy")] = [8]
        self.assertEqual(self.client.call("res.partner", "copy", [7]), [8])

    def test_the_context_goes_by_name(self):
        self.odoo.answers[("res.partner", "read")] = []
        self.client.call("res.partner", "read", [7], context={"lang": "ca_ES"})
        self.assertEqual(self.odoo.calls[0]["kwargs"], {"context": {"lang": "ca_ES"}})

    def test_the_client_logs_in_at_its_first_call_only(self):
        self.odoo.answers[("res.partner", "read")] = []
        self.assertEqual(self.odoo.logins, 0)
        self.client.call("res.partner", "read", [7])
        self.client.call("res.partner", "read", [8])
        self.assertEqual(self.odoo.logins, 1)

    def test_a_fault_is_an_odoo_error_that_hides_the_password(self):
        self.odoo.answers[("res.partner", "unlink")] = xmlrpc.client.Fault(
            4, f"Access denied for {PASSWORD} on res.partner"
        )
        with self.assertRaises(OdooError) as caught:
            self.client.call("res.partner", "unlink", [7])
        self.assertEqual(caught.exception.code, 4)
        self.assertEqual(caught.exception.message, "Access denied for *** on res.partner")
        self.assertEqual(
            str(caught.exception),
            "Odoo answered a call of profile 'erp' with an error (code 4): "
            "Access denied for *** on res.partner",
        )
        self.assertNeverShows(caught.exception, PASSWORD)

    def test_a_fault_at_the_login_is_an_odoo_error_that_hides_the_password(self):
        # Odoo's answer to a login for a db it does not hold, as after a DNS
        # switch; this one also quotes the password, which must not show.
        self.odoo.login_fault = xmlrpc.client.Fault(
            1, f'database "{DB}" does not exist, login with {PASSWORD}'
        )
        error = self.raised(OdooError, lambda: self.client.call("res.partner", "read", [7]))
        self.assertEqual(error.code, 1)
        self.assertEqual(error.message, f'database "{DB}" does not exist, login with ***')
        self.assertNeverShows(error, PASSWORD)
        self.assertNoFrameHolds(error, PASSWORD)
        self.assertEqual(self.odoo.calls, [])

    def test_an_empty_password_hides_nothing_in_an_odoo_error(self):
        # Odoo refuses an empty password at the login; a server that takes it
        # must not get *** between every two characters of its message.
        self.odoo.password = ""
        self.odoo.answers[("res.partner", "unlink")] = xmlrpc.client.Fault(4, "Access denied")
        client = self.make_client(self.odoo.url, "xmlrpc", "", "other")
        with self.assertRaises(OdooError) as caught:
            client.call("res.partner", "unlink", [7])
        self.assertEqual(caught.exception.message, "Access denied")

    def test_a_refused_login_is_an_auth_error(self):
        client = self.make_client(self.odoo.url, "xmlrpc", "odoo_wrong_password_test", "other")
        with self.assertRaises(AuthError) as caught:
            client.call("res.partner", "read", [7])
        self.assertEqual(str(caught.exception), "Authentication failed for profile 'other'")
        self.assertEqual(self.odoo.calls, [])

    def test_a_refused_connection_is_a_request_error(self):
        client = self.make_client(f"http://127.0.0.1:{free_port()}", "xmlrpc", PASSWORD, "other")
        with self.assertRaises(RequestError) as caught:
            client.call("res.partner", "read", [7])
        self.assertIsInstance(caught.exception, ConnectionRefusedError)
        self.assertNeverShows(caught.exception, PASSWORD)


def _gzip_corrupt():
    """A gzip body whose first deflate byte is no block type: zlib's error."""
    body = bytearray(gzip.compress(xmlrpc.client.dumps((UID,), methodresponse=True).encode()))
    body[10] = 0xFF
    return bytes(body)


class NotXmlRpcTest(StubTestCase):
    """An answer that is not XML-RPC, to the login or to a call, is a
    RequestError that stands for xmlrpc.client's ResponseError, whatever its
    parser raised; no frame of it holds the password."""

    # Each answer, after what xmlrpc.client's parser raises for it.
    ANSWERS = {
        "ExpatError: a page that is not XML": Answer(200, b"<html><body>Log in</html>"),
        "ResponseError: XML that is no answer": Answer(200, b"<html><body>Log in</body></html>"),
        "ValueError: an int that is not one": Answer(200, xmlrpc_answer("<int>seven</int>")),
        "UnicodeEncodeError: base64 outside ASCII": Answer(
            200, xmlrpc_answer("<base64>é</base64>")
        ),
        "TypeError: a boolean neither 0 nor 1": Answer(200, xmlrpc_answer("<boolean>2</boolean>")),
        "TypeError: a fault that is not a struct": Answer(
            200,
            b"<?xml version='1.0'?><methodResponse><fault><value><int>1</int></value>"
            b"</fault></methodResponse>",
        ),
        "IndexError: a struct name without its value": Answer(
            200, xmlrpc_answer("<struct><member><name>id</name></member></struct>")
        ),
        "LookupError: XML that names an encoding Python does not know": Answer(
            200,
            b"<?xml version='1.0' encoding='x-unknown-test'?><methodResponse>"
            b"<params><param><value><int>7</int></value></param></params>"
            b"</methodResponse>",
        ),
        "InvalidOperation: a bigdecimal that is not a number": Answer(
            200, xmlrpc_answer("<bigdecimal>seven</bigdecimal>")
        ),
        "EOFError: a gzip body cut short": Answer(
            200,
            gzip.compress(xmlrpc.client.dumps((UID,), methodresponse=True).encode())[:-12],
            {"Content-Encoding": "gzip"},
        ),
        "zlib.error: a gzip body corrupt": Answer(
            200, _gzip_corrupt(), {"Content-Encoding": "gzip"}
        ),
    }

    def assertIsAnswerError(self, error):
        self.assertIsInstance(error, xmlrpc.client.ResponseError)
        self.assertEqual((error.error_class, error.code), ("ResponseError", None))
        self.assertNeverShows(error, PASSWORD)
        self.assertNoFrameHolds(error, PASSWORD)

    def test_an_answer_to_a_call_that_is_not_xml_rpc_is_a_request_error(self):
        odoo = self.start(RawXmlRpc())
        client = self.make_client(odoo.url, "xmlrpc", PASSWORD)
        for case, answer in self.ANSWERS.items():
            with self.subTest(case=case):
                odoo.call_answer = answer
                self.assertIsAnswerError(
                    self.raised(
                        RequestError,
                        lambda: client.call("res.partner", "search_count", domain=[]),
                    )
                )

    def test_an_answer_to_the_login_that_is_not_xml_rpc_is_a_request_error(self):
        odoo = self.start(RawXmlRpc())
        for number, (case, answer) in enumerate(self.ANSWERS.items()):
            with self.subTest(case=case):
                odoo.login_answer = answer
                client = self.make_client(odoo.url, "xmlrpc", PASSWORD, f"login-{number}")
                self.assertIsAnswerError(
                    self.raised(
                        RequestError,
                        lambda: client.call("res.partner", "search_count", domain=[]),
                    )
                )
        self.assertEqual(
            [request["path"] for request in odoo.requests],
            ["/xmlrpc/2/common"] * len(self.ANSWERS),
        )


class Json2ClientTest(StubTestCase):
    def setUp(self):
        super().setUp()
        self.odoo = self.start(Json2Odoo())
        self.client = self.make_client(self.odoo.url, "json2", KEY)

    def sent_body(self, request=0):
        return json.loads(self.odoo.requests[request]["body"])

    def test_a_method_of_records_gets_the_ids_by_name(self):
        self.odoo.answers[("res.partner", "read")] = [{"id": 7, "name": "Example"}]
        result = self.client.call("res.partner", "read", [7], fields=["name"])
        self.assertEqual(result, [{"id": 7, "name": "Example"}])
        [request] = self.odoo.requests
        self.assertEqual(request["method"], "POST")
        self.assertEqual(request["path"], "/json/2/res.partner/read")
        self.assertEqual(self.sent_body(), {"ids": [7], "fields": ["name"]})

    def test_a_method_of_the_model_gets_no_ids(self):
        self.odoo.answers[("res.partner", "search_read")] = [{"id": 7}]
        self.client.call(
            "res.partner", "search_read", domain=[["is_company", "=", True]], fields=["name"]
        )
        self.assertEqual(
            self.sent_body(), {"domain": [["is_company", "=", True]], "fields": ["name"]}
        )

    def test_ids_as_a_tuple_or_empty_go_as_a_list(self):
        self.odoo.answers[("res.partner", "unlink")] = True
        self.client.call("res.partner", "unlink", (7, 8))
        self.client.call("res.partner", "unlink", [])
        self.assertEqual([self.sent_body(0), self.sent_body(1)], [{"ids": [7, 8]}, {"ids": []}])

    def test_each_call_sends_the_key_the_db_and_json(self):
        self.odoo.answers[("res.partner", "read")] = []
        self.client.call("res.partner", "read", [7])
        headers = self.odoo.requests[0]["headers"]
        self.assertEqual(headers["authorization"], f"Bearer {KEY}")
        self.assertEqual(headers["x-odoo-database"], DB)
        self.assertEqual(headers["content-type"], "application/json")

    def test_a_null_the_server_sends_comes_back_as_none(self):
        # Odoo's JSON-2 controller sends None as null: this checks only that
        # the client hands a null back as None.
        self.odoo.answers[("res.partner", "message_unsubscribe")] = None
        self.assertIsNone(self.client.call("res.partner", "message_unsubscribe", [7]))

    def test_the_answer_comes_back_as_the_server_sent_it(self):
        # Odoo's JSON-2 controller sends records as their ids; the stub sends
        # ids, and this checks only that the client hands them back as they
        # came.
        self.odoo.answers[("res.partner", "copy")] = [8]
        self.assertEqual(self.client.call("res.partner", "copy", [7]), [8])

    def test_the_context_goes_by_name(self):
        self.odoo.answers[("res.partner", "read")] = []
        self.client.call("res.partner", "read", [7], context={"lang": "ca_ES"})
        self.assertEqual(self.sent_body(), {"ids": [7], "context": {"lang": "ca_ES"}})

    def test_a_parameter_named_model_or_method_goes_by_name(self):
        self.odoo.answers[("ir.model.access", "check")] = True
        self.client.call("ir.model.access", "check", model="res.partner", method="read")
        self.assertEqual(self.sent_body(), {"model": "res.partner", "method": "read"})

    def test_a_name_goes_quoted_into_its_segment_of_the_url(self):
        self.odoo.answers[("res.partner", "a%2Fb%3Fc")] = True
        self.client.call("res.partner", "a/b?c")
        self.assertEqual(self.odoo.requests[0]["path"], "/json/2/res.partner/a%2Fb%3Fc")

    def test_an_odoo_error_is_an_odoo_error_that_hides_the_key(self):
        self.odoo.answers[("res.partner", "write")] = Answer(
            422, odoo_error("odoo.exceptions.ValidationError", f"Wrong value: {KEY}")
        )
        with self.assertRaises(OdooError) as caught:
            self.client.call("res.partner", "write", [7], vals={"name": KEY})
        self.assertEqual(caught.exception.code, 422)
        self.assertEqual(caught.exception.message, "Wrong value: ***")
        self.assertNeverShows(caught.exception, KEY)

    def test_a_refused_key_is_an_auth_error(self):
        other_key = "f" * 40
        client = self.make_client(self.odoo.url, "json2", other_key, "other")
        with self.assertRaises(AuthError) as caught:
            client.call("res.partner", "read", [7])
        self.assertEqual(str(caught.exception), "Authentication failed for profile 'other'")
        self.assertNeverShows(caught.exception, other_key)

    def test_an_error_status_without_an_odoo_error_is_a_request_error(self):
        # A proxy's page, or Odoo's own when the db header names no database.
        self.odoo.answers[("res.partner", "read")] = Answer(
            502, f"<html>Bad Gateway {KEY}</html>".encode(), {"Content-Type": "text/html"}
        )
        with self.assertRaises(RequestError) as caught:
            self.client.call("res.partner", "read", [7])
        self.assertIsInstance(caught.exception, requests.HTTPError)
        self.assertEqual((caught.exception.error_class, caught.exception.code), ("HTTPError", 502))
        self.assertIsNone(caught.exception.response)
        self.assertNeverShows(caught.exception, KEY)

    def test_a_200_whose_body_is_not_json_is_a_request_error(self):
        self.odoo.answers[("res.partner", "read")] = Answer(
            200, f"<html>Log in {KEY}</html>".encode(), {"Content-Type": "text/html"}
        )
        with self.assertRaises(RequestError) as caught:
            self.client.call("res.partner", "read", [7])
        self.assertIsInstance(caught.exception, requests.JSONDecodeError)
        self.assertEqual(
            (caught.exception.error_class, caught.exception.code), ("JSONDecodeError", 200)
        )
        self.assertNeverShows(caught.exception, KEY)

    def test_a_refused_connection_is_a_request_error(self):
        client = self.make_client(f"http://127.0.0.1:{free_port()}", "json2", KEY, "other")
        with self.assertRaises(RequestError) as caught:
            client.call("res.partner", "read", [7])
        self.assertIsInstance(caught.exception, requests.ConnectionError)
        self.assertIsNone(caught.exception.request)
        self.assertNeverShows(caught.exception, KEY)

    def test_a_redirect_to_another_origin_is_refused_and_sends_nothing_there(self):
        elsewhere = self.start(HttpRecorder())
        port = elsewhere.server.server_address[1]
        targets = {
            301: f"{elsewhere.url}/json/2/res.partner/read",
            302: f"{elsewhere.url}/json/2/res.partner/read",
            303: f"{elsewhere.url}/json/2/res.partner/read",
            307: f"http://localhost:{port}/json/2/res.partner/read",
            308: f"https://127.0.0.1:{port}/json/2/res.partner/read",
        }
        for status, target in targets.items():
            with self.subTest(status=status, target=target):
                self.odoo.answers[("res.partner", "read")] = Answer(
                    status, b"", {"Location": target}
                )
                with self.assertRaises(RequestError) as caught:
                    self.client.call("res.partner", "read", [7])
                self.assertIsInstance(caught.exception, RedirectError)
                self.assertEqual(caught.exception.code, status)
                self.assertIsNone(caught.exception.request)
                self.assertIsNone(caught.exception.response)
                self.assertNeverShows(caught.exception, KEY)
        self.assertEqual(elsewhere.requests, [])

    def test_a_redirect_to_a_url_that_names_no_origin_is_refused(self):
        self.odoo.answers[("res.partner", "read")] = Answer(
            307, b"", {"Location": "http://127.0.0.1:notaport/json/2/res.partner/read"}
        )
        with self.assertRaises(RedirectError):
            self.client.call("res.partner", "read", [7])

    def test_a_redirect_to_a_location_that_is_not_a_url_is_refused(self):
        # A header goes as Latin-1: each character below is one byte sent.
        locations = {
            "not UTF-8": "/json/2/\xff\xfe",
            "a host that NFKC makes another": "http://a\xef\xbc\x8f@127.0.0.1/json/2/x",
            "an IPv6 host left open": "http://[::1/json/2/res.partner/read",
        }
        for case, location in locations.items():
            with self.subTest(case=case):
                self.odoo.answers[("res.partner", "read")] = Answer(
                    307, b"", {"Location": location}
                )
                error = self.raised(
                    RequestError, lambda: self.client.call("res.partner", "read", [7])
                )
                self.assertIsInstance(error, RedirectError)
                self.assertEqual(error.code, 307)
                self.assertNeverShows(error, KEY)
                self.assertNoFrameHolds(error, KEY)

    def test_a_200_nested_deeper_than_the_parser_follows_is_a_request_error(self):
        self.odoo.answers[("res.partner", "read")] = Answer(200, b"[" * 100000 + b"]" * 100000)
        error = self.raised(RequestError, lambda: self.client.call("res.partner", "read", [7]))
        self.assertIsInstance(error, requests.JSONDecodeError)
        self.assertEqual((error.error_class, error.code), ("JSONDecodeError", 200))

    def test_a_redirect_within_the_origin_is_followed_with_the_key(self):
        self.odoo.answers[("res.partner", "read")] = Answer(
            307, b"", {"Location": "/json/2/res.partner/search_read"}
        )
        self.odoo.answers[("res.partner", "search_read")] = [{"id": 7}]
        self.assertEqual(self.client.call("res.partner", "read", [7]), [{"id": 7}])
        followed = self.odoo.requests[1]
        self.assertEqual(followed["path"], "/json/2/res.partner/search_read")
        self.assertEqual(followed["headers"]["authorization"], f"Bearer {KEY}")
        self.assertEqual(self.sent_body(1), {"ids": [7]})

    def test_a_request_for_another_origin_gets_no_key_and_is_not_sent(self):
        elsewhere = self.start(HttpRecorder())
        url = f"{elsewhere.url}/json/2/res.partner/read"
        session = self.client._transport._session
        prepared = requests.Request("POST", url).prepare()
        with self.assertRaises(requests.exceptions.InvalidURL):
            session.auth(prepared)
        self.assertNotIn("Authorization", prepared.headers)
        with self.assertRaises(requests.exceptions.InvalidURL):
            session.post(url, data=b"{}")
        self.assertEqual(elsewhere.requests, [])

    def test_a_call_that_requests_would_send_to_another_host_is_refused(self):
        # The load check refuses this url (test_client.UrlTest); it is
        # patched out here to reach the check of each request behind it.
        # requests ends the host at the backslash: the call would go to
        # 127.0.0.1, port 80.
        with mock.patch("odoo_xmlrpc_auth.client._check_url"):
            client = self.make_client(
                "http://127.0.0.1\\.odoo.example.com:8069", "json2", KEY, "other"
            )
        error = self.raised(RequestError, lambda: client.call("res.partner", "read", [7]))
        self.assertIsInstance(error, requests.exceptions.InvalidURL)
        self.assertEqual((error.error_class, error.code), ("InvalidURL", None))
        self.assertNeverShows(error, KEY)

    def test_netrc_is_never_read(self):
        netrc_path = os.path.join(self._home, ".netrc")
        with open(netrc_path, "w", encoding="utf-8") as f:
            f.write("machine 127.0.0.1 login netrc_user password netrc_dummy_password_test\n")
        os.chmod(netrc_path, 0o600)
        # A redirect too: requests reads ~/.netrc again for the url it follows.
        self.odoo.answers[("res.partner", "read")] = Answer(
            307, b"", {"Location": "/json/2/res.partner/search_read"}
        )
        self.odoo.answers[("res.partner", "search_read")] = []
        with mock.patch("netrc.netrc", side_effect=AssertionError("~/.netrc was read")) as netrc:
            self.client.call("res.partner", "read", [7])
        netrc.assert_not_called()
        self.assertEqual(
            [request["headers"]["authorization"] for request in self.odoo.requests],
            [f"Bearer {KEY}", f"Bearer {KEY}"],
        )

    def test_no_proxy_of_the_environment_gets_the_call(self):
        # A proxy would get the key in clear for an http url.
        proxy = self.start(HttpRecorder())
        self.odoo.answers[("res.partner", "read")] = []
        with mock.patch.dict(os.environ, {"HTTP_PROXY": proxy.url, "http_proxy": proxy.url}):
            for name in ("NO_PROXY", "no_proxy"):
                os.environ.pop(name, None)
            self.client.call("res.partner", "read", [7])
        self.assertEqual(proxy.requests, [])
        self.assertEqual(len(self.odoo.requests), 1)

    def test_the_client_shows_its_profile_and_api_only(self):
        self.assertEqual(repr(self.client), "Client(profile='erp', api='json2')")

    def test_no_repr_of_the_client_or_its_parts_shows_the_key(self):
        # A debugger or `pytest -l` prints the reprs of a frame's locals,
        # requests' frames among them, which hold the session's auth.
        transport = self.client._transport
        for part in (self.client, transport, transport._session, transport._session.auth):
            with self.subTest(part=type(part).__name__):
                self.assertNotIn(KEY, repr(part))

    def test_an_origin_names_the_default_port_of_its_scheme(self):
        # A redirect that spells the default port stays within the origin.
        self.assertEqual(
            _origin("https://odoo.example.com/json/2"), _origin("https://odoo.example.com:443/x")
        )
        self.assertEqual(
            _origin("http://odoo.example.com/json/2"), _origin("http://odoo.example.com:80/x")
        )
        self.assertNotEqual(
            _origin("http://odoo.example.com/json/2"), _origin("https://odoo.example.com/json/2")
        )

    def test_a_model_or_a_method_that_is_not_a_name_is_refused_before_sending(self):
        for name in ("", ".", ".."):
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    self.client.call(name, "read", [7])
                with self.assertRaises(ValueError):
                    self.client.call("res.partner", name, [7])
        self.assertEqual(self.odoo.requests, [])

    def test_ids_that_are_not_a_list_are_refused_before_sending(self):
        with self.assertRaisesRegex(TypeError, "ids must be None"):
            self.client.call("res.partner", "read", 7)
        self.assertEqual(self.odoo.requests, [])


class FramesTest(StubTestCase):
    """No frame of an error the library raises holds the password or the
    API key in a local: a test runner or a debugger prints each frame's
    locals (pytest -l), and a log or a report can keep them. A frame that
    holds a ServerProxy, as each of its calls' frames does, shows its
    password one attribute down, in _password, readable as in 1.x:
    support._shown does not open a ServerProxy."""

    def test_no_frame_of_a_failed_xmlrpc_call_of_client_holds_the_password(self):
        odoo = self.start(XmlRpcOdoo())
        odoo.answers[("res.partner", "search_count")] = 3
        after_login = self.make_client(odoo.url, "xmlrpc", PASSWORD, "after-login")
        after_login.call("res.partner", "search_count", domain=[])
        odoo.stop()
        cases = {
            "a connection refused at the login": self.make_client(
                f"http://127.0.0.1:{free_port()}", "xmlrpc", PASSWORD, "refused"
            ),
            "a connection refused after the login": after_login,
        }
        for case, client in cases.items():
            with self.subTest(case=case):
                error = self.raised(
                    RequestError, lambda: client.call("res.partner", "search_count", domain=[])
                )
                self.assertNoFrameHolds(error, PASSWORD)

    def test_no_frame_of_a_refused_login_of_client_holds_the_password(self):
        odoo = self.start(XmlRpcOdoo())
        client = self.make_client(odoo.url, "xmlrpc", "odoo_wrong_password_test")
        error = self.raised(
            AuthError, lambda: client.call("res.partner", "search_count", domain=[])
        )
        self.assertNoFrameHolds(error, "odoo_wrong_password_test")

    def test_no_frame_of_a_failed_login_of_server_proxy_holds_the_password(self):
        odoo = self.start(XmlRpcOdoo())
        self.make_profile(f"http://127.0.0.1:{free_port()}", "xmlrpc", PASSWORD, "refused")
        self.make_profile(odoo.url, "xmlrpc", "odoo_wrong_password_test", "wrong")
        cases = {
            "refused": (RequestError, PASSWORD),
            "wrong": (AuthError, "odoo_wrong_password_test"),
        }
        for profile, (error_class, password) in cases.items():
            with self.subTest(profile=profile):
                error = self.raised(error_class, lambda: ServerProxy(profile=profile))
                self.assertNoFrameHolds(error, password)

    def test_no_frame_of_a_failed_call_of_server_proxy_holds_the_password(self):
        odoo = self.start(XmlRpcOdoo())
        self.make_profile(odoo.url, "xmlrpc", PASSWORD)
        proxy = ServerProxy(profile="erp")
        odoo.stop()
        error = self.raised(
            RequestError, lambda: proxy.execute_kw("res.partner", "search_count", [[]])
        )
        self.assertNoFrameHolds(error, PASSWORD)

    def test_no_frame_of_a_refused_profile_holds_the_password(self):
        # Each is refused after the password is read.
        self.make_profile("ftp://odoo.example.com", "xmlrpc", PASSWORD, "bad-url")
        self.make_store(
            "[odoo]\n"
            "url = https://odoo.example.com\n"
            "db = odoo db test\n"
            f"user = {USER}\n"
            f"password = {KEY}\n"
            "api = json2\n",
            profile="bad-db",
        )
        cases = {"bad-url": PASSWORD, "bad-db": KEY}
        for profile, password in cases.items():
            with self.subTest(profile=profile):
                error = self.raised(ConfigError, lambda: Client(profile=profile))
                self.assertNoFrameHolds(error, password)


class CliTest(StubTestCase):
    """The CLI calls by name through Client, and keeps execute_kw's
    positional args for an `api = xmlrpc` profile only."""

    def setUp(self):
        super().setUp()
        self.xmlrpc_odoo = self.start(XmlRpcOdoo())
        self.json2_odoo = self.start(Json2Odoo())
        self.make_profile(self.xmlrpc_odoo.url, "xmlrpc", PASSWORD, "erp-xmlrpc")
        self.make_profile(self.json2_odoo.url, "json2", KEY, "erp-json2")

    def run_cli(self, profile, payload):
        """Run the CLI on ``payload`` (JSON text, or a value sent as JSON);
        return its exit code, its stdout and its stderr."""
        text = payload if isinstance(payload, str) else json.dumps(payload)
        stdout, stderr = io.StringIO(), io.StringIO()
        code = 0
        argv = ["odoo-xmlrpc-auth", "--profile", profile, text]
        with mock.patch.object(sys, "argv", argv):
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                try:
                    cli.main()
                except SystemExit as exit_:
                    code = exit_.code
        return code, stdout.getvalue(), stderr.getvalue()

    def test_the_named_form_calls_over_json2(self):
        self.json2_odoo.answers[("res.partner", "read")] = [{"id": 7, "name": "Example"}]
        code, out, err = self.run_cli(
            "erp-json2",
            {"model": "res.partner", "method": "read", "ids": [7], "kwargs": {"fields": ["name"]}},
        )
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(json.loads(out), [{"id": 7, "name": "Example"}])
        self.assertEqual(
            json.loads(self.json2_odoo.requests[0]["body"]), {"ids": [7], "fields": ["name"]}
        )

    def test_the_named_form_calls_over_xmlrpc(self):
        self.xmlrpc_odoo.answers[("res.partner", "search_count")] = 3
        code, out, err = self.run_cli(
            "erp-xmlrpc",
            {"model": "res.partner", "method": "search_count", "kwargs": {"domain": []}},
        )
        self.assertEqual((code, out, err), (0, "3\n", ""))
        self.assertEqual(self.xmlrpc_odoo.calls[0]["args"], [])
        self.assertEqual(self.xmlrpc_odoo.calls[0]["kwargs"], {"domain": []})

    def test_the_args_form_calls_execute_kw_on_an_xmlrpc_profile(self):
        self.xmlrpc_odoo.answers[("res.partner", "read")] = [{"id": 7}]
        code, out, err = self.run_cli(
            "erp-xmlrpc",
            {"model": "res.partner", "method": "read", "args": [[7]], "kwargs": {"fields": ["id"]}},
        )
        self.assertEqual((code, out, err), (0, '[{"id": 7}]\n', ""))
        self.assertEqual(
            self.xmlrpc_odoo.calls,
            [{"model": "res.partner", "method": "read", "args": [[7]], "kwargs": {"fields": ["id"]}}],
        )
        self.assertEqual(self.xmlrpc_odoo.logins, 1)

    def test_the_args_form_is_refused_on_a_json2_profile(self):
        code, out, err = self.run_cli(
            "erp-json2", {"model": "res.partner", "method": "read", "args": [[7]]}
        )
        self.assertEqual((code, out), (1, ""))
        self.assertEqual(
            err,
            "Error: 'args' is for `api = xmlrpc` profiles, and profile 'erp-json2' has "
            "`api = json2`: give 'ids' and 'kwargs' instead\n",
        )
        self.assertEqual(self.json2_odoo.requests, [])

    def test_an_odoo_error_is_printed_on_stderr(self):
        self.json2_odoo.answers[("res.partner", "write")] = Answer(
            422, odoo_error("odoo.exceptions.ValidationError", "Wrong value")
        )
        code, out, err = self.run_cli(
            "erp-json2",
            {"model": "res.partner", "method": "write", "ids": [7], "kwargs": {"vals": {}}},
        )
        self.assertEqual((code, out), (1, ""))
        self.assertEqual(
            err,
            "Error: Odoo answered a call of profile 'erp-json2' with an error "
            "(code 422): Wrong value\n",
        )

    def test_a_payload_that_is_not_a_call_is_refused_before_any_call(self):
        call = {"model": "res.partner", "method": "read"}
        refused = {
            "[]": "the JSON payload must be an object",
            json.dumps({"model": "res.partner"}): "missing 'method' in JSON payload",
            json.dumps({**call, "model": 5}): "'model' in the JSON payload must be a string",
            json.dumps({**call, "kwarg": {}}): "unknown keys in the JSON payload: kwarg",
            json.dumps({**call, "ids": [7], "args": [[7]]}): (
                "give 'ids' or 'args' in the JSON payload, not both"
            ),
            json.dumps({**call, "ids": 7}): "'ids' in the JSON payload must be a list",
            json.dumps({**call, "args": {}}): "'args' in the JSON payload must be a list",
            json.dumps({**call, "kwargs": []}): "'kwargs' in the JSON payload must be an object",
        }
        for payload, message in refused.items():
            with self.subTest(payload=payload):
                for profile in ("erp-xmlrpc", "erp-json2"):
                    code, out, err = self.run_cli(profile, payload)
                    self.assertEqual((code, out, err), (1, "", f"Error: {message}\n"))
        self.assertEqual((self.xmlrpc_odoo.logins, self.json2_odoo.requests), (0, []))


if __name__ == "__main__":
    unittest.main()
