"""Tests of Client over both APIs, against stub servers on 127.0.0.1.

XmlRpcOdoo and Json2Odoo (support.py) stand for Odoo: they answer from a
table each test fills and record what they get, so that a test checks what
the client sent, and where. Nothing reaches a real server or a real store.

Run from the repository root:
    PYTHONPATH=src python3 -m unittest discover -s tests
"""

import contextlib
import datetime
import gc
import gzip
import io
import json
import os
import socket
import ssl
import sys
import threading
import time
import unittest
import warnings
import xmlrpc.client
from unittest import mock

import requests

import odoo_xmlrpc_auth.client
from odoo_xmlrpc_auth import cli
from odoo_xmlrpc_auth.client import (
    ArgumentError,
    AuthError,
    Client,
    ConfigError,
    OdooError,
    RedirectError,
    RequestError,
    ServerProxy,
    _execute_kw,
    _origin,
    _SafeTransport,
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


def retried(attempt, waited_for):
    """Call ``attempt`` until it returns true, again each time it returns
    false, for 5 s at most; past them, fail, saying that the test waited for
    ``waited_for``. No try sleeps a duration: ``attempt`` lets the other
    threads run, if it needs them to."""
    seconds = 5
    deadline = time.monotonic() + seconds
    while not attempt():
        if time.monotonic() > deadline:
            raise AssertionError(f"waited {seconds} s for {waited_for}")


def collect_garbage():
    """Collect the garbage of every generation, as gc.collect() does, and
    make sure the collection ran: while another thread is in one, which a
    finalizer that lets the GIL go keeps running as this thread runs,
    gc.collect() returns at once and collects nothing. Each try lets the
    other threads go on (time.sleep), then drops a reference cycle, which
    the collection finds if it ran, unless one another thread starts takes
    the cycle first; the tries go on until one finds something (retried)."""

    def collected():
        time.sleep(0)
        cycle = []
        cycle.append(cycle)
        del cycle
        return gc.collect() > 0

    retried(collected, "a collection of garbage in this thread")


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

    def record_timeouts(self):
        """The list where each timeout given to a socket, from now to the end
        of the test, is recorded in order. The stubs' sockets get none."""
        timeouts = []
        settimeout = socket.socket.settimeout

        def recording(sock, seconds):
            timeouts.append(seconds)
            settimeout(sock, seconds)

        patcher = mock.patch.object(socket.socket, "settimeout", recording)
        patcher.start()
        self.addCleanup(patcher.stop)
        return timeouts

    def fail_on_resource_warnings(self):
        """Fail the test if it leaves a socket or a file open: from now to
        its end a ResourceWarning is an error, and a collection of garbage,
        which closes what was left open with that warning, runs once the
        test is over (collect_garbage). Raised in a finalizer, the error
        reaches sys.unraisablehook, which records it here; any other
        exception the hook gets goes on to the hook it replaces. Once that
        collection ends, whatever ends it, the hook it replaced and the
        warnings' filters it found are put back."""
        warned = []
        previous_hook = sys.unraisablehook

        def hook(unraisable):
            if issubclass(unraisable.exc_type, ResourceWarning):
                warned.append(str(unraisable.exc_value))
            else:
                previous_hook(unraisable)

        filters = warnings.catch_warnings()
        filters.__enter__()
        warnings.simplefilter("error", ResourceWarning)
        patcher = mock.patch.object(sys, "unraisablehook", hook)
        patcher.start()

        def check():
            # collect_garbage fails once it has waited 5 s (retried): the
            # hook and the filters, left in place, would stay for the rest of
            # the run.
            try:
                collect_garbage()
            finally:
                patcher.stop()
                filters.__exit__(None, None, None)
            self.assertEqual(warned, [])

        self.addCleanup(check)


class XmlRpcClientTest(StubTestCase):
    def setUp(self):
        super().setUp()
        self.odoo = self.start(XmlRpcOdoo())
        self.client = self.make_client(self.odoo.url, "xmlrpc", PASSWORD)

    def test_a_method_of_records_gets_the_ids_as_its_one_positional_argument(self):
        # copy has no leading parameter that goes positionally.
        self.odoo.answers[("res.partner", "copy")] = [8]
        result = self.client.call("res.partner", "copy", [7], default={"name": "Example"})
        self.assertEqual(result, [8])
        self.assertEqual(
            self.odoo.calls,
            [
                {
                    "model": "res.partner",
                    "method": "copy",
                    "args": [[7]],
                    "kwargs": {"default": {"name": "Example"}},
                }
            ],
        )

    def test_execute_kw_sends_its_args_and_kwargs_as_they_are(self):
        self.odoo.answers[("res.partner", "search")] = [7]
        result = _execute_kw(
            self.client, "res.partner", "search", [[["name", "=", "Example"]]], {"limit": 1}
        )
        self.assertEqual(result, [7])
        self.assertEqual(
            self.odoo.calls,
            [
                {
                    "model": "res.partner",
                    "method": "search",
                    "args": [[["name", "=", "Example"]]],
                    "kwargs": {"limit": 1},
                }
            ],
        )

    def test_execute_kw_without_kwargs_sends_none(self):
        self.odoo.answers[("res.partner", "search_count")] = 3
        self.assertEqual(_execute_kw(self.client, "res.partner", "search_count", [[]]), 3)
        self.assertEqual(self.odoo.calls[0]["kwargs"], {})

    def test_execute_kw_refuses_args_or_kwargs_of_another_type_unsent(self):
        refusals = {
            "args not a list": (
                ("res.partner", "search", {"domain": []}),
                "args must be the list of execute_kw's positional arguments",
            ),
            "kwargs not a dict": (
                ("res.partner", "search", [[]], [("limit", 1)]),
                "kwargs must be None or the dict of execute_kw's named arguments",
            ),
        }
        for case, (arguments, reason) in refusals.items():
            with self.subTest(case=case):
                error = self.raised(
                    TypeError, lambda: _execute_kw(self.client, *arguments)
                )
                self.assertIsInstance(error, ArgumentError)
                self.assertEqual(error.reason, reason)
        self.assertEqual((self.odoo.logins, self.odoo.calls), (0, []))

    def test_execute_kw_refuses_a_json2_profile_unsent(self):
        odoo = self.start(Json2Odoo())
        client = self.make_client(odoo.url, "json2", KEY, "erp-json2")
        with self.assertRaisesRegex(
            ConfigError,
            r"^Profile 'erp-json2' has `api = json2`, and execute_kw sends "
            r"XML-RPC's positional arguments: give them by name to Client\.call$",
        ):
            _execute_kw(client, "res.partner", "search", [[]])
        self.assertEqual(odoo.requests, [])

    def test_a_method_of_the_model_gets_no_positional_argument(self):
        # fields_get has no leading parameter that goes positionally.
        self.odoo.answers[("res.partner", "fields_get")] = {}
        self.client.call("res.partner", "fields_get", attributes=["string"])
        self.assertEqual(self.odoo.calls[0]["args"], [])
        self.assertEqual(self.odoo.calls[0]["kwargs"], {"attributes": ["string"]})

    def test_the_leading_arguments_of_the_orm_methods_go_positionally(self):
        # Odoo calls the database's last override, whose names can differ
        # from the base's (on 16, res.company.write takes values), and older
        # bases use other names than 19's (args, fields_list): the ids first,
        # for a method of records, then the leading arguments; every other
        # one by name.
        domain = [["is_company", "=", True]]
        calls = {
            "write": (([7],), {"vals": {"name": "Example"}}),
            "read": (([7],), {"fields": ["name"], "load": "_classic_read"}),
            "search": ((), {"domain": domain, "offset": 5, "limit": 10, "order": "name"}),
            "search_count": ((), {"domain": domain}),
            "search_read": (
                (),
                {"domain": domain, "fields": ["name"], "limit": 10, "context": {"lang": "ca_ES"}},
            ),
            "name_search": ((), {"name": "Ex", "domain": domain, "operator": "=ilike", "limit": 5}),
            "default_get": ((), {"fields": ["name"], "context": {"lang": "ca_ES"}}),
        }
        sent = {
            "write": ([[7], {"name": "Example"}], {}),
            "read": ([[7], ["name"]], {"load": "_classic_read"}),
            "search": ([domain], {"offset": 5, "limit": 10, "order": "name"}),
            "search_count": ([domain], {}),
            "search_read": ([domain, ["name"]], {"limit": 10, "context": {"lang": "ca_ES"}}),
            "name_search": (["Ex", domain], {"operator": "=ilike", "limit": 5}),
            "default_get": ([["name"]], {"context": {"lang": "ca_ES"}}),
        }
        for method, (ids, named) in calls.items():
            with self.subTest(method=method):
                self.odoo.answers[("res.partner", method)] = True
                self.client.call("res.partner", method, *ids, **named)
                call = self.odoo.calls[-1]
                self.assertEqual((call["args"], call["kwargs"]), sent[method])

    def test_fields_given_without_a_domain_come_after_an_empty_one(self):
        self.odoo.answers[("res.partner", "search_read")] = []
        self.client.call("res.partner", "search_read", fields=["name"], limit=5)
        self.assertEqual(
            (self.odoo.calls[0]["args"], self.odoo.calls[0]["kwargs"]),
            ([[], ["name"]], {"limit": 5}),
        )

    def test_a_domain_given_without_fields_goes_alone(self):
        self.odoo.answers[("res.partner", "search_read")] = []
        self.client.call("res.partner", "search_read", domain=[["id", "=", 7]])
        self.assertEqual(
            (self.odoo.calls[0]["args"], self.odoo.calls[0]["kwargs"]),
            ([[["id", "=", 7]]], {}),
        )

    def test_a_domain_given_without_a_name_comes_after_an_empty_name(self):
        self.odoo.answers[("res.partner", "name_search")] = []
        self.client.call("res.partner", "name_search", domain=[["id", "=", 7]], limit=8)
        self.assertEqual(
            (self.odoo.calls[0]["args"], self.odoo.calls[0]["kwargs"]),
            (["", [["id", "=", 7]]], {"limit": 8}),
        )

    def test_an_orm_method_called_against_its_kind_goes_as_it_is_written(self):
        # write without ids, search with ids: Odoo refuses either, and the
        # client does not guess which argument is which.
        self.odoo.answers[("res.partner", "write")] = True
        self.odoo.answers[("res.partner", "search")] = []
        self.client.call("res.partner", "write", vals={"name": "Example"})
        self.client.call("res.partner", "search", [7], domain=[])
        self.assertEqual(
            [(call["args"], call["kwargs"]) for call in self.odoo.calls],
            [([], {"vals": {"name": "Example"}}), ([[7]], {"domain": []})],
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

    def test_create_gets_its_vals_list_as_the_positional_argument(self):
        # Odoo's call_kw reads create's values back from its first
        # positional argument: given by name, the call fails once the
        # records are made, and rolls back.
        self.odoo.answers[("res.partner", "create")] = [9]
        result = self.client.call(
            "res.partner", "create", vals_list=[{"name": "Example"}], context={"lang": "ca_ES"}
        )
        self.assertEqual(result, [9])
        self.assertEqual(
            self.odoo.calls,
            [
                {
                    "model": "res.partner",
                    "method": "create",
                    "args": [[{"name": "Example"}]],
                    "kwargs": {"context": {"lang": "ca_ES"}},
                }
            ],
        )

    def test_create_with_ids_goes_as_it_is_written(self):
        # create takes no ids: the call goes as written, for Odoo to refuse,
        # as JSON-2 answers 422 for ids on a method of the model.
        self.odoo.answers[("res.partner", "create")] = [9]
        self.client.call("res.partner", "create", [7], vals_list=[{"name": "Example"}])
        self.assertEqual(
            (self.odoo.calls[0]["args"], self.odoo.calls[0]["kwargs"]),
            ([[7]], {"vals_list": [{"name": "Example"}]}),
        )

    def test_a_call_sends_the_bytes_xmlrpc_client_writes_for_it(self):
        # The call's values are marshalled once, and sent after the
        # credentials: the request is the one xmlrpc.client writes for the
        # whole call, characters outside UTF-8 written as references.
        odoo = self.start(RawXmlRpc())
        odoo.call_answer = Answer(200, xmlrpc_answer("<boolean>1</boolean>"))
        client = self.make_client(odoo.url, "xmlrpc", PASSWORD)
        vals = {"name": "Café <&> \ud800", "parent_id": None, "tag_ids": [[6, 0, [1]]]}
        client.call("res.partner", "write", [7], vals=vals, context={"lang": "ca_ES"})
        self.assertEqual(
            odoo.requests[1]["body"],
            xmlrpc.client.dumps(
                (
                    DB,
                    UID,
                    PASSWORD,
                    "res.partner",
                    "write",
                    [[7], vals],
                    {"context": {"lang": "ca_ES"}},
                ),
                "execute_kw",
                allow_none=True,
            ).encode("utf-8", "xmlcharrefreplace"),
        )

    def test_an_https_url_goes_over_tls(self):
        # The stub speaks plain HTTP: over TLS its answer is no handshake.
        client = self.make_client(
            self.odoo.url.replace("http://", "https://"), "xmlrpc", PASSWORD, "https"
        )
        error = self.raised(
            RequestError, lambda: client.call("res.partner", "search_count", domain=[])
        )
        self.assertIsInstance(error, ssl.SSLError)
        self.assertEqual(self.odoo.logins, 0)

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

    def test_a_fault_code_that_is_no_int_is_its_text_masked(self):
        # Odoo's codes are ints, which stay ints (above). The decoder takes
        # any value, which can quote the password like the message.
        codes = {
            "a text": (f"denied for {PASSWORD}", "denied for ***"),
            "a list": ([f"denied for {PASSWORD}"], "['denied for ***']"),
        }
        for case, (code, masked) in codes.items():
            with self.subTest(case=case):
                self.odoo.answers[("res.partner", "unlink")] = xmlrpc.client.Fault(
                    code, "Access denied"
                )
                error = self.raised(
                    OdooError, lambda: self.client.call("res.partner", "unlink", [7])
                )
                self.assertEqual(error.code, masked)
                self.assertNeverShows(error, PASSWORD)
                self.assertNoFrameHolds(error, PASSWORD)

    def test_a_fault_whose_code_or_text_is_base64_hides_the_password(self):
        # Odoo writes texts, in UTF-8; a base64 value is bytes, which
        # xmlrpc.client gives as a Binary, whose str() reads them as Latin-1.
        password = "odoo_dummy_pàss_test"
        self.odoo.password = password
        cases = {
            "UTF-8": (password.encode("utf-8"), "denied for ***"),
            "Latin-1": (password.encode("latin-1"), "denied for ***"),
        }
        client = self.make_client(self.odoo.url, "xmlrpc", password, "binary")
        for case, (written, masked) in cases.items():
            with self.subTest(case=case):
                text = xmlrpc.client.Binary(b"denied for " + written)
                self.odoo.answers[("res.partner", "unlink")] = xmlrpc.client.Fault(text, text)
                error = self.raised(
                    OdooError, lambda: client.call("res.partner", "unlink", [7])
                )
                self.assertEqual((error.code, error.message), (masked, masked))
                self.assertNeverShows(error, password)
                self.assertNotIn(password.encode("utf-8").decode("latin-1"), str(error))

    def test_a_fault_nested_deeper_than_str_follows_raises_nothing_over_it(self):
        # The decoder reads an array of arrays at any depth, as a Fault's
        # code or text too, and str() of one nested deeper than it follows
        # raised RecursionError in the block that handles the Fault, the
        # Fault as its context, whose frames hold the password. How deep
        # str() follows differs from one Python to the next: under 1,000
        # deep on 3.11, past 64,000 on 3.14.2. The value is nested deeper
        # than str() follows on the Python that runs the test.
        depth = _deeper_than_str_follows()
        nested = (
            "<value><array><data>" * depth
            + "<value><int>1</int></value>"
            + "</data></array></value>" * depth
        )
        # The code and the message each case's error carries.
        written = "<a list str() could not write: RecursionError>"
        cases = {
            "its text": ("<value><int>1</int></value>", nested, (1, written)),
            "its code": (nested, "<value><string>denied</string></value>", (written, "denied")),
        }
        for case, (code, text, carried) in cases.items():
            with self.subTest(case=case):
                stub = self.start(RawXmlRpc())
                stub.call_answer = Answer(
                    200,
                    (
                        "<?xml version='1.0'?><methodResponse><fault><value><struct>"
                        f"<member><name>faultCode</name>{code}</member>"
                        f"<member><name>faultString</name>{text}</member>"
                        "</struct></value></fault></methodResponse>"
                    ).encode(),
                    {"Content-Type": "text/xml"},
                )
                client = self.make_client(stub.url, "xmlrpc", PASSWORD, "nested")
                error = self.raised(OdooError, lambda: client.call("res.partner", "read", [7]))
                self.assertEqual((error.code, error.message), carried)
                self.assertNeverShows(error, PASSWORD)
                self.assertNoFrameHolds(error, PASSWORD)

    def test_a_fault_quoting_the_password_escaped_hides_it_too(self):
        # Odoo writes a value in an error as Python's repr does, and JSON as
        # json.dumps does: such a password differs in each.
        password = 'odoo_dummy"pass\\word_é_test'
        self.odoo.password = password
        self.odoo.answers[("res.partner", "write")] = xmlrpc.client.Fault(
            2,
            "Wrong values: "
            + ", ".join(
                (
                    repr(password),
                    ascii(password),
                    json.dumps(password),
                    json.dumps(password, ensure_ascii=False),
                )
            ),
        )
        client = self.make_client(self.odoo.url, "xmlrpc", password, "escaped")
        error = self.raised(OdooError, lambda: client.call("res.partner", "write", [7], vals={}))
        self.assertEqual(error.message, "Wrong values: '***', '***', \"***\", \"***\"")

    def test_a_longer_form_of_the_password_is_masked_whole(self):
        # The password as it is starts its repr form, one backslash longer:
        # whichever form matches, its trailing run takes the whole run.
        password = "odoo_dummy_pass_test\\"
        self.odoo.password = password
        self.odoo.answers[("res.partner", "write")] = xmlrpc.client.Fault(
            2, f"Wrong value: {password!r}"
        )
        client = self.make_client(self.odoo.url, "xmlrpc", password, "backslash")
        error = self.raised(OdooError, lambda: client.call("res.partner", "write", [7], vals={}))
        self.assertEqual(error.message, "Wrong value: '***'")

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

    def test_a_login_answering_no_uid_a_call_can_send_is_a_request_error(self):
        # Each call sends the uid back: xmlrpc.client refuses to marshal an
        # int beyond 32 bits, from frames that hold the password.
        uids = {
            "an int beyond 32 bits": "<int>2147483648</int>",
            "a negative int": "<int>-7</int>",
            "a boolean": "<boolean>1</boolean>",
            "a string": "<string>7</string>",
            # No frame of the error holds the answer (assertIsAnswerError).
            "a string echoing the password": f"<string>echo {PASSWORD}</string>",
        }
        odoo = self.start(RawXmlRpc())
        for number, (case, uid) in enumerate(uids.items()):
            with self.subTest(case=case):
                odoo.login_answer = Answer(200, xmlrpc_answer(uid))
                client = self.make_client(odoo.url, "xmlrpc", PASSWORD, f"uid-{number}")
                self.assertIsAnswerError(
                    self.raised(
                        RequestError,
                        lambda: client.call("res.partner", "search_count", domain=[]),
                    )
                )
        self.assertEqual(
            [request["path"] for request in odoo.requests],
            ["/xmlrpc/2/common"] * len(uids),
        )

    def test_server_proxy_raises_an_answer_that_is_not_xml_rpc_as_xmlrpc_client(self):
        # As in 1.x (README): here, the UnicodeEncodeError of base64 outside
        # ASCII, which Client's parser turns into a ResponseError.
        odoo = self.start(RawXmlRpc())
        odoo.call_answer = self.ANSWERS["UnicodeEncodeError: base64 outside ASCII"]
        self.make_profile(odoo.url, "xmlrpc", PASSWORD)
        with ServerProxy(profile="erp") as proxy:
            error = self.raised(
                UnicodeEncodeError, lambda: proxy.execute_kw("res.partner", "search", [[]])
            )
        self.assertNotIsInstance(error, RequestError)

    def test_a_login_answering_the_largest_uid_lets_the_call_go(self):
        odoo = self.start(RawXmlRpc())
        odoo.login_answer = Answer(200, xmlrpc_answer("<int>2147483647</int>"))
        odoo.call_answer = Answer(200, xmlrpc_answer("<int>3</int>"))
        client = self.make_client(odoo.url, "xmlrpc", PASSWORD)
        self.assertEqual(client.call("res.partner", "search_count", domain=[]), 3)


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

    def test_the_leading_arguments_of_the_orm_methods_go_by_name(self):
        # Odoo 19 binds the body to the names of the method's signature
        # (addons/rpc/controllers/json2.py, signature.bind): no client can
        # send them otherwise. These are the eight methods whose leading
        # arguments go positionally over XML-RPC; only the ids are added.
        domain = [["is_company", "=", True]]
        calls = {
            "write": (([7],), {"vals": {"name": "Example"}}),
            "read": (([7],), {"fields": ["name"], "load": "_classic_read"}),
            "create": ((), {"vals_list": [{"name": "Example"}]}),
            "search": ((), {"domain": domain, "offset": 5, "limit": 10, "order": "name"}),
            "search_count": ((), {"domain": domain}),
            "search_read": ((), {"fields": ["name"], "limit": 5}),
            "name_search": ((), {"name": "Ex", "domain": domain, "operator": "=ilike"}),
            "default_get": ((), {"fields": ["name"], "context": {"lang": "ca_ES"}}),
        }
        sent = {
            "write": {"ids": [7], "vals": {"name": "Example"}},
            "read": {"ids": [7], "fields": ["name"], "load": "_classic_read"},
            "create": {"vals_list": [{"name": "Example"}]},
            "search": {"domain": domain, "offset": 5, "limit": 10, "order": "name"},
            "search_count": {"domain": domain},
            "search_read": {"fields": ["name"], "limit": 5},
            "name_search": {"name": "Ex", "domain": domain, "operator": "=ilike"},
            "default_get": {"fields": ["name"], "context": {"lang": "ca_ES"}},
        }
        for number, (method, (ids, named)) in enumerate(calls.items()):
            with self.subTest(method=method):
                self.odoo.answers[("res.partner", method)] = []
                self.client.call("res.partner", method, *ids, **named)
                self.assertEqual(self.sent_body(number), sent[method])

    def test_create_gets_its_vals_list_by_name(self):
        # The same call as over XML-RPC, where vals_list goes positionally.
        self.odoo.answers[("res.partner", "create")] = [9]
        result = self.client.call("res.partner", "create", vals_list=[{"name": "Example"}])
        self.assertEqual(result, [9])
        self.assertEqual(self.sent_body(), {"vals_list": [{"name": "Example"}]})

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
        # The answer of a redirect it refuses is closed, its socket with it.
        self.fail_on_resource_warnings()
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
                with self.assertRaises(ValueError) as caught:
                    self.client.call(name, "read", [7])
                self.assertIsInstance(caught.exception, odoo_xmlrpc_auth.client.ArgumentError)
                with self.assertRaises(ValueError):
                    self.client.call("res.partner", name, [7])
        self.assertEqual(self.odoo.requests, [])

    def test_ids_that_are_not_a_list_are_refused_before_sending(self):
        with self.assertRaisesRegex(TypeError, "ids must be None") as caught:
            self.client.call("res.partner", "read", 7)
        self.assertIsInstance(caught.exception, odoo_xmlrpc_auth.client.ArgumentError)
        self.assertEqual(self.odoo.requests, [])


def _nested(depth):
    """A list nested ``depth`` deep, deeper than an encoder's recursion."""
    value = []
    for _ in range(depth):
        value = [value]
    return value


def _deepest(encode):
    """The deepest nesting of a list that ``encode`` writes, called from
    here: a call encodes from a few frames deeper, so its own limit is
    within a few levels of it, on any version of Python."""
    deepest, refused = 1, 2
    while _encodes(encode, refused):
        deepest, refused = refused, refused * 2
    while refused - deepest > 1:
        middle = (deepest + refused) // 2
        if _encodes(encode, middle):
            deepest = middle
        else:
            refused = middle
    return deepest


def _encodes(encode, depth):
    """Whether ``encode`` writes a list nested ``depth`` deep."""
    try:
        encode(_nested(depth))
    except RecursionError:
        return False
    return True


def _deeper_than_str_follows():
    """A depth of lists nested in lists that str() cannot write on the
    running Python: the first of 1,000, 2,000, 4,000 and so on at which it
    raises RecursionError, called from here. A call writes from a few
    frames deeper, where str() follows no deeper."""
    depth = 1_000
    while _encodes(str, depth):
        depth *= 2
    return depth


class ArgumentTest(StubTestCase):
    """A value the profile's API cannot carry is refused before anything is
    sent, the login included: an ArgumentError, also an instance of the
    error the encoder raised, with the encoder's text, and no frame of it
    holds the password or the key."""

    def assertRefusedUnsent(self, client, password, error_class, value):
        error = self.raised(
            error_class, lambda: client.call("res.partner", "write", [7], vals={"x": value})
        )
        self.assertNoFrameHolds(error, password)
        self.assertNeverShows(error, password)
        self.assertIsInstance(error, odoo_xmlrpc_auth.client.ArgumentError)
        self.assertTrue(str(error).startswith("A call of profile 'erp' cannot be sent: "))

    def test_a_value_xmlrpc_cannot_marshal_is_refused_unsent(self):
        odoo = self.start(XmlRpcOdoo())
        client = self.make_client(odoo.url, "xmlrpc", PASSWORD)
        holds_itself = []
        holds_itself.append(holds_itself)
        values = {
            "a set": (TypeError, {1, 2}),
            "a date": (TypeError, datetime.date(2026, 1, 1)),
            "a dict keyed by an int": (TypeError, {1: "one"}),
            "a list that holds itself": (TypeError, holds_itself),
            "an int beyond 32 bits": (OverflowError, 2**40),
            "a list nested 2000 deep": (RecursionError, _nested(2000)),
        }
        for case, (error_class, value) in values.items():
            with self.subTest(case=case):
                self.assertRefusedUnsent(client, PASSWORD, error_class, value)
        self.assertEqual((odoo.logins, odoo.calls), (0, []))

    def assertSentOrRefused(self, client, around, sent):
        """Each call of ``client`` with a value nested from 40 levels under
        ``around`` to 40 over it is sent, which ``sent()`` counts, or
        refused with ArgumentError, and never raises another error; both
        happen."""
        outcomes = set()
        for depth in range(around - 40, around + 40):
            with self.subTest(depth=depth):
                try:
                    client.call("res.partner", "write", [7], vals={"x": _nested(depth)})
                    outcomes.add("sent")
                except ArgumentError:
                    outcomes.add("refused")
        self.assertEqual(outcomes, {"sent", "refused"})
        self.assertGreater(sent(), 0)

    def test_a_value_nested_near_the_marshallers_limit_is_sent_or_refused(self):
        # The call is marshalled once: what the check lets through is what
        # is sent, never marshalled again from a deeper stack, where it
        # would raise a RecursionError from frames that hold the password.
        odoo = self.start(RawXmlRpc())
        odoo.call_answer = Answer(200, xmlrpc_answer("<boolean>1</boolean>"))
        client = self.make_client(odoo.url, "xmlrpc", PASSWORD)
        around = _deepest(lambda value: xmlrpc.client.dumps((value,), allow_none=True))
        self.assertSentOrRefused(
            client,
            around,
            lambda: [r["path"] for r in odoo.requests].count("/xmlrpc/2/object"),
        )

    def test_the_request_carries_the_text_of_the_one_marshal_of_the_call(self):
        # The very text the check's marshal wrote, not one written again:
        # a second marshal of the call's values, from frames that hold the
        # password, fails near the limit at some depths of the caller's
        # stack only, which the test above cannot pin at every depth. Every
        # marshal goes through Marshaller.dumps, xmlrpc.client.dumps's too:
        # the call's values go through it once, wherever a second one would
        # be, and the request carries what that one wrote.
        odoo = self.start(XmlRpcOdoo())
        odoo.answers[("res.partner", "write")] = True
        client = self.make_client(odoo.url, "xmlrpc", PASSWORD)
        module = odoo_xmlrpc_auth.client
        written, carried = [], []
        dumps, request = xmlrpc.client.Marshaller.dumps, module._execute_kw_request

        def recording_dumps(marshaller, values):
            written.append(dumps(marshaller, values))
            return written[-1]

        def recording_request(db, uid, password, params):
            carried.append(params)
            return request(db, uid, password, params)

        with mock.patch.object(xmlrpc.client.Marshaller, "dumps", recording_dumps):
            with mock.patch.object(module, "_execute_kw_request", recording_request):
                client.call("res.partner", "write", [7], vals={"name": "marshalled_once"})
        # The stub's answers, the login and the credentials are marshalled
        # too: only the call's values hold its name.
        calls = [text for text in written if "marshalled_once" in text]
        self.assertEqual((len(calls), len(carried)), (1, 1))
        self.assertIn(carried[0], calls[0])

    def test_a_model_or_a_method_that_is_no_string_utf8_encodes_is_refused_unsent(self):
        # JSON-2 quoted it into the url, raising a TypeError or a
        # UnicodeEncodeError, and XML-RPC sent it to the server.
        xmlrpc_odoo = self.start(XmlRpcOdoo())
        json2_odoo = self.start(Json2Odoo())
        clients = {
            "xmlrpc": self.make_client(xmlrpc_odoo.url, "xmlrpc", PASSWORD, "erp-xmlrpc"),
            "json2": self.make_client(json2_odoo.url, "json2", KEY, "erp-json2"),
        }
        names = {
            "None": (TypeError, None),
            "an int": (TypeError, 5),
            "a lone surrogate": (ValueError, "res.partner\ud800"),
        }
        for api, client in clients.items():
            for case, (error_class, name) in names.items():
                for model, method in ((name, "read"), ("res.partner", name)):
                    with self.subTest(api=api, case=case, model=model):
                        error = self.raised(
                            error_class, lambda: client.call(model, method, [7])
                        )
                        self.assertIsInstance(error, ArgumentError)
                        self.assertNeverShows(error, "\ud800")
        for case, (error_class, name) in names.items():
            with self.subTest(case=case, form="execute_kw"):
                error = self.raised(
                    error_class,
                    lambda: _execute_kw(clients["xmlrpc"], name, "read", [[7]]),
                )
                self.assertIsInstance(error, ArgumentError)
        self.assertEqual(
            (xmlrpc_odoo.logins, xmlrpc_odoo.calls, json2_odoo.requests), (0, [], [])
        )

    def test_the_marshallers_text_is_the_reason(self):
        odoo = self.start(XmlRpcOdoo())
        client = self.make_client(odoo.url, "xmlrpc", PASSWORD)
        error = self.raised(
            OverflowError, lambda: client.call("res.partner", "read", [2**40])
        )
        self.assertEqual(
            str(error), "A call of profile 'erp' cannot be sent: int exceeds XML-RPC limits"
        )
        self.assertEqual(error.reason, "int exceeds XML-RPC limits")

    def test_a_value_json_cannot_hold_is_refused_unsent(self):
        odoo = self.start(Json2Odoo())
        client = self.make_client(odoo.url, "json2", KEY)
        holds_itself = {}
        holds_itself["self"] = holds_itself
        values = {
            "a set": (TypeError, {1, 2}),
            "NaN": (ValueError, float("nan")),
            "an infinity": (ValueError, float("inf")),
            "a dict that holds itself": (ValueError, holds_itself),
            # Deeper than json's C encoder follows on every version: from
            # Python 3.12 it counts its own recursion, not Python's.
            "a list nested 100000 deep": (RecursionError, _nested(100_000)),
        }
        for case, (error_class, value) in values.items():
            with self.subTest(case=case):
                self.assertRefusedUnsent(client, KEY, error_class, value)
        self.assertEqual(odoo.requests, [])

    def test_a_value_nested_near_jsons_limit_is_sent_or_refused(self):
        # The body is encoded once, and sent as it is.
        odoo = self.start(Json2Odoo())
        odoo.answers[("res.partner", "write")] = True
        client = self.make_client(odoo.url, "json2", KEY)
        around = _deepest(lambda value: json.dumps(value, allow_nan=False))
        self.assertSentOrRefused(client, around, lambda: len(odoo.requests))

    def test_the_reason_hides_the_key(self):
        # json's text names the type of a value it cannot hold, and a type's
        # name can be anything: here, the key.
        odoo = self.start(Json2Odoo())
        client = self.make_client(odoo.url, "json2", KEY)
        value = type(KEY, (), {})()
        error = self.raised(
            TypeError, lambda: client.call("res.partner", "write", [7], vals={"x": value})
        )
        self.assertEqual(error.reason, "Object of type *** is not JSON serializable")
        self.assertNeverShows(error, KEY)


class ResourceWarningsCheckTest(unittest.TestCase):
    """What the check of a test that may leave no socket or file open
    (StubTestCase.fail_on_resource_warnings) leaves once that test ends."""

    def test_a_failed_collection_puts_the_hook_and_the_filters_back(self):
        # The check's collection of garbage (collect_garbage) fails once it
        # has waited 5 s for one in this thread. Here it fails at once, in a
        # test that arms the check, run whole, so that its cleanups run while
        # this test looks on.
        hook = sys.unraisablehook
        filters = list(warnings.filters)

        class ArmsTheCheck(StubTestCase):
            def runTest(self):
                self.fail_on_resource_warnings()

        failed_wait = AssertionError("waited 5 s for a collection of garbage in this thread")
        result = unittest.TestResult()
        with mock.patch(f"{__name__}.collect_garbage", side_effect=failed_wait):
            ArmsTheCheck().run(result)
        self.assertEqual(result.errors, [])
        (failure,) = result.failures
        self.assertIn(
            "AssertionError: waited 5 s for a collection of garbage in this thread", failure[1]
        )
        self.assertIs(sys.unraisablehook, hook)
        self.assertEqual(warnings.filters, filters)


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

    def test_no_frame_of_an_answer_refused_over_json2_holds_the_key(self):
        # The answer's request carries the key in its headers, and Odoo's
        # error body can echo it, unmasked.
        answers = {
            "an error of Odoo echoing the key": (
                OdooError,
                Answer(422, odoo_error("odoo.exceptions.UserError", f"Not {KEY}")),
            ),
            "a 401": (AuthError, Answer(401, b"")),
            "an error that is not Odoo's": (RequestError, Answer(502, b"Bad gateway")),
            "a 200 that is not JSON": (RequestError, Answer(200, b"<html>Log in</html>")),
        }
        odoo = self.start(Json2Odoo())
        client = self.make_client(odoo.url, "json2", KEY)
        for case, (error_class, answer) in answers.items():
            with self.subTest(case=case):
                odoo.answers[("res.partner", "search_count")] = answer
                error = self.raised(
                    error_class,
                    lambda: client.call("res.partner", "search_count", domain=[]),
                )
                self.assertNoFrameHolds(error, KEY)


# How long, in seconds, a stub holds a request at most before it lets the
# connection go: far above the timeouts the tests give Client (0.2 s), far
# below the 120 s the mutant runner waits for the suite. A test whose
# timeout does not fire then fails in seconds, on the error of a connection
# let go, instead of waiting forever.
HOLD = 10


def close_after_hold(test, closable):
    """Close ``closable``, a socket of ``test``, after HOLD seconds, unless
    the test ends first."""
    timer = threading.Timer(HOLD, closable.close)
    timer.daemon = True
    timer.start()
    test.addCleanup(timer.cancel)


class HeldXmlRpcCall(RawXmlRpc):
    """RawXmlRpc that answers the login and holds the call, unanswered,
    until the test releases it, or HOLD seconds have passed."""

    def __init__(self):
        self.released = threading.Event()
        super().__init__()

    def answer(self, request):
        if request["path"] == "/xmlrpc/2/common":
            answer = self.login_answer
        else:
            self.released.wait(HOLD)
            answer = None
        return answer


class HalfAnswer:
    """A server that reads one request, sends a 200 whose body stops after
    its first byte, and holds the rest until it stops, or HOLD seconds
    have passed."""

    def __init__(self):
        self.released = threading.Event()
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(1)
        self.url = f"http://127.0.0.1:{self.listener.getsockname()[1]}"
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()

    def serve(self):
        connection, _ = self.listener.accept()
        with connection:
            connection.recv(65536)
            connection.sendall(
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                b"Content-Length: 2\r\n\r\n{"
            )
            self.released.wait(HOLD)

    def stop(self):
        self.released.set()
        self.thread.join(5)
        self.listener.close()


class TimeoutTest(StubTestCase):
    """Each request of Client, the login's too, has the connect timeout to
    connect, then the read timeout for its answer: 30 s and 600 s, or the
    pair given at its creation. When one fires, the RequestError says which,
    and after the read timeout that the call may still complete. ServerProxy
    sets none, as before 2.0."""

    def held(self):
        """The url of a server whose system takes each connection, and which
        never reads a request nor answers it, for HOLD seconds: its listener
        closes then, and the system resets the connections it took."""
        listener = socket.socket()
        self.addCleanup(listener.close)
        listener.bind(("127.0.0.1", 0))
        listener.listen(8)
        close_after_hold(self, listener)
        return f"http://127.0.0.1:{listener.getsockname()[1]}"

    def full(self):
        """The url of a server whose queue of connections is full, so that a
        new connection never opens: Linux drops its SYN. For HOLD seconds:
        its listener closes then, and the next SYN is refused."""
        listener = socket.socket()
        self.addCleanup(listener.close)
        listener.bind(("127.0.0.1", 0))
        listener.listen(0)
        filler = socket.create_connection(listener.getsockname())
        self.addCleanup(filler.close)
        close_after_hold(self, listener)
        return f"http://127.0.0.1:{listener.getsockname()[1]}"

    def test_each_request_has_30_s_to_connect_and_600_s_for_its_answer_by_default(self):
        xmlrpc_odoo = self.start(XmlRpcOdoo())
        xmlrpc_odoo.answers[("res.partner", "search_count")] = 3
        json2_odoo = self.start(Json2Odoo())
        json2_odoo.answers[("res.partner", "search_count")] = 3
        # The XML-RPC stub closes the connection after each answer: the login
        # and the call each open one.
        cases = {
            "xmlrpc": (
                self.make_client(xmlrpc_odoo.url, "xmlrpc", PASSWORD, "erp-xmlrpc"),
                [30, 600, 30, 600],
            ),
            "json2": (
                self.make_client(json2_odoo.url, "json2", KEY, "erp-json2"),
                [30, 600],
            ),
        }
        timeouts = self.record_timeouts()
        for api, (client, expected) in cases.items():
            with self.subTest(api=api):
                timeouts.clear()
                self.assertEqual(client.call("res.partner", "search_count", domain=[]), 3)
                self.assertEqual(timeouts, expected)

    def test_a_client_takes_its_two_timeouts_at_creation(self):
        xmlrpc_odoo = self.start(XmlRpcOdoo())
        xmlrpc_odoo.answers[("res.partner", "search_count")] = 3
        json2_odoo = self.start(Json2Odoo())
        json2_odoo.answers[("res.partner", "search_count")] = 3
        self.make_profile(xmlrpc_odoo.url, "xmlrpc", PASSWORD, "erp-xmlrpc")
        self.make_profile(json2_odoo.url, "json2", KEY, "erp-json2")
        cases = {"erp-xmlrpc": [2, 9.5, 2, 9.5], "erp-json2": [2, 9.5]}
        timeouts = self.record_timeouts()
        for profile, expected in cases.items():
            with self.subTest(profile=profile):
                timeouts.clear()
                with Client(profile, timeout=(2, 9.5)) as client:
                    client.call("res.partner", "search_count", domain=[])
                self.assertEqual(timeouts, expected)

    def test_a_client_refuses_a_timeout_that_is_not_two_positive_numbers(self):
        # No profile "erp" exists: each is refused before the profile is read.
        cases = {
            "one number": (30, TypeError),
            "a list": ([30, 600], TypeError),
            "one member": ((30,), TypeError),
            "three members": ((30, 600, 5), TypeError),
            "a string": ((30, "600"), TypeError),
            "a bool": ((True, 600), TypeError),
            "None": (None, TypeError),
            "zero": ((0, 600), ValueError),
            "negative": ((30, -1), ValueError),
            "NaN": ((30, float("nan")), ValueError),
            "infinite": ((30, float("inf")), ValueError),
            "beyond what a socket takes": ((30, 1e10), ValueError),
        }
        for case, (timeout, error_class) in cases.items():
            with self.subTest(case=case):
                error = self.raised(
                    ArgumentError, lambda: Client("erp", timeout=timeout)
                )
                self.assertIsInstance(error, error_class)
                self.assertIn("timeout", str(error))

    def test_a_client_takes_the_largest_timeout_a_socket_takes(self):
        odoo = self.start(Json2Odoo())
        odoo.answers[("res.partner", "search_count")] = 3
        self.make_profile(odoo.url, "json2", KEY)
        largest = threading.TIMEOUT_MAX
        timeouts = self.record_timeouts()
        with Client("erp", timeout=(largest, largest)) as client:
            self.assertEqual(client.call("res.partner", "search_count", domain=[]), 3)
        self.assertEqual(timeouts, [largest, largest])

    def test_a_connect_timeout_says_it_fired_before_anything_was_sent(self):
        # Every socket's own timeout ends the wait should a connection get
        # none: Linux resends a dropped SYN for about two minutes.
        self.addCleanup(socket.setdefaulttimeout, socket.getdefaulttimeout())
        socket.setdefaulttimeout(5)
        url = self.full()
        cases = {
            "xmlrpc": (PASSWORD, socket.timeout, "TimeoutError"),
            "json2": (KEY, requests.ConnectTimeout, "ConnectTimeout"),
        }
        for api, (secret, original, name) in cases.items():
            with self.subTest(api=api):
                self.make_profile(url, api, secret, f"erp-{api}")
                with Client(f"erp-{api}", timeout=(0.2, 5)) as client:
                    error = self.raised(
                        RequestError,
                        lambda: client.call("res.partner", "search_count", domain=[]),
                    )
                self.assertIsInstance(error, original)
                self.assertEqual(error.timeout_fired, "connect")
                self.assertEqual(
                    str(error),
                    f"A request of profile 'erp-{api}' failed: {name}. The connect "
                    "timeout fired, before anything was sent. Its message is not "
                    "shown, as it can quote the profile's values.",
                )
                self.assertNeverShows(error, secret)
                self.assertNoFrameHolds(error, secret)

    def test_an_xmlrpc_tls_handshake_is_under_the_connect_timeout(self):
        # Every socket's own timeout ends the wait should the handshake get
        # none: the timeouts recorded then tell.
        self.addCleanup(socket.setdefaulttimeout, socket.getdefaulttimeout())
        socket.setdefaulttimeout(5)
        self.make_profile(self.held().replace("http://", "https://"), "xmlrpc", PASSWORD)
        timeouts = self.record_timeouts()
        with Client("erp", timeout=(0.2, 9)) as client:
            error = self.raised(
                RequestError, lambda: client.call("res.partner", "search_count", domain=[])
            )
        self.assertEqual(error.timeout_fired, "connect")
        # The TLS socket takes over the timeout of the socket it wraps, and
        # sets it again once its handshake ends (ssl.SSLSocket.do_handshake).
        self.assertEqual(timeouts, [0.2, 0.2, 0.2])

    def test_an_xmlrpc_https_connection_reads_under_the_read_timeout(self):
        # The transport gives the connection both timeouts as it builds it,
        # before it connects: no TLS server is needed, and no socket is
        # opened. The TCP socket and the TLS socket are fakes; the context
        # that wraps one into the other is the one the connection makes.
        tcp_socket = mock.create_autospec(socket.socket, instance=True)
        tls_socket = mock.create_autospec(ssl.SSLSocket, instance=True)
        with mock.patch("socket.create_connection", return_value=tcp_socket) as connect:
            with mock.patch.object(ssl.SSLContext, "wrap_socket", return_value=tls_socket) as wrap:
                _SafeTransport((2, 9.5)).make_connection("127.0.0.1").connect()
        connect.assert_called_once_with(("127.0.0.1", 443), 2, None)
        wrap.assert_called_once_with(tcp_socket, server_hostname="127.0.0.1")
        tls_socket.settimeout.assert_called_once_with(9.5)

    def test_a_read_timeout_says_the_server_may_still_complete_the_call(self):
        held_call = self.start(HeldXmlRpcCall())
        self.addCleanup(held_call.released.set)
        held = self.held()
        cases = {
            "the xmlrpc login": (held, "xmlrpc", PASSWORD, socket.timeout, "TimeoutError"),
            "an xmlrpc call": (held_call.url, "xmlrpc", PASSWORD, socket.timeout, "TimeoutError"),
            "a json2 call": (held, "json2", KEY, requests.ReadTimeout, "ReadTimeout"),
        }
        for case, (url, api, secret, original, name) in cases.items():
            with self.subTest(case=case):
                self.make_profile(url, api, secret)
                with Client("erp", timeout=(5, 0.2)) as client:
                    error = self.raised(
                        RequestError,
                        lambda: client.call("res.partner", "search_count", domain=[]),
                    )
                self.assertIsInstance(error, original)
                self.assertEqual(error.timeout_fired, "read")
                self.assertEqual(
                    str(error),
                    f"A request of profile 'erp' failed: {name}. The read timeout "
                    "fired: the server may still complete the call, so do not "
                    "repeat it before reading back what it did. Its message is "
                    "not shown, as it can quote the profile's values.",
                )
                self.assertNeverShows(error, secret)
                self.assertNoFrameHolds(error, secret)
        self.assertEqual(
            [request["path"] for request in held_call.requests],
            ["/xmlrpc/2/common", "/xmlrpc/2/object"],
        )

    def test_a_json2_answer_whose_body_stops_coming_is_a_read_timeout(self):
        half = self.start(HalfAnswer())
        self.make_profile(half.url, "json2", KEY)
        with Client("erp", timeout=(5, 0.2)) as client:
            error = self.raised(
                RequestError, lambda: client.call("res.partner", "search_count", domain=[])
            )
        self.assertIsInstance(error, requests.ConnectionError)
        self.assertEqual(error.timeout_fired, "read")
        self.assertIn("The read timeout fired", str(error))
        self.assertNeverShows(error, KEY)
        self.assertNoFrameHolds(error, KEY)

    def test_server_proxy_sets_no_timeout(self):
        odoo = self.start(XmlRpcOdoo())
        odoo.answers[("res.partner", "search_count")] = 3
        self.make_profile(odoo.url, "xmlrpc", PASSWORD)
        timeouts = self.record_timeouts()
        with ServerProxy(profile="erp") as proxy:
            self.assertEqual(proxy.execute_kw("res.partner", "search_count", [[]], {}), 3)
        self.assertEqual(timeouts, [])

    def test_a_timeout_of_server_proxy_is_none_of_clients(self):
        # Sockets get this timeout when nothing sets theirs: a script can set
        # it for ServerProxy, whose error then reads as before 2.0.
        self.addCleanup(socket.setdefaulttimeout, socket.getdefaulttimeout())
        socket.setdefaulttimeout(0.2)
        self.make_profile(self.held(), "xmlrpc", PASSWORD)
        error = self.raised(RequestError, lambda: ServerProxy(profile="erp"))
        self.assertIsInstance(error, socket.timeout)
        self.assertIsNone(error.timeout_fired)
        self.assertEqual(
            str(error),
            "A request of profile 'erp' failed: TimeoutError. Its message is not "
            "shown, as it can quote the profile's values.",
        )


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

    def test_the_cli_calls_under_the_clients_default_timeouts(self):
        self.xmlrpc_odoo.answers[("res.partner", "search_count")] = 3
        self.json2_odoo.answers[("res.partner", "search_count")] = 3
        # The XML-RPC stub closes the connection after each answer: the login
        # and the call each open one.
        cases = {"erp-xmlrpc": [30, 600, 30, 600], "erp-json2": [30, 600]}
        timeouts = self.record_timeouts()
        for profile, expected in cases.items():
            with self.subTest(profile=profile):
                timeouts.clear()
                result = self.run_cli(
                    profile, {"model": "res.partner", "method": "search_count",
                              "kwargs": {"domain": []}}
                )
                self.assertEqual(result, (0, "3\n", ""))
                self.assertEqual(timeouts, expected)

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
        # search_count's domain goes positionally over XML-RPC.
        self.assertEqual(self.xmlrpc_odoo.calls[0]["args"], [[]])
        self.assertEqual(self.xmlrpc_odoo.calls[0]["kwargs"], {})

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

    def test_the_args_form_goes_through_the_client_and_reads_the_profile_once(self):
        self.xmlrpc_odoo.answers[("res.partner", "read")] = [{"id": 7}]
        with mock.patch.object(
            odoo_xmlrpc_auth.client, "_load_config", wraps=odoo_xmlrpc_auth.client._load_config
        ) as load:
            code, _, _ = self.run_cli(
                "erp-xmlrpc", {"model": "res.partner", "method": "read", "args": [[7]]}
            )
        self.assertEqual((code, load.call_count), (0, 1))

    def test_an_odoo_error_on_the_args_form_hides_the_password(self):
        self.xmlrpc_odoo.answers[("res.partner", "unlink")] = xmlrpc.client.Fault(
            4, f"Access denied for {PASSWORD} on res.partner"
        )
        code, out, err = self.run_cli(
            "erp-xmlrpc", {"model": "res.partner", "method": "unlink", "args": [[7]]}
        )
        self.assertEqual((code, out), (1, ""))
        self.assertEqual(
            err,
            "Error: Odoo answered a call of profile 'erp-xmlrpc' with an error "
            "(code 4): Access denied for *** on res.partner\n",
        )

    def test_the_args_form_passes_its_kwargs_as_they_are(self):
        # ids in kwargs is the named form's refusal only: the form before 2.0
        # gives execute_kw its kwargs as 1.x did.
        self.xmlrpc_odoo.answers[("res.partner", "read")] = [{"id": 7}]
        code, _, _ = self.run_cli(
            "erp-xmlrpc",
            {"model": "res.partner", "method": "read", "args": [[7]], "kwargs": {"ids": [8]}},
        )
        self.assertEqual(code, 0)
        self.assertEqual(
            (self.xmlrpc_odoo.calls[0]["args"], self.xmlrpc_odoo.calls[0]["kwargs"]),
            ([[7]], {"ids": [8]}),
        )

    def test_kwargs_holding_ids_are_refused_before_any_call(self):
        # The records go in the payload's ids, as Client.call takes them.
        payload = {"model": "res.partner", "method": "read", "kwargs": {"ids": [7]}}
        for profile in ("erp-xmlrpc", "erp-json2"):
            with self.subTest(profile=profile):
                code, out, err = self.run_cli(profile, payload)
                self.assertEqual(
                    (code, out, err),
                    (1, "", "Error: 'ids' goes at the top of the JSON payload, not in 'kwargs'\n"),
                )
        self.assertEqual((self.xmlrpc_odoo.logins, self.json2_odoo.requests), (0, []))

    def test_the_args_form_is_refused_on_a_json2_profile(self):
        code, out, err = self.run_cli(
            "erp-json2", {"model": "res.partner", "method": "read", "args": [[7]]}
        )
        self.assertEqual((code, out), (1, ""))
        self.assertEqual(
            err,
            "Error: Profile 'erp-json2' has `api = json2`, and execute_kw sends "
            "XML-RPC's positional arguments: give them by name to Client.call\n",
        )
        self.assertEqual(self.json2_odoo.requests, [])

    def test_kwargs_left_out_or_null_are_no_named_argument_in_either_form(self):
        self.xmlrpc_odoo.answers[("res.partner", "search_count")] = 3
        self.json2_odoo.answers[("res.partner", "search_count")] = 3
        call = {"model": "res.partner", "method": "search_count"}
        payloads = [
            ("erp-xmlrpc", {**call, "kwargs": None}),
            ("erp-json2", {**call, "kwargs": None}),
            ("erp-xmlrpc", {**call, "args": [[]], "kwargs": None}),
            ("erp-xmlrpc", {**call, "args": [[]]}),
        ]
        for profile, payload in payloads:
            with self.subTest(profile=profile, payload=payload):
                self.assertEqual(self.run_cli(profile, payload), (0, "3\n", ""))
        self.assertEqual(
            [(sent["args"], sent["kwargs"]) for sent in self.xmlrpc_odoo.calls],
            [([], {}), ([[]], {}), ([[]], {})],
        )
        self.assertEqual(json.loads(self.json2_odoo.requests[0]["body"]), {})

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

    def test_a_payload_that_is_not_a_call_is_refused_before_its_profile_is_read(self):
        # The payload's message, also for a profile that does not exist.
        call = {"model": "res.partner", "method": "read"}
        refused = {
            "[]": "the JSON payload must be an object",
            json.dumps({"model": "res.partner"}): "missing 'method' in JSON payload",
            json.dumps({**call, "kwarg": {}}): "unknown keys in the JSON payload: kwarg",
            json.dumps({**call, "ids": [7], "args": [[7]]}): (
                "give 'ids' or 'args' in the JSON payload, not both"
            ),
            json.dumps({**call, "kwargs": []}): "'kwargs' in the JSON payload must be an object",
            json.dumps({**call, "kwargs": {"ids": [7]}}): (
                "'ids' goes at the top of the JSON payload, not in 'kwargs'"
            ),
        }
        for payload, message in refused.items():
            with self.subTest(payload=payload):
                for profile in ("erp-xmlrpc", "erp-json2", "no-such-profile"):
                    code, out, err = self.run_cli(profile, payload)
                    self.assertEqual((code, out, err), (1, "", f"Error: {message}\n"))
        self.assertEqual((self.xmlrpc_odoo.logins, self.json2_odoo.requests), (0, []))

    def test_json_python_cannot_read_ends_as_one_message(self):
        # Errors of json other than a JSONDecodeError. Python refuses an int
        # longer than 4300 digits from 3.10.7 on and converts it before, so
        # json's ValueError for it, as 3.11 words it, is raised here.
        too_long = ValueError(
            "Exceeds the limit (4300 digits) for integer string conversion: value "
            "has 5000 digits; use sys.set_int_max_str_digits() to increase the limit"
        )
        with mock.patch("json.loads", side_effect=too_long):
            result = self.run_cli(
                "erp-xmlrpc",
                '{"model": "res.partner", "method": "read", "ids": [' + "1" * 5000 + "]}",
            )
        self.assertEqual(
            result,
            (
                1,
                "",
                "Error: invalid JSON: Exceeds the limit (4300 digits) for integer "
                "string conversion: value has 5000 digits; use "
                "sys.set_int_max_str_digits() to increase the limit\n",
            ),
        )
        # An array nested deeper than json's parser follows, on any version.
        code, out, err = self.run_cli("erp-xmlrpc", "[" * 100000 + "]" * 100000)
        self.assertEqual((code, out), (1, ""))
        self.assertTrue(err.startswith("Error: invalid JSON: "))
        self.assertEqual(err.count("\n"), 1)
        self.assertEqual(self.xmlrpc_odoo.logins, 0)

    def test_a_value_json2_cannot_send_ends_as_one_message(self):
        # NaN is no JSON, but Python's json reads it; JSON-2 cannot send it.
        # From Python 3.13 json's text names the value too.
        code, out, err = self.run_cli(
            "erp-json2",
            '{"model": "res.partner", "method": "search_read", "kwargs": {"limit": NaN}}',
        )
        self.assertEqual((code, out), (1, ""))
        self.assertRegex(
            err,
            r"\AError: A call of profile 'erp-json2' cannot be sent: Out of range "
            r"float values are not JSON compliant(: nan)?\n\Z",
        )
        self.assertEqual(self.json2_odoo.requests, [])

    def test_a_call_client_refuses_ends_as_one_message(self):
        refusals = [
            (
                "erp-xmlrpc",
                {"model": "", "method": "read", "ids": [7]},
                "A model and a method are names: neither can be empty, '.' or '..'",
            ),
            (
                "erp-json2",
                {"model": "res.partner", "method": "..", "ids": [7]},
                "A model and a method are names: neither can be empty, '.' or '..'",
            ),
            (
                "erp-json2",
                {"model": "res.partner", "method": "read\ud800", "ids": [7]},
                "A model and a method are names: neither can hold a character "
                "UTF-8 cannot encode, such as a lone surrogate",
            ),
            (
                "erp-xmlrpc",
                {"model": "res.partner\ud800", "method": "read", "args": [[7]]},
                "A model and a method are names: neither can hold a character "
                "UTF-8 cannot encode, such as a lone surrogate",
            ),
            (
                "erp-xmlrpc",
                {"model": "res.partner", "method": "read", "ids": [2**40]},
                "int exceeds XML-RPC limits",
            ),
            (
                "erp-xmlrpc",
                {"model": "res.partner", "method": "read", "args": [[2**40]]},
                "int exceeds XML-RPC limits",
            ),
            (
                "erp-xmlrpc",
                {"model": 5, "method": "read", "ids": [7]},
                "A model and a method are names: both must be strings",
            ),
            (
                "erp-json2",
                {"model": "res.partner", "method": ["read"]},
                "A model and a method are names: both must be strings",
            ),
            (
                "erp-xmlrpc",
                {"model": "res.partner", "method": None, "args": [[7]]},
                "A model and a method are names: both must be strings",
            ),
            (
                "erp-xmlrpc",
                {"model": "res.partner", "method": "read", "ids": 7},
                "ids must be None, for a method of the model, or a list of the "
                "records' ids",
            ),
            (
                "erp-json2",
                {"model": "res.partner", "method": "read", "ids": {"id": 7}},
                "ids must be None, for a method of the model, or a list of the "
                "records' ids",
            ),
            (
                "erp-xmlrpc",
                {"model": "res.partner", "method": "read", "args": {"ids": [7]}},
                "args must be the list of execute_kw's positional arguments",
            ),
            (
                "erp-xmlrpc",
                {"model": "res.partner", "method": "read", "args": [[7]], "kwargs": []},
                "kwargs must be None or the dict of execute_kw's named arguments",
            ),
        ]
        for profile, payload, reason in refusals:
            with self.subTest(profile=profile, payload=payload):
                code, out, err = self.run_cli(profile, payload)
                self.assertEqual(
                    (code, out, err),
                    (1, "", f"Error: A call of profile '{profile}' cannot be sent: {reason}\n"),
                )
        self.assertEqual((self.xmlrpc_odoo.logins, self.json2_odoo.requests), (0, []))


if __name__ == "__main__":
    unittest.main()
