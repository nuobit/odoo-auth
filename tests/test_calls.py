"""Tests of Client over both APIs, against stub servers on 127.0.0.1.

XmlRpcOdoo and Json2Odoo (support.py) stand for Odoo: they answer from a
table each test fills and record what they get, so that a test checks what
the client sent, and where. Nothing reaches a real server or a real store.

Run from the repository root:
    PYTHONPATH=src python3 -m unittest discover -s tests
"""

import contextlib
import datetime
import errno
import gc
import gzip
import hashlib
import http.client
import io
import json
import logging
import os
import socket
import ssl
import struct
import sys
import threading
import time
import unittest
import uuid
import warnings
import xmlrpc.client
import zlib
from unittest import mock

import requests
import urllib3

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
    KeepAliveOdoo,
    RawAnswer,
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

    def record_sockets(self, module):
        """The list where each socket that ``module``'s create_connection
        opens, from now to the end of the test, is recorded in order: the
        connections of a Client's transport, real ones, over XML-RPC with
        socket's, over JSON-2 with urllib3.util.connection's."""
        opened = []
        create = module.create_connection

        def recording(*args, **kwargs):
            opened.append(create(*args, **kwargs))
            return opened[-1]

        patcher = mock.patch.object(module, "create_connection", recording)
        patcher.start()
        self.addCleanup(patcher.stop)
        return opened

    def wait_for_heads(self):
        """From now to the end of the test, each connection of http.client,
        urllib3's among them, waits up to HOLD seconds for the head of an
        answer, then reads its body under the timeout its socket had: a read
        timeout far shorter than HOLD fires only where a body stops coming,
        however late a stub on a loaded machine starts to answer."""
        getresponse = http.client.HTTPConnection.getresponse

        def waiting(connection):
            sock = connection.sock
            timeout = sock.gettimeout()
            sock.settimeout(HOLD)
            try:
                return getresponse(connection)
            finally:
                # The body is read on that socket, also once http.client let
                # it go for an answer that closes the connection.
                sock.settimeout(timeout)

        patcher = mock.patch.object(http.client.HTTPConnection, "getresponse", waiting)
        patcher.start()
        self.addCleanup(patcher.stop)

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

    @contextlib.contextmanager
    def json2_https_client(self, *handshakes):
        """A Client over JSON-2 to https://127.0.0.1, offline, for the with
        block: each connection's TCP socket is a fake, and its TLS handshake
        gives the next of ``handshakes``, a fake TLS socket, or raises it, an
        error. Every connection stops at its fake TCP socket, none looks a
        name up, and the block uses every handshake."""
        tcp_sockets = [mock.create_autospec(socket.socket, instance=True) for _ in handshakes]
        with mock.patch(
            "urllib3.util.connection.create_connection", side_effect=tcp_sockets
        ) as connect:
            with mock.patch.object(ssl.SSLContext, "wrap_socket", side_effect=handshakes):
                yield self.make_client("https://127.0.0.1", "json2", KEY)
        self.assertEqual(connect.call_count, len(handshakes))


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
    """An answer that cannot be read as XML-RPC, to the login or to a call,
    is a RequestError that stands for xmlrpc.client's ResponseError,
    whatever reading or parsing it raised; no frame of it holds the
    password. A call's has code 200, the status it came with, and its
    message says that the server may have run the call; the login's has
    none, as no call was sent."""

    # Each answer, after what xmlrpc.client raises reading it.
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
        "IncompleteRead: a body cut short": RawAnswer(
            b"HTTP/1.1 200 OK\r\nContent-Type: text/xml\r\nContent-Length: 400\r\n\r\n"
            b"<?xml version='1.0'?><methodResponse>"
        ),
        "IncompleteRead: a body sent in bad chunks": RawAnswer(
            b"HTTP/1.1 200 OK\r\nContent-Type: text/xml\r\nTransfer-Encoding: chunked\r\n\r\n"
            b"zz\r\n<?xml version='1.0'?>\r\n0\r\n\r\n"
        ),
        "BadGzipFile: a body that is not the gzip it says": Answer(
            200, xmlrpc_answer("<int>7</int>"), {"Content-Encoding": "gzip"}
        ),
    }

    def assertIsAnswerError(self, error, code):
        self.assertIsInstance(error, xmlrpc.client.ResponseError)
        self.assertEqual((error.error_class, error.code), ("ResponseError", code))
        self.assertNeverShows(error, PASSWORD)
        self.assertNoFrameHolds(error, PASSWORD)

    def test_an_answer_to_a_call_that_cannot_be_read_says_the_call_may_have_run(self):
        # The answer read in part is closed as the call ends, its socket
        # with it, not left to the garbage collector.
        opened = self.record_sockets(socket)
        odoo = self.start(RawXmlRpc())
        client = self.make_client(odoo.url, "xmlrpc", PASSWORD)
        for case, answer in self.ANSWERS.items():
            with self.subTest(case=case):
                odoo.call_answer = answer
                error = self.raised(
                    RequestError,
                    lambda: client.call("res.partner", "search_count", domain=[]),
                )
                self.assertEqual([sock for sock in opened if sock.fileno() != -1], [])
                self.assertIsAnswerError(error, 200)
                self.assertEqual(
                    str(error),
                    "A request of profile 'erp' failed: ResponseError, code 200. The "
                    "server answered: it may have run the call, so do not repeat it "
                    "before reading back what it did. Its message is not shown, as it "
                    "can quote the profile's values.",
                )

    def test_a_call_whose_answer_breaks_off_is_sent_once(self):
        # A connection reset as the answer comes, after the server ran the
        # call: an answer that cannot be read, sent once, where
        # xmlrpc.client sends a request again on a reset
        # (Transport.request). Reading the answer's body raises it here, as
        # a reset socket does. The answer read in part is closed as the call
        # ends, its socket with it, not left to the garbage collector.
        opened = self.record_sockets(socket)
        odoo = self.start(RawXmlRpc())
        odoo.call_answer = Answer(200, xmlrpc_answer("<int>3</int>"))
        client = self.make_client(odoo.url, "xmlrpc", PASSWORD)
        self.assertEqual(client.call("res.partner", "search_count", domain=[]), 3)
        reset = ConnectionResetError(errno.ECONNRESET, "Connection reset by peer")
        with mock.patch.object(http.client.HTTPResponse, "read", side_effect=reset):
            error = self.raised(
                RequestError, lambda: client.call("res.partner", "search_count", domain=[])
            )
        self.assertEqual([sock for sock in opened if sock.fileno() != -1], [])
        self.assertIsAnswerError(error, 200)
        self.assertEqual(
            [request["path"] for request in odoo.requests],
            ["/xmlrpc/2/common", "/xmlrpc/2/object", "/xmlrpc/2/object"],
        )

    def test_an_answer_to_the_login_that_cannot_be_read_is_a_request_error(self):
        odoo = self.start(RawXmlRpc())
        for number, (case, answer) in enumerate(self.ANSWERS.items()):
            with self.subTest(case=case):
                odoo.login_answer = answer
                client = self.make_client(odoo.url, "xmlrpc", PASSWORD, f"login-{number}")
                error = self.raised(
                    RequestError,
                    lambda: client.call("res.partner", "search_count", domain=[]),
                )
                self.assertIsAnswerError(error, None)
                self.assertNotIn("The server answered", str(error))
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
                    ),
                    None,
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

    def made_logger(self, name, logger_class=logging.Logger):
        """The new logger ``name``, of ``logger_class``, a subclass of
        logging.Logger, made as logging makes any logger. Once the test ends,
        whatever its outcome, what the making added to logging's loggers is
        taken out: each name it added, the logger's and the placeholder of
        each parent name that had none, and the logger from the placeholder
        of each parent name that had one. Every logger a test makes is made
        here."""
        manager = logging.Logger.manager
        before = dict(manager.loggerDict)
        previous = manager.loggerClass
        manager.setLoggerClass(logger_class)
        try:
            logger = logging.getLogger(name)
        finally:
            manager.loggerClass = previous
        added = [key for key in manager.loggerDict if key not in before]
        holders = [
            holder
            for holder in before.values()
            if isinstance(holder, logging.PlaceHolder) and logger in holder.loggerMap
        ]

        def take_out():
            # A test may have dropped the logger itself.
            for key in added:
                manager.loggerDict.pop(key, None)
            for holder in holders:
                del holder.loggerMap[logger]

        self.addCleanup(take_out)
        return logger

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

    def test_a_200_whose_body_cannot_be_read_says_the_call_may_have_run(self):
        # Odoo answers 200 once the call ran: a retry would run it again. The
        # answer read in part is closed as the call ends, its socket with it,
        # not left to the garbage collector.
        answers = {
            "not JSON": (
                "JSONDecodeError",
                Answer(200, f"<html>Log in {KEY}</html>".encode(), {"Content-Type": "text/html"}),
            ),
            "cut short": (
                "ChunkedEncodingError",
                RawAnswer(
                    b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                    b"Content-Length: 400\r\n\r\n3"
                ),
            ),
            "sent in bad chunks": (
                "ChunkedEncodingError",
                RawAnswer(
                    b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                    b"Transfer-Encoding: chunked\r\n\r\nzz\r\n3\r\n0\r\n\r\n"
                ),
            ),
            # Over HTTP/1.1, which keeps the connection, and longer than one
            # read of requests (10 KiB): urllib3 decodes the first read, which
            # is not gzip, before the rest has come, and leaves the
            # connection open.
            "not the gzip it says": (
                "ContentDecodingError",
                RawAnswer(
                    b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                    b"Content-Encoding: gzip\r\nContent-Length: 20003\r\n\r\n[3]"
                    + b" " * 20_000
                ),
            ),
        }
        send = requests.Session.send
        responses = []

        def recording_send(session, request, **kwargs):
            responses.append(send(session, request, **kwargs))
            return responses[-1]

        for case, (error_class, answer) in answers.items():
            with self.subTest(case=case):
                self.odoo.answers[("res.partner", "write")] = answer
                with mock.patch.object(requests.Session, "send", recording_send):
                    error = self.raised(
                        RequestError,
                        lambda: self.client.call("res.partner", "write", [7], vals={}),
                    )
                self.assertTrue(responses[-1].raw.closed)
                self.assertEqual((error.error_class, error.code), (error_class, 200))
                self.assertIn(
                    "The server answered: it may have run the call, so do not repeat "
                    "it before reading back what it did.",
                    str(error),
                )
                self.assertNeverShows(error, KEY)
                self.assertNoFrameHolds(error, KEY)

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
                # The call went out, and the server answered: no connection
                # failed.
                self.assertEqual(
                    str(caught.exception),
                    f"A request of profile 'erp' failed: RedirectError, code {status}. "
                    "Its message is not shown, as it can quote the profile's values.",
                )
                self.assertIsNone(caught.exception.request)
                self.assertIsNone(caught.exception.response)
                self.assertNeverShows(caught.exception, KEY)
        self.assertEqual(elsewhere.requests, [])

    def test_an_error_of_urllib3_that_requests_lets_through_is_a_request_error(self):
        # requests raises as it is an error of urllib3 its adapter does not
        # know (HTTPAdapter.send), from frames that hold the key: under
        # requests 2.34.2 and urllib3 2.3.0, the LocationParseError of a
        # redirect to <origin>//x:y. urllib3's pool raises each here, on any
        # version, the others with an __init__ or a repr of their own.
        errors = {
            "LocationParseError": urllib3.exceptions.LocationParseError("//x:y"),
            "HeaderParsingError": urllib3.exceptions.HeaderParsingError([], b"X-Echo"),
            "IncompleteRead": urllib3.exceptions.IncompleteRead(1, 399),
        }
        for name, raised in errors.items():
            with self.subTest(error=name):
                with mock.patch.object(urllib3.HTTPConnectionPool, "urlopen", side_effect=raised):
                    error = self.raised(
                        RequestError, lambda: self.client.call("res.partner", "read", [7])
                    )
                self.assertIsInstance(error, type(raised))
                self.assertEqual((error.error_class, error.code), (name, None))
                self.assertNeverShows(error, KEY)
                self.assertNoFrameHolds(error, KEY)
        self.assertEqual(self.odoo.requests, [])

    def test_urllib3_logs_nothing_of_an_answer_the_client_reads(self):
        # urllib3 warns of headers it cannot parse, quoting them as the
        # server sent them: here, a header whose name holds the key. An
        # application that configured logging prints every warning; the one
        # of another connection still reaches it.
        logged = io.StringIO()
        handler = logging.StreamHandler(logged)
        logging.getLogger().addHandler(handler)
        self.addCleanup(logging.getLogger().removeHandler, handler)
        self.odoo.answers[("res.partner", "search_count")] = Answer(
            200, 3, {f"X-Echo {KEY}": "echo"}
        )
        self.assertEqual(self.client.call("res.partner", "search_count", domain=[]), 3)
        self.assertEqual(logged.getvalue(), "")
        with urllib3.HTTPConnectionPool("127.0.0.1", self.odoo.server.server_address[1]) as pool:
            pool.request(
                "POST",
                "/json/2/res.partner/search_count",
                headers={"Authorization": f"Bearer {KEY}"},
            )
        self.assertIn("Failed to parse headers", logged.getvalue())

    def test_no_logger_of_urllib3_logs_a_record_of_a_call(self):
        # At DEBUG, urllib3's pool logs each request line: here, that of a
        # redirect within the origin whose location echoes the key. A filter
        # set on urllib3's own logger sees none of its children's records.
        # The records of another connection still reach the application.
        logged = io.StringIO()
        handler = logging.StreamHandler(logged)
        logging.getLogger().addHandler(handler)
        self.addCleanup(logging.getLogger().removeHandler, handler)
        urllib3_logger = logging.getLogger("urllib3")
        self.addCleanup(urllib3_logger.setLevel, urllib3_logger.level)
        urllib3_logger.setLevel(logging.DEBUG)
        self.odoo.answers[("res.partner", "read")] = Answer(
            307, b"", {"Location": f"/json/2/res.partner/{KEY}"}
        )
        self.odoo.answers[("res.partner", KEY)] = [{"id": 7}]
        self.assertEqual(self.client.call("res.partner", "read", [7]), [{"id": 7}])
        self.assertEqual(self.odoo.requests[1]["path"], f"/json/2/res.partner/{KEY}")
        self.assertEqual(logged.getvalue(), "")
        with urllib3.HTTPConnectionPool("127.0.0.1", self.odoo.server.server_address[1]) as pool:
            pool.request(
                "POST", f"/json/2/res.partner/{KEY}", headers={"Authorization": f"Bearer {KEY}"}
            )
        self.assertIn(f'"POST /json/2/res.partner/{KEY} HTTP/1.1" 200', logged.getvalue())

    def test_a_logger_urllib3_makes_once_the_library_loaded_logs_nothing_of_a_call(self):
        # Such as the logger of a module of urllib3's contrib imported later:
        # here, one made by the test, through which urllib3's pool logs.
        logged = io.StringIO()
        handler = logging.StreamHandler(logged)
        logging.getLogger().addHandler(handler)
        self.addCleanup(logging.getLogger().removeHandler, handler)
        later = self.made_logger("urllib3.contrib.made_by_a_test")
        self.addCleanup(later.setLevel, later.level)
        later.setLevel(logging.DEBUG)
        self.odoo.answers[("res.partner", "search_count")] = 3
        with mock.patch.object(urllib3.connectionpool, "log", later):
            self.assertEqual(self.client.call("res.partner", "search_count", domain=[]), 3)
        self.assertEqual(logged.getvalue(), "")
        later.debug("a record of no call")
        self.assertEqual(logged.getvalue(), "a record of no call\n")

    def test_what_making_a_logger_adds_goes_as_the_test_ends_whatever_its_outcome(self):
        # The helper every logger of a test is made through (made_logger), in
        # a test that fails. Before it, a logger whose name made placeholders
        # for its parents, family and family.kept. In it, a logger whose name
        # needs a placeholder of its parent, family.new, and one that the
        # placeholder of family.kept holds too, as does family's.
        manager = logging.Logger.manager
        family = f"made_by_a_test_{uuid.uuid4().hex}"
        self.made_logger(f"{family}.kept.before")
        before = dict(manager.loggerDict)
        held_before = {
            name: dict(holder.loggerMap)
            for name, holder in before.items()
            if isinstance(holder, logging.PlaceHolder)
        }

        class FailingTest(unittest.TestCase):
            made_logger = Json2ClientTest.made_logger

            def test_it(self):
                self.made_logger(f"{family}.new.one")
                self.made_logger(f"{family}.kept.two")
                self.fail("a test that fails")

        outcome = unittest.TestResult()
        FailingTest("test_it").run(outcome)
        self.assertEqual(len(outcome.failures), 1)
        self.assertEqual(manager.loggerDict, before)
        self.assertEqual(
            {
                name: dict(holder.loggerMap)
                for name, holder in manager.loggerDict.items()
                if isinstance(holder, logging.PlaceHolder)
            },
            held_before,
        )

    def test_a_logger_made_between_two_steps_of_the_walk_breaks_nothing(self):
        # Each call over JSON-2 first walks logging's loggers. Code that runs
        # meanwhile, a thread or a signal's handler, can make a logger
        # between two steps of the walk: walking logging's dict itself, the
        # walk then raised RuntimeError on any Python, the dict having grown
        # under it. Here a logger of urllib3 makes one as the walk gives it
        # the filter, and the walk goes on to the logger of urllib3 made
        # after it.
        made = []
        test = self

        class MakesALogger(logging.Logger):
            def addFilter(self, filter):
                super().addFilter(filter)
                made.append(test.made_logger(f"made_by_a_test.{uuid.uuid4().hex}"))

        self.made_logger(f"urllib3.contrib.made_by_a_test_{uuid.uuid4().hex}", MakesALogger)
        after = self.made_logger(f"urllib3.contrib.made_by_a_test_{uuid.uuid4().hex}")
        odoo_xmlrpc_auth.client._filter_urllib3_loggers()
        self.assertEqual(len(made), 1)
        self.assertEqual(after.filters, [odoo_xmlrpc_auth.client._JSON2_CALL_RECORDS])

    def test_a_logger_dropped_between_two_steps_of_the_walk_breaks_nothing(self):
        # The test above, for a logger dropped: walking logging's dict
        # itself, the walk raised RuntimeError on any Python, the dict having
        # shrunk under it.
        doomed = self.made_logger(f"made_by_a_test.{uuid.uuid4().hex}")
        dropped = []

        class DropsALogger(logging.Logger):
            def addFilter(self, filter):
                super().addFilter(filter)
                dropped.append(logging.Logger.manager.loggerDict.pop(doomed.name))

        self.made_logger(f"urllib3.contrib.made_by_a_test_{uuid.uuid4().hex}", DropsALogger)
        after = self.made_logger(f"urllib3.contrib.made_by_a_test_{uuid.uuid4().hex}")
        odoo_xmlrpc_auth.client._filter_urllib3_loggers()
        self.assertEqual(dropped, [doomed])
        self.assertEqual(after.filters, [odoo_xmlrpc_auth.client._JSON2_CALL_RECORDS])

    @unittest.skipIf(
        sys.version_info >= (3, 12),
        "from Python 3.12, no collection runs inside a call of C such as the walk's",
    )
    def test_a_logger_made_during_the_walk_over_the_loggers_breaks_nothing(self):
        # Python 3.11 can collect garbage as an object is made inside a call
        # of C, and run a finalizer there, which can make a logger, as a
        # thread that runs meanwhile can. Each call over JSON-2 walks
        # logging's loggers first: taken one by one from logging's dict, which
        # then changed size, the walk raised RuntimeError. Here each object
        # made runs a collection, and each collection the finalizer of an
        # object in a cycle, which makes a logger and the next such object.
        made = []
        armed = threading.Event()
        test = self

        class MakesALogger:
            def __del__(self):
                if armed.is_set():
                    # A name no logger has yet: logging's dict grows.
                    made.append(test.made_logger(f"made_by_a_test.{uuid.uuid4().hex}"))
                    plant()

        def plant():
            garbage = MakesALogger()
            garbage.itself = garbage

        unfiltered = self.made_logger("urllib3.contrib.made_by_a_test_before_the_walk")
        threshold = gc.get_threshold()
        gc.collect()
        armed.set()
        gc.set_threshold(1)
        plant()
        try:
            odoo_xmlrpc_auth.client._filter_urllib3_loggers()
        finally:
            gc.set_threshold(*threshold)
            armed.clear()
        self.assertNotEqual(made, [])
        self.assertEqual(unfiltered.filters, [odoo_xmlrpc_auth.client._JSON2_CALL_RECORDS])

    @unittest.skipIf(
        sys.version_info >= (3, 12),
        "from Python 3.12, no collection runs inside a call of C such as the copy's",
    )
    def test_a_logger_dropped_as_the_loggers_are_copied_breaks_nothing(self):
        # Python 3.11's dict.copy makes the new dict after it took the
        # entries of logging's dict and before it reads their count: a
        # collection of garbage that the allocation runs there, whose
        # finalizer drops a logger, as a thread that runs meanwhile can, gave
        # a copy whose count was one entry short, and its walk raised
        # RuntimeError. Here objects in cycles, each of whose finalizers
        # drops a logger the test made, wait in the youngest generation of
        # garbage for the first collection once the walk starts: on Python
        # 3.11 the first object made then runs it.
        names = [f"made_by_a_test.{uuid.uuid4().hex}" for _ in range(200)]
        for name in names:
            self.made_logger(name)
        dropped = []

        class DropsALogger:
            def __init__(self, name):
                self.name = name
                self.itself = self

            def __del__(self):
                dropped.append(logging.Logger.manager.loggerDict.pop(self.name))

        unfiltered = self.made_logger(f"urllib3.contrib.made_by_a_test_{uuid.uuid4().hex}")
        threshold = gc.get_threshold()
        gc.collect()
        for name in names:
            DropsALogger(name)
        gc.set_threshold(1)
        try:
            odoo_xmlrpc_auth.client._filter_urllib3_loggers()
            # Counted before the collection below, which runs every finalizer
            # still waiting: each logger was dropped inside the walk.
            self.assertEqual(len(dropped), 200)
        finally:
            gc.set_threshold(*threshold)
            gc.collect()
        self.assertEqual(unfiltered.filters, [odoo_xmlrpc_auth.client._JSON2_CALL_RECORDS])

    def test_urllib3_logs_nothing_of_an_https_call(self):
        # Offline, the TLS sockets fakes: an answer whose header's name
        # holds the key, read at DEBUG over the call's connection, then over
        # a connection of urllib3's own, which still logs.
        logged = io.StringIO()
        handler = logging.StreamHandler(logged)
        logging.getLogger().addHandler(handler)
        self.addCleanup(logging.getLogger().removeHandler, handler)
        urllib3_logger = logging.getLogger("urllib3")
        self.addCleanup(urllib3_logger.setLevel, urllib3_logger.level)
        urllib3_logger.setLevel(logging.DEBUG)
        answer = (
            b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
            + f"X-Echo {KEY}: echo\r\n".encode()
            + b"Content-Length: 1\r\n\r\n3"
        )
        tls_sockets = [mock.create_autospec(ssl.SSLSocket, instance=True) for _ in range(2)]
        for tls_socket in tls_sockets:
            tls_socket.makefile.return_value = io.BytesIO(answer)
        with self.json2_https_client(*tls_sockets) as client:
            self.assertEqual(client.call("res.partner", "search_count", domain=[]), 3)
            self.assertEqual(logged.getvalue(), "")
            with urllib3.HTTPSConnectionPool("127.0.0.1") as pool:
                pool.request("POST", "/json/2/res.partner/search_count")
        self.assertIn("Failed to parse headers", logged.getvalue())
        self.assertIn(KEY, logged.getvalue())

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

    def redirect_with_body(self, head, body, after=None):
        """Answer the call with a 303 within the origin, on a connection
        that closes after it, whose head ends with ``head``, its last
        headers, and whose body is ``body``, then as RawAnswer does with
        ``after``. The redirect's target answers 3."""
        self.odoo.answers[("res.partner", "write")] = RawAnswer(
            b"HTTP/1.1 303 See Other\r\nLocation: /json/2/res.partner/search_count\r\n"
            b"Connection: close\r\n" + head + b"\r\n" + body,
            after=after,
        )
        self.odoo.answers[("res.partner", "search_count")] = 3

    def test_a_redirects_body_is_read_undecoded(self):
        # Said to be gzip, it decodes for more than the first chunk requests
        # reads, then fails at a block of the reserved type, and more body
        # comes after it. requests read it decoded, then, after the decoding
        # error, the rest undecoded, which urllib3 refused with a RuntimeError
        # that left the call raw, from frames holding the key. Read undecoded
        # first, it is never decoded, and the redirect is followed.
        digits = b"".join(
            hashlib.sha256(str(n).encode()).hexdigest().encode() for n in range(500)
        )
        deflate = zlib.compressobj(9, zlib.DEFLATED, -15)
        body = (
            b"\x1f\x8b\x08\x00\x00\x00\x00\x00\x00\x03"
            + deflate.compress(digits)
            + deflate.flush(zlib.Z_FULL_FLUSH)
            + b"\x07"
            + b"x" * 2000
        )
        self.redirect_with_body(
            b"Content-Encoding: gzip\r\n" + f"Content-Length: {len(body)}\r\n".encode(), body
        )
        self.assertEqual(self.client.call("res.partner", "write", [7], vals={}), 3)
        self.assertEqual([request["method"] for request in self.odoo.requests], ["POST", "GET"])

    def test_a_redirects_body_that_stops_coming_is_a_read_timeout(self):
        # Said to be gzip, its first chunk is not, and the rest stops coming:
        # requests read that rest undecoded, and urllib3's ReadTimeoutError
        # reached the call as it is, saying nothing of a timeout, though the
        # call's request went out, answered by the redirect. The read timeout
        # waits for the redirect's head (wait_for_heads): it fires for the
        # body alone.
        self.fail_on_resource_warnings()
        self.wait_for_heads()
        released = threading.Event()
        self.addCleanup(released.set)
        self.redirect_with_body(
            b"Content-Encoding: gzip\r\nContent-Length: 100000\r\n",
            b"this is not gzip " * 800,
            after=lambda: released.wait(HOLD),
        )
        with Client("erp", timeout=(5, 0.2)) as client:
            error = self.raised(
                RequestError, lambda: client.call("res.partner", "write", [7], vals={})
            )
        self.assertIsInstance(error, requests.ConnectionError)
        self.assertEqual((error.timeout_fired, error.code), ("read", None))
        self.assertEqual(
            str(error),
            "A request of profile 'erp' failed: ConnectionError. The read timeout "
            "fired: the server may still complete the call, so do not repeat it "
            "before reading back what it did. Its message is not shown, as it can "
            "quote the profile's values.",
        )
        self.assertEqual([request["method"] for request in self.odoo.requests], ["POST"])
        self.assertNeverShows(error, KEY)
        self.assertNoFrameHolds(error, KEY)

    def test_a_redirects_body_that_breaks_off_says_the_call_may_have_run(self):
        # Said to be gzip, its first chunk is not, and the connection ends
        # before the rest: urllib3's ProtocolError reached the call as it is,
        # saying nothing of a call cut off.
        self.fail_on_resource_warnings()
        self.redirect_with_body(
            b"Content-Encoding: gzip\r\nContent-Length: 100000\r\n", b"this is not gzip " * 800
        )
        error = self.raised(
            RequestError, lambda: self.client.call("res.partner", "write", [7], vals={})
        )
        self.assertIsInstance(error, requests.exceptions.ChunkedEncodingError)
        self.assertEqual(
            str(error),
            "A request of profile 'erp' failed: ChunkedEncodingError, code 303. It "
            "failed after the call started to go out, before its answer was read "
            "whole: the server may have run the call, so do not repeat it before "
            "reading back what it did. Its message is not shown, as it can quote the "
            "profile's values.",
        )
        self.assertEqual([request["method"] for request in self.odoo.requests], ["POST"])
        self.assertNeverShows(error, KEY)
        self.assertNoFrameHolds(error, KEY)

    def test_a_redirects_body_in_bad_chunks_is_an_error_with_no_note(self):
        # As the body of any answer: the connection brought it whole. A
        # redirect whose body cannot be read is not followed.
        self.redirect_with_body(
            b"Transfer-Encoding: chunked\r\n", b"zz\r\n3\r\n0\r\n\r\n"
        )
        error = self.raised(
            RequestError, lambda: self.client.call("res.partner", "write", [7], vals={})
        )
        self.assertIsInstance(error, requests.exceptions.ChunkedEncodingError)
        self.assertEqual(
            str(error),
            "A request of profile 'erp' failed: ChunkedEncodingError, code 303. Its "
            "message is not shown, as it can quote the profile's values.",
        )
        self.assertEqual([request["method"] for request in self.odoo.requests], ["POST"])
        self.assertNeverShows(error, KEY)
        self.assertNoFrameHolds(error, KEY)

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


class WireDebugTest(StubTestCase):
    """The library's own connections print nothing whatever the process's
    debug level: http.client.HTTPConnection.debuglevel = 1, as requests'
    docs set it to see the wire, would print each request's headers and
    body, the API key or the password among them."""

    def setUp(self):
        super().setUp()
        self.xmlrpc_odoo = self.start(XmlRpcOdoo())
        self.json2_odoo = self.start(Json2Odoo())
        self.xmlrpc_odoo.answers[("res.partner", "search_count")] = 3
        self.json2_odoo.answers[("res.partner", "search_count")] = 3
        patcher = mock.patch.object(http.client.HTTPConnection, "debuglevel", 1)
        patcher.start()
        self.addCleanup(patcher.stop)

    def printed(self, call):
        """What ``call()`` prints on stdout, once it returned 3."""
        printed = io.StringIO()
        with contextlib.redirect_stdout(printed):
            self.assertEqual(call(), 3)
        return printed.getvalue()

    def test_a_call_of_client_over_each_api_prints_nothing(self):
        clients = {
            "xmlrpc": self.make_client(self.xmlrpc_odoo.url, "xmlrpc", PASSWORD, "erp-x"),
            "json2": self.make_client(self.json2_odoo.url, "json2", KEY, "erp-j"),
        }
        for api, client in clients.items():
            with self.subTest(api=api):
                self.assertEqual(
                    self.printed(
                        lambda: client.call("res.partner", "search_count", domain=[])
                    ),
                    "",
                )

    def test_the_connections_of_an_https_profile_print_nothing(self):
        # Real profiles are https. http.client prints what a connection
        # sends and gets when the connection's debug level is above 0. Each
        # connection is built as a call builds it, Client's over each API
        # and ServerProxy's for its login and its calls, and read before it
        # connects, which only sending does: no TLS server is needed.
        url = "https://127.0.0.1"
        xmlrpc_client = self.make_client(url, "xmlrpc", PASSWORD, "https-xmlrpc")
        json2_client = self.make_client(url, "json2", KEY, "https-json2")
        logins = []

        def log_in(profile, config, transport):
            logins.append(transport)
            self.addCleanup(transport.close)
            return UID

        with mock.patch.object(odoo_xmlrpc_auth.client, "_log_in", log_in):
            proxy = ServerProxy(profile="https-xmlrpc")
        self.addCleanup(proxy("close"))
        adapter = json2_client._transport._session.get_adapter(url)
        connections = {
            "Client over XML-RPC": xmlrpc_client._transport._transport.make_connection(
                "127.0.0.1"
            ),
            "Client over JSON-2": adapter.poolmanager.connection_from_url(url)._new_conn(),
            "ServerProxy's login": logins[0].make_connection("127.0.0.1"),
            "ServerProxy's calls": proxy("transport").make_connection("127.0.0.1"),
        }
        for case, connection in connections.items():
            with self.subTest(case=case):
                self.assertEqual(connection.debuglevel, 0)

    def test_a_call_of_server_proxy_with_a_profile_prints_nothing(self):
        self.make_profile(self.xmlrpc_odoo.url, "xmlrpc", PASSWORD)

        def call():
            with ServerProxy(profile="erp") as proxy:
                return proxy.execute_kw("res.partner", "search_count", [[]], {})

        self.assertEqual(self.printed(call), "")

    def test_server_proxy_with_a_profile_takes_xmlrpc_clients_options(self):
        # Its transport is the library's, made with the options given, as
        # xmlrpc.client makes its own.
        odoo = self.start(RawXmlRpc())
        cases = {
            "use_datetime": (
                {"use_datetime": True},
                "<dateTime.iso8601>20260101T00:00:00</dateTime.iso8601>",
                datetime.datetime(2026, 1, 1),
            ),
            "use_builtin_types": (
                {"use_builtin_types": True},
                "<base64>ZHVtbXk=</base64>",
                b"dummy",
            ),
            "headers": ({"headers": [("X-Test", "yes")]}, "<int>3</int>", 3),
        }
        self.make_profile(odoo.url, "xmlrpc", PASSWORD)
        for case, (options, value, expected) in cases.items():
            with self.subTest(case=case):
                odoo.call_answer = Answer(200, xmlrpc_answer(value))
                with ServerProxy(profile="erp", **options) as proxy:
                    answer = proxy.execute_kw("res.partner", "search", [[]])
                # Its type too: xmlrpc.client's DateTime and Binary, read
                # without the option, compare equal to a datetime or bytes.
                self.assertEqual((type(answer), answer), (type(expected), expected))
        self.assertEqual(odoo.requests[-1]["headers"].get("x-test"), "yes")

    def test_server_proxy_refuses_verbose_with_a_profile_before_reading_it(self):
        # verbose asks xmlrpc.client to print the wire, the password among
        # it; nothing of the library is printed. Refused before the store
        # is read, given by name or in its place.
        for args, kwargs in (((), {"verbose": True}), ((None, None, 1), {})):
            with self.subTest(args=args, kwargs=kwargs):
                error = self.raised(
                    ArgumentError,
                    lambda: ServerProxy(None, *args, profile="no-such-profile", **kwargs),
                )
                self.assertIsInstance(error, ValueError)
                self.assertEqual(
                    str(error),
                    "A ServerProxy of profile 'no-such-profile' cannot be created: "
                    "it takes no verbose, which would print each request, the "
                    "password among them",
                )


class ServerProxyTransportTest(StubTestCase):
    """ServerProxy with a profile builds the transport xmlrpc.client's
    ServerProxy builds for itself, from the same arguments, but with the
    library's connections, as in 1.x but for those: a transport it is given
    carries its calls, its https transport takes the context, and allow_none
    is True unless it is given."""

    def setUp(self):
        super().setUp()
        self.odoo = self.start(XmlRpcOdoo())
        self.make_profile(self.odoo.url, "xmlrpc", PASSWORD)

    def test_a_transport_it_is_given_carries_its_calls(self):
        handlers = []

        class Recording(xmlrpc.client.Transport):
            def request(self, host, handler, request_body, verbose=False):
                handlers.append(handler)
                return super().request(host, handler, request_body, verbose)

        transport = Recording()
        self.odoo.answers[("res.partner", "search_count")] = 3
        with ServerProxy(profile="erp", transport=transport) as proxy:
            self.assertIs(proxy("transport"), transport)
            self.assertEqual(proxy.execute_kw("res.partner", "search_count", [[]], {}), 3)
        self.assertEqual(handlers, ["/xmlrpc/2/object"])

    def test_its_https_transport_connects_with_the_context_it_is_given(self):
        # The login is replaced: no TLS server is needed, and nothing is
        # sent, as a connection connects only when it sends.
        self.make_profile("https://127.0.0.1", "xmlrpc", PASSWORD, "https")
        context = ssl.create_default_context()
        with mock.patch.object(odoo_xmlrpc_auth.client, "_log_in", return_value=UID):
            proxy = ServerProxy(profile="https", context=context)
        self.addCleanup(proxy("close"))
        transport = proxy("transport")
        self.assertIs(transport.context, context)
        self.assertIs(transport.make_connection("127.0.0.1")._context, context)

    def test_it_takes_its_uri_by_name_as_xmlrpc_client_does(self):
        # Without a profile exactly like xmlrpc.client's ServerProxy; with
        # one, the profile's url replaces it, given by name or in its place.
        with ServerProxy(uri=f"{self.odoo.url}/xmlrpc/2/common") as proxy:
            self.assertEqual(proxy.authenticate(DB, USER, PASSWORD, {}), UID)
        self.odoo.answers[("res.partner", "search_count")] = 3
        with ServerProxy(uri="http://127.0.0.1:9/xmlrpc/2/object", profile="erp") as proxy:
            self.assertEqual(proxy.execute_kw("res.partner", "search_count", [[]], {}), 3)
        for factory in (xmlrpc.client.ServerProxy, ServerProxy):
            with self.subTest(factory=factory.__module__):
                with self.assertRaisesRegex(
                    TypeError, r"\AServerProxy.__init__\(\) missing 1 required positional "
                    r"argument: 'uri'\Z"
                ):
                    factory()

    def test_it_allows_none_unless_it_is_told_otherwise(self):
        self.odoo.answers[("res.partner", "write")] = True
        with ServerProxy(profile="erp") as proxy:
            self.assertIs(
                proxy.execute_kw("res.partner", "write", [[7], {"name": None}], {}), True
            )
        self.assertEqual(self.odoo.calls[0]["args"], [[7], {"name": None}])
        with ServerProxy(profile="erp", allow_none=False) as proxy:
            with self.assertRaisesRegex(TypeError, "cannot marshal None"):
                proxy.execute_kw("res.partner", "write", [[7], {"name": None}], {})


class KeptConnectionTest(StubTestCase):
    """Client keeps its connection from one call to the next, over both
    APIs, and checks it before a request goes out on it, as urllib3 checks a
    pooled one: a connection the server closed since its last answer carries
    nothing, and the request goes out on a new one. Over XML-RPC, a failure
    of a request, whatever it raises, closes its connection. ServerProxy
    keeps xmlrpc.client's own sending."""

    def test_a_kept_connection_the_server_closed_carries_nothing(self):
        # The login closes its own connection (_log_in). Without the check,
        # the third call goes out on the closed connection, and its end
        # makes xmlrpc.client send it again on a new one.
        opened = self.record_sockets(socket)
        odoo = self.start(KeepAliveOdoo({("call", 2): "answer, then close idle"}))
        client = self.make_client(odoo.url, "xmlrpc", PASSWORD)
        self.assertEqual(client.call("res.partner", "search_count", domain=[]), 3)
        self.assertEqual(client.call("res.partner", "search_count", domain=[]), 3)
        # The calls' connection, the second, once the server's end of it has
        # come: readable, as the check before reuse reads it.
        self.assertTrue(urllib3.util.wait_for_read(opened[1], timeout=5))
        self.assertEqual(client.call("res.partner", "search_count", domain=[]), 3)
        self.assertTrue(odoo.idle_read.wait(5))
        self.assertEqual(odoo.after_close, [])
        self.assertEqual(
            odoo.requests, [(1, "login"), (2, "call"), (2, "call"), (3, "call")]
        )

    def test_a_json2_connection_the_server_closed_carries_nothing(self):
        # urllib3's own check, which JSON-2 has always had.
        opened = self.record_sockets(urllib3.util.connection)
        odoo = self.start(KeepAliveOdoo({("call", 2): "answer, then close idle"}))
        client = self.make_client(odoo.url, "json2", KEY)
        self.assertEqual(client.call("res.partner", "search_count", domain=[]), 3)
        self.assertEqual(client.call("res.partner", "search_count", domain=[]), 3)
        # The calls' connection, once the server's end of it has come:
        # readable, as urllib3's check before reuse reads it.
        self.assertTrue(urllib3.util.wait_for_read(opened[0], timeout=5))
        self.assertEqual(client.call("res.partner", "search_count", domain=[]), 3)
        self.assertTrue(odoo.idle_read.wait(5))
        self.assertEqual(odoo.after_close, [])
        self.assertEqual(odoo.requests, [(1, "call"), (1, "call"), (2, "call")])

    def test_an_https_connection_the_server_closed_carries_nothing(self):
        # Offline, without a TLS handshake: the TLS sockets are fakes, each
        # one's file descriptor a real socket's, the kept end of a pair, which
        # is readable once its other end, the server's, is closed.
        body = xmlrpc.client.dumps((3,), methodresponse=True).encode()
        answer = (
            "HTTP/1.1 200 OK\r\nContent-Type: text/xml\r\n"
            f"Content-Length: {len(body)}\r\n\r\n"
        ).encode() + body
        tls_sockets = []
        server_ends = []
        for _ in range(2):
            kept_end, server_end = socket.socketpair()
            self.addCleanup(kept_end.close)
            self.addCleanup(server_end.close)
            tls_socket = mock.create_autospec(ssl.SSLSocket, instance=True)
            tls_socket.fileno.return_value = kept_end.fileno()
            tls_socket.makefile.side_effect = lambda *args, **kwargs: io.BytesIO(answer)
            tls_sockets.append(tls_socket)
            server_ends.append(server_end)
        tcp_sockets = [mock.create_autospec(socket.socket, instance=True) for _ in range(2)]
        transport = _SafeTransport((2, 9.5))
        with mock.patch("socket.create_connection", side_effect=tcp_sockets):
            with mock.patch.object(ssl.SSLContext, "wrap_socket", side_effect=tls_sockets):
                for _ in range(2):
                    self.assertEqual(
                        transport.request("127.0.0.1", "/xmlrpc/2/object", b"<call/>"), (3,)
                    )
                server_ends[0].close()
                self.assertEqual(
                    transport.request("127.0.0.1", "/xmlrpc/2/object", b"<call/>"), (3,)
                )
        first, second = tls_sockets
        self.assertEqual(sent_requests(first), 2)
        first.close.assert_called_once_with()
        self.assertEqual(sent_requests(second), 1)

    def test_a_connection_whose_error_answer_stalls_is_not_kept(self):
        # An answer whose status is not 200, whose body stops coming on a
        # connection that stays open: xmlrpc.client reads that body outside
        # the try that closes a connection after a failure, so its read
        # timeout kept the connection, its answer half read, for the next
        # call to go out on. The read timeout waits for each answer's head
        # (wait_for_heads): it fires for that body alone.
        self.wait_for_heads()
        odoo = self.start(KeepAliveOdoo({("call", 1): "answer an error that stalls"}))
        self.make_profile(odoo.url, "xmlrpc", PASSWORD)
        with Client("erp", timeout=(5, 0.2)) as client:
            error = self.raised(
                RequestError, lambda: client.call("res.partner", "search_count", domain=[])
            )
            self.assertEqual(error.timeout_fired, "read")
            self.assertEqual(client.call("res.partner", "search_count", domain=[]), 3)
        self.assertEqual(odoo.requests, [(1, "login"), (2, "call"), (3, "call")])

    def test_a_connection_whose_answer_is_not_a_200_is_not_kept(self):
        # xmlrpc.client raises its ProtocolError for it outside the try that
        # closes a connection after a failure, and reads its body only when
        # it has a Content-Length. Sent in chunks, the body stayed unread on
        # the connection, and the next call went out on it, to fail; on a
        # connection that closes after it, the answer kept its socket open;
        # with a Content-Length, the connection was kept.
        opened = self.record_sockets(socket)
        for action in (
            "answer an error in chunks",
            "answer an error in chunks, then close",
            "answer an error",
        ):
            with self.subTest(action=action):
                odoo = self.start(KeepAliveOdoo({("call", 1): action}))
                self.make_profile(odoo.url, "xmlrpc", PASSWORD)
                with Client("erp") as client:
                    error = self.raised(
                        RequestError,
                        lambda: client.call("res.partner", "search_count", domain=[]),
                    )
                    self.assertEqual([sock for sock in opened if sock.fileno() != -1], [])
                    self.assertEqual(client.call("res.partner", "search_count", domain=[]), 3)
                self.assertIsInstance(error, xmlrpc.client.ProtocolError)
                self.assertEqual(
                    str(error),
                    "A request of profile 'erp' failed: ProtocolError, code 500. Its "
                    "message is not shown, as it can quote the profile's values.",
                )
                self.assertEqual(odoo.requests, [(1, "login"), (2, "call"), (3, "call")])

    def test_an_interrupted_request_closes_its_connection(self):
        # An interrupt, such as Ctrl-C, is no Exception: xmlrpc.client's
        # close after a failure missed it. As the request was sent, it left
        # the connection in the middle of it, on which the next call failed;
        # as the answer's head or its body was read, the answer unread on an
        # open socket. It goes on unchanged, once the connection is closed.
        points = {
            "the request's send": (
                http.client.HTTPConnection,
                "send",
                [(1, "login"), (2, "call"), (3, "call")],
            ),
            "the read of the answer's head": (
                http.client.HTTPResponse,
                "begin",
                [(1, "login"), (2, "call"), (2, "call"), (3, "call")],
            ),
            # xmlrpc.client's parse_response reads the body and parses it,
            # once the head was read.
            "the read of the answer's body": (
                xmlrpc.client.Transport,
                "parse_response",
                [(1, "login"), (2, "call"), (2, "call"), (3, "call")],
            ),
        }
        opened = self.record_sockets(socket)
        for point, (owner, step, received) in points.items():
            with self.subTest(point=point):
                odoo = self.start(KeepAliveOdoo())
                self.make_profile(odoo.url, "xmlrpc", PASSWORD)
                interrupt = KeyboardInterrupt()
                with Client("erp") as client:
                    self.assertEqual(client.call("res.partner", "search_count", domain=[]), 3)
                    with mock.patch.object(owner, step, side_effect=interrupt):
                        with self.assertRaises(KeyboardInterrupt) as raised:
                            client.call("res.partner", "search_count", domain=[])
                    self.assertIs(raised.exception, interrupt)
                    self.assertEqual([sock for sock in opened if sock.fileno() != -1], [])
                    self.assertEqual(client.call("res.partner", "search_count", domain=[]), 3)
                # Once the server's threads are done, each request it read is
                # recorded, the interrupted one's among them, in any order.
                odoo.stop()
                self.assertCountEqual(odoo.requests, received)

    def test_an_interrupted_tls_handshake_leaves_nothing_for_the_next_call(self):
        # Offline: the login is replaced, and the server, which speaks no
        # TLS, reads any request sent in clear. Each TCP connection is real,
        # and the ssl module's handshake raises an interrupt, then a reset,
        # at the step each case names. Before ssl takes the socket over, the
        # connection kept the plain socket, on which the next call went out
        # unencrypted, the password in it; in the handshake, the socket ssl
        # detached, on which the check before reuse raised ValueError. The TLS
        # socket that took its descriptor over is closed by ssl itself after
        # anything the handshake raises, the interrupt too, from Python 3.11.8
        # and 3.12.2 (SSLSocket._create): that nothing is left open in the
        # handshake rests on it, which is why the library requires 3.11.8 or
        # later, but not 3.12.0 or 3.12.1. An earlier release's ssl, such as
        # 3.11.6's, closes it after an OSError or a ValueError only, and
        # leaves it open after the interrupt until it is collected.
        self.fail_on_resource_warnings()
        steps = {
            "before ssl takes the socket over": (ssl.SSLContext, "wrap_socket"),
            "in the handshake": (ssl.SSLSocket, "do_handshake"),
        }
        for case, (owner, step) in steps.items():
            with self.subTest(case=case):
                odoo = self.start(KeepAliveOdoo())
                self.make_profile(odoo.url.replace("http://", "https://"), "xmlrpc", PASSWORD)
                interrupt = KeyboardInterrupt()
                reset = ConnectionResetError(errno.ECONNRESET, "Connection reset by peer")
                with mock.patch.object(odoo_xmlrpc_auth.client, "_log_in", return_value=UID):
                    with mock.patch.object(owner, step, side_effect=[interrupt, reset]):
                        with Client("erp") as client:
                            with self.assertRaises(KeyboardInterrupt) as raised:
                                client.call("res.partner", "search_count", domain=[])
                            error = self.raised(
                                RequestError,
                                lambda: client.call("res.partner", "search_count", domain=[]),
                            )
                self.assertIs(raised.exception, interrupt)
                self.assertIsInstance(error, ConnectionResetError)
                self.assertEqual(odoo.requests, [])

    def test_an_interrupted_json2_tls_handshake_leaves_nothing_for_the_next_call(self):
        # The test above over JSON-2, at the same two steps; JSON-2 has no
        # login to replace. Offline: the server, which speaks no TLS, reads any
        # request sent in clear. Each TCP connection is real, and the ssl
        # module's handshake raises an interrupt, then a reset, at the step
        # each case names; in the handshake, the TLS socket is the one ssl's
        # own wrap_socket makes. urllib3 keeps the TCP socket in its connection
        # while ssl wraps it (HTTPSConnection.connect): before ssl takes it
        # over, the plain one, which would carry the next call unencrypted, the
        # key in it; in the handshake, the one ssl detached. It closes that
        # connection after anything a request raises, the interrupt too, and
        # keeps it out of its pool (HTTPConnectionPool.urlopen): the reset,
        # raised as the next call wraps its socket, shows that the call opened
        # a new connection. The TLS socket that took the detached one's
        # descriptor over is out of urllib3's reach: ssl closes it after
        # anything the handshake raises, from Python 3.11.8 and 3.12.2
        # (SSLSocket._create). An earlier release's ssl, such as 3.11.6's,
        # leaves it open after the interrupt: the test fails there.
        self.fail_on_resource_warnings()
        steps = {
            "before ssl takes the socket over": (ssl.SSLContext, "wrap_socket"),
            "in the handshake": (ssl.SSLSocket, "do_handshake"),
        }
        for case, (owner, step) in steps.items():
            with self.subTest(case=case):
                odoo = self.start(KeepAliveOdoo())
                self.make_profile(odoo.url.replace("http://", "https://"), "json2", KEY)
                interrupt = KeyboardInterrupt()
                reset = ConnectionResetError(errno.ECONNRESET, "Connection reset by peer")
                with mock.patch.object(owner, step, side_effect=[interrupt, reset]):
                    with Client("erp") as client:
                        with self.assertRaises(KeyboardInterrupt) as raised:
                            client.call("res.partner", "search_count", domain=[])
                        error = self.raised(
                            RequestError,
                            lambda: client.call("res.partner", "search_count", domain=[]),
                        )
                self.assertIs(raised.exception, interrupt)
                self.assertIsInstance(error, requests.ConnectionError)
                self.assertEqual(
                    str(error),
                    "A request of profile 'erp' failed: ConnectionError. Its message is "
                    "not shown, as it can quote the profile's values.",
                )
                self.assertEqual(odoo.requests, [])

    def test_server_proxy_writes_a_call_on_its_kept_connection_unchecked(self):
        # A guard of xmlrpc.client's own sending, which ServerProxy keeps:
        # the call goes out on the connection the server closed, and that
        # connection's end makes xmlrpc.client send it again on a new one.
        odoo = self.start(KeepAliveOdoo({("call", 1): "answer, then close idle"}))
        self.make_profile(odoo.url, "xmlrpc", PASSWORD)
        with ServerProxy(profile="erp") as proxy:
            self.assertEqual(proxy.execute_kw("res.partner", "search_count", [[]]), 3)
            self.assertTrue(odoo.idle_closed.wait(5))
            self.assertEqual(proxy.execute_kw("res.partner", "search_count", [[]]), 3)
        self.assertTrue(odoo.idle_read.wait(5))
        self.assertEqual(odoo.after_close, ["call"])
        self.assertEqual(odoo.requests, [(1, "login"), (2, "call"), (3, "call")])


def sent(tls_socket):
    """What ``tls_socket``, a fake, was given to send, in order."""
    return b"".join(call.args[0] for call in tls_socket.sendall.call_args_list)


def sent_requests(tls_socket):
    """How many requests went out on ``tls_socket``, a fake: the request lines
    in what it was given to send."""
    return sent(tls_socket).count(b"POST /xmlrpc/2/object HTTP/1.1\r\n")


class FailingAnswer(io.BytesIO):
    """The file a fake TLS socket gives to read its answer: ``head``, its
    first bytes if any, then any read past them raises ``failure``. Without
    a head, its status line cannot be read."""

    def __init__(self, failure, head=b""):
        super().__init__(head)
        self.failure = failure

    def readline(self, size=-1):
        if self.tell() == len(self.getvalue()):
            raise self.failure
        return super().readline(size)

    def read(self, size=-1):
        if self.tell() == len(self.getvalue()):
            raise self.failure
        return super().read(size)


class CutOffTest(StubTestCase):
    """Client sends each request once, the XML-RPC login's too, and never
    again by itself after a failure: xmlrpc.client sends one a second time
    after its connection was closed or reset before the answer
    (Transport.request), and a server that ran the call would run it
    twice. ServerProxy keeps that second sending.

    When the connection of a call fails after the call started to go out,
    the RequestError says that the server may have run it, on both APIs. A
    failure before anything was sent says nothing of it, nor does the
    login's.

    The server here reads the request, then closes the connection without
    answering ("drop"), as one that ran the call and broke off before its
    answer."""

    def handshake_reset(self):
        """The url of a server that reads the client's first message of a
        TLS handshake, then resets the connection: a handshake that fails
        before anything was sent."""
        listener = socket.socket()
        self.addCleanup(listener.close)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)

        def reset():
            connection, _ = listener.accept()
            with connection:
                connection.recv(65536)
                # Closed with a linger of 0 seconds, it sends a reset.
                connection.setsockopt(
                    socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0)
                )

        thread = threading.Thread(target=reset, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        return f"https://127.0.0.1:{listener.getsockname()[1]}"

    def json2_https_call(self, *handshakes):
        """The RequestError of a call of json2_https_client(*handshakes)."""
        with self.json2_https_client(*handshakes) as client:
            return self.raised(
                RequestError, lambda: client.call("res.partner", "search_count", domain=[])
            )

    def test_a_call_cut_off_on_a_new_connection_is_sent_once_and_may_have_run(self):
        # The login closes its own connection (_log_in): the first call
        # goes out on a new one.
        odoo = self.start(KeepAliveOdoo({("call", 1): "drop"}))
        client = self.make_client(odoo.url, "xmlrpc", PASSWORD)
        error = self.raised(
            RequestError, lambda: client.call("res.partner", "search_count", domain=[])
        )
        self.assertIsInstance(error, http.client.RemoteDisconnected)
        self.assertEqual(
            str(error),
            "A request of profile 'erp' failed: RemoteDisconnected. It failed after "
            "the call started to go out, before its answer was read whole: the server "
            "may have run the call, so do not repeat it before reading back what it "
            "did. Its message is not shown, as it can quote the profile's values.",
        )
        self.assertEqual(odoo.requests, [(1, "login"), (2, "call")])
        self.assertNeverShows(error, PASSWORD)
        self.assertNoFrameHolds(error, PASSWORD)

    def test_a_call_cut_off_on_a_kept_connection_is_sent_once_and_may_have_run(self):
        odoo = self.start(KeepAliveOdoo({("call", 2): "drop"}))
        client = self.make_client(odoo.url, "xmlrpc", PASSWORD)
        self.assertEqual(client.call("res.partner", "search_count", domain=[]), 3)
        error = self.raised(
            RequestError, lambda: client.call("res.partner", "search_count", domain=[])
        )
        self.assertIsInstance(error, http.client.RemoteDisconnected)
        self.assertEqual(
            str(error),
            "A request of profile 'erp' failed: RemoteDisconnected. It failed after "
            "the call started to go out, before its answer was read whole: the server "
            "may have run the call, so do not repeat it before reading back what it "
            "did. Its message is not shown, as it can quote the profile's values.",
        )
        self.assertEqual(odoo.requests, [(1, "login"), (2, "call"), (2, "call")])
        self.assertNeverShows(error, PASSWORD)
        self.assertNoFrameHolds(error, PASSWORD)

    def test_a_json2_call_cut_off_on_a_new_connection_says_it_may_have_run(self):
        odoo = self.start(KeepAliveOdoo({("call", 1): "drop"}))
        client = self.make_client(odoo.url, "json2", KEY)
        error = self.raised(
            RequestError, lambda: client.call("res.partner", "search_count", domain=[])
        )
        self.assertIsInstance(error, requests.ConnectionError)
        self.assertEqual(
            str(error),
            "A request of profile 'erp' failed: ConnectionError. It failed after the "
            "call started to go out, before its answer was read whole: the server may "
            "have run the call, so do not repeat it before reading back what it did. "
            "Its message is not shown, as it can quote the profile's values.",
        )
        self.assertEqual(odoo.requests, [(1, "call")])
        self.assertNeverShows(error, KEY)
        self.assertNoFrameHolds(error, KEY)

    def test_a_json2_call_cut_off_on_a_kept_connection_says_it_may_have_run(self):
        odoo = self.start(KeepAliveOdoo({("call", 2): "drop"}))
        client = self.make_client(odoo.url, "json2", KEY)
        self.assertEqual(client.call("res.partner", "search_count", domain=[]), 3)
        error = self.raised(
            RequestError, lambda: client.call("res.partner", "search_count", domain=[])
        )
        self.assertIsInstance(error, requests.ConnectionError)
        self.assertEqual(
            str(error),
            "A request of profile 'erp' failed: ConnectionError. It failed after the "
            "call started to go out, before its answer was read whole: the server may "
            "have run the call, so do not repeat it before reading back what it did. "
            "Its message is not shown, as it can quote the profile's values.",
        )
        self.assertEqual(odoo.requests, [(1, "call"), (1, "call")])
        self.assertNeverShows(error, KEY)
        self.assertNoFrameHolds(error, KEY)

    def test_a_login_cut_off_is_sent_once_and_runs_no_call(self):
        odoo = self.start(KeepAliveOdoo({("login", 1): "drop"}))
        client = self.make_client(odoo.url, "xmlrpc", PASSWORD)
        error = self.raised(
            RequestError, lambda: client.call("res.partner", "search_count", domain=[])
        )
        self.assertIsInstance(error, http.client.RemoteDisconnected)
        self.assertEqual(
            str(error),
            "A request of profile 'erp' failed: RemoteDisconnected. Its message is "
            "not shown, as it can quote the profile's values.",
        )
        self.assertEqual(odoo.requests, [(1, "login")])
        self.assertNeverShows(error, PASSWORD)
        self.assertNoFrameHolds(error, PASSWORD)

    def test_a_call_whose_connection_cannot_open_has_no_note(self):
        # The server is gone after the first call: the kept connection is at
        # its end, and the new one is refused, before anything was sent.
        cases = {
            "xmlrpc": (
                PASSWORD,
                socket,
                ConnectionRefusedError,
                "ConnectionRefusedError, code 111",
                [(1, "login"), (2, "call")],
            ),
            "json2": (
                KEY,
                urllib3.util.connection,
                requests.ConnectionError,
                "ConnectionError",
                [(1, "call")],
            ),
        }
        for api, (secret, module, original, name, received) in cases.items():
            with self.subTest(api=api):
                opened = self.record_sockets(module)
                odoo = self.start(KeepAliveOdoo())
                client = self.make_client(odoo.url, api, secret, f"erp-{api}")
                self.assertEqual(client.call("res.partner", "search_count", domain=[]), 3)
                odoo.stop()
                # The kept connection, the last one opened, once the server's
                # end of it has come: readable, as the check before reuse
                # reads it.
                self.assertTrue(urllib3.util.wait_for_read(opened[-1], timeout=5))
                error = self.raised(
                    RequestError,
                    lambda: client.call("res.partner", "search_count", domain=[]),
                )
                self.assertIsInstance(error, original)
                self.assertEqual(
                    str(error),
                    f"A request of profile 'erp-{api}' failed: {name}. Its message is "
                    "not shown, as it can quote the profile's values.",
                )
                self.assertEqual(odoo.requests, received)

    def test_an_xmlrpc_tls_handshake_that_fails_has_no_note(self):
        # Offline: the login is replaced, and the call's TLS handshake, on a
        # fake socket, fails as one the server resets: once, as every
        # request of Client, where xmlrpc.client would try a reset again.
        self.make_profile("https://127.0.0.1", "xmlrpc", PASSWORD)
        tcp_socket = mock.create_autospec(socket.socket, instance=True)
        reset = ConnectionResetError(errno.ECONNRESET, "Connection reset by peer")
        with mock.patch.object(odoo_xmlrpc_auth.client, "_log_in", return_value=UID):
            with mock.patch("socket.create_connection", return_value=tcp_socket):
                with mock.patch.object(
                    ssl.SSLContext, "wrap_socket", side_effect=reset
                ) as wrap:
                    with Client("erp") as client:
                        error = self.raised(
                            RequestError,
                            lambda: client.call("res.partner", "search_count", domain=[]),
                        )
        self.assertIsInstance(error, ConnectionResetError)
        self.assertEqual(
            str(error),
            "A request of profile 'erp' failed: ConnectionResetError, code 104. Its "
            "message is not shown, as it can quote the profile's values.",
        )
        wrap.assert_called_once()
        tcp_socket.sendall.assert_not_called()
        self.assertNoFrameHolds(error, PASSWORD)

    def test_an_xmlrpc_tls_handshake_that_fails_leaves_nothing_for_the_next_call(self):
        # Offline: the login is replaced, and the sockets are fakes. A
        # handshake that fails leaves its TCP socket in the connection
        # (http.client's HTTPSConnection.connect). The ssl module detaches
        # that socket before its handshake, and the check before reuse
        # raises ValueError on a detached one; here the handshake fails
        # before ssl takes the socket over, as an interrupt there can
        # (test_an_interrupted_tls_handshake_leaves_nothing_for_the_next_call),
        # and leaves the socket whole: one whose descriptor is a real
        # socket's, the kept end of a pair, open and silent. Kept, it would
        # carry the next call unencrypted.
        kept_end, server_end = socket.socketpair()
        self.addCleanup(kept_end.close)
        self.addCleanup(server_end.close)
        failed_tcp_socket, tcp_socket = (
            mock.create_autospec(socket.socket, instance=True) for _ in range(2)
        )
        failed_tcp_socket.fileno.return_value = kept_end.fileno()
        body = xmlrpc.client.dumps((3,), methodresponse=True).encode()
        tls_socket = mock.create_autospec(ssl.SSLSocket, instance=True)
        tls_socket.makefile.return_value = io.BytesIO(
            (
                "HTTP/1.1 200 OK\r\nContent-Type: text/xml\r\n"
                f"Content-Length: {len(body)}\r\n\r\n"
            ).encode()
            + body
        )
        reset = ConnectionResetError(errno.ECONNRESET, "Connection reset by peer")
        with mock.patch.object(odoo_xmlrpc_auth.client, "_log_in", return_value=UID):
            with mock.patch(
                "socket.create_connection", side_effect=[failed_tcp_socket, tcp_socket]
            ):
                with mock.patch.object(
                    ssl.SSLContext, "wrap_socket", side_effect=[reset, tls_socket]
                ):
                    client = self.make_client("https://127.0.0.1", "xmlrpc", PASSWORD)
                    self.raised(
                        RequestError,
                        lambda: client.call("res.partner", "search_count", domain=[]),
                    )
                    failed_tcp_socket.close.assert_called_once_with()
                    self.assertEqual(
                        client.call("res.partner", "search_count", domain=[]), 3
                    )
        failed_tcp_socket.sendall.assert_not_called()
        self.assertEqual(sent_requests(tls_socket), 1)

    def test_a_call_cut_off_by_a_reset_keeps_the_errno_of_its_error(self):
        odoo = self.start(KeepAliveOdoo({("call", 1): "reset"}))
        client = self.make_client(odoo.url, "xmlrpc", PASSWORD)
        error = self.raised(
            RequestError, lambda: client.call("res.partner", "search_count", domain=[])
        )
        self.assertIsInstance(error, ConnectionResetError)
        self.assertEqual(
            str(error),
            "A request of profile 'erp' failed: ConnectionResetError, code 104. It "
            "failed after the call started to go out, before its answer was read "
            "whole: the server may have run the call, so do not repeat it before "
            "reading back what it did. Its message is not shown, as it can quote the "
            "profile's values.",
        )
        self.assertEqual(odoo.requests, [(1, "login"), (2, "call")])

    def test_a_call_whose_error_answer_breaks_off_leaves_no_socket_open(self):
        # An answer whose status is not 200, on a connection that closes after
        # it and so hands the answer its socket: xmlrpc.client reads that body
        # outside the try that closes a connection after a failure, and a
        # reset there left the answer and its socket open. Reading the body
        # raises it here, as a reset socket does. The call says that the
        # server may have run it, as any call cut off.
        opened = self.record_sockets(socket)
        odoo = self.start(RawXmlRpc())
        odoo.call_answer = Answer(200, xmlrpc_answer("<int>3</int>"))
        client = self.make_client(odoo.url, "xmlrpc", PASSWORD)
        self.assertEqual(client.call("res.partner", "search_count", domain=[]), 3)
        odoo.call_answer = Answer(
            500, b"<html>Internal Server Error</html>", {"Content-Type": "text/html"}
        )
        reset = ConnectionResetError(errno.ECONNRESET, "Connection reset by peer")
        with mock.patch.object(http.client.HTTPResponse, "read", side_effect=reset):
            error = self.raised(
                RequestError, lambda: client.call("res.partner", "search_count", domain=[])
            )
        self.assertEqual([sock for sock in opened if sock.fileno() != -1], [])
        self.assertIsInstance(error, ConnectionResetError)
        self.assertEqual(
            str(error),
            "A request of profile 'erp' failed: ConnectionResetError, code 104. It "
            "failed after the call started to go out, before its answer was read "
            "whole: the server may have run the call, so do not repeat it before "
            "reading back what it did. Its message is not shown, as it can quote the "
            "profile's values.",
        )
        self.assertEqual(
            [request["path"] for request in odoo.requests],
            ["/xmlrpc/2/common", "/xmlrpc/2/object", "/xmlrpc/2/object"],
        )

    def test_an_https_error_answer_that_breaks_off_is_closed(self):
        # Offline, without a TLS handshake: the sockets are fakes, and the
        # file the TLS socket gives to read its answer stands for a real
        # socket's, which holds that socket open until it is closed. The
        # answer, a 500 on a connection that closes after it, breaks off in
        # its body.
        answer_file = FailingAnswer(
            ConnectionResetError(errno.ECONNRESET, "Connection reset by peer"),
            b"HTTP/1.1 500 Internal Server Error\r\nContent-Length: 10\r\n"
            b"Connection: close\r\n\r\n",
        )
        tls_socket = mock.create_autospec(ssl.SSLSocket, instance=True)
        tls_socket.makefile.return_value = answer_file
        tcp_socket = mock.create_autospec(socket.socket, instance=True)
        transport = _SafeTransport((2, 9.5))
        with mock.patch("socket.create_connection", return_value=tcp_socket) as connect:
            with mock.patch.object(ssl.SSLContext, "wrap_socket", return_value=tls_socket):
                self.raised(
                    odoo_xmlrpc_auth.client._CutOff,
                    lambda: transport.request("127.0.0.1", "/xmlrpc/2/object", b"<call/>"),
                )
        connect.assert_called_once()
        self.assertTrue(answer_file.closed)

    def test_an_answer_whose_head_cannot_be_read_says_the_call_may_have_run(self):
        # Over JSON-2, urllib3 raises it as a connection aborted.
        cases = {
            "xmlrpc": (
                PASSWORD,
                http.client.BadStatusLine,
                "BadStatusLine",
                [(1, "login"), (2, "call")],
            ),
            "json2": (KEY, requests.ConnectionError, "ConnectionError", [(1, "call")]),
        }
        for api, (secret, original, name, received) in cases.items():
            with self.subTest(api=api):
                odoo = self.start(
                    KeepAliveOdoo({("call", 1): "answer a head that cannot be read"})
                )
                client = self.make_client(odoo.url, api, secret, f"erp-{api}")
                error = self.raised(
                    RequestError,
                    lambda: client.call("res.partner", "search_count", domain=[]),
                )
                self.assertIsInstance(error, original)
                self.assertEqual(
                    str(error),
                    f"A request of profile 'erp-{api}' failed: {name}. It failed after "
                    "the call started to go out, before its answer was read whole: the "
                    "server may have run the call, so do not repeat it before reading "
                    "back what it did. Its message is not shown, as it can quote the "
                    "profile's values.",
                )
                self.assertEqual(odoo.requests, received)

    def test_a_json2_tls_handshake_that_fails_has_no_note(self):
        error = self.json2_https_call(
            ssl.SSLError(1, "[SSL: SSLV3_ALERT_HANDSHAKE_FAILURE] handshake failure")
        )
        self.assertIsInstance(error, requests.exceptions.SSLError)
        self.assertEqual(
            str(error),
            "A request of profile 'erp' failed: SSLError. Its message is not shown, "
            "as it can quote the profile's values.",
        )

    def test_a_json2_tls_handshake_reset_has_no_note(self):
        # urllib3 raises it as it raises a call cut off, a ProtocolError
        # "Connection aborted.": nothing of the call had gone out.
        client = self.make_client(self.handshake_reset(), "json2", KEY)
        error = self.raised(
            RequestError, lambda: client.call("res.partner", "search_count", domain=[])
        )
        self.assertIsInstance(error, requests.ConnectionError)
        self.assertEqual(
            str(error),
            "A request of profile 'erp' failed: ConnectionError. Its message is not "
            "shown, as it can quote the profile's values.",
        )
        self.assertNeverShows(error, KEY)

    def test_a_json2_tls_error_as_the_call_goes_out_says_it_may_have_run(self):
        # urllib3 raises it as it raises a handshake that fails, an SSLError.
        tls_socket = mock.create_autospec(ssl.SSLSocket, instance=True)
        tls_socket.sendall.side_effect = ssl.SSLError(1, "[SSL: BAD_LENGTH] bad length")
        error = self.json2_https_call(tls_socket)
        self.assertIsInstance(error, requests.exceptions.SSLError)
        self.assertEqual(
            str(error),
            "A request of profile 'erp' failed: SSLError. It failed after the call "
            "started to go out, before its answer was read whole: the server may have "
            "run the call, so do not repeat it before reading back what it did. Its "
            "message is not shown, as it can quote the profile's values.",
        )
        tls_socket.sendall.assert_called_once()
        self.assertNeverShows(error, KEY)
        self.assertNoFrameHolds(error, KEY)

    def test_a_json2_call_whose_first_bytes_fail_says_it_may_have_run(self):
        # Offline, over http, where the connection opens as the request
        # starts to go out: its TCP socket, a fake, fails as the request's
        # head is sent, before its body.
        tcp_socket = mock.create_autospec(socket.socket, instance=True)
        tcp_socket.sendall.side_effect = ConnectionAbortedError(
            errno.ECONNABORTED, "Software caused connection abort"
        )
        with mock.patch(
            "urllib3.util.connection.create_connection", return_value=tcp_socket
        ) as connect:
            client = self.make_client("http://127.0.0.1", "json2", KEY)
            error = self.raised(
                RequestError, lambda: client.call("res.partner", "search_count", domain=[])
            )
        connect.assert_called_once()
        self.assertIsInstance(error, requests.ConnectionError)
        self.assertEqual(
            str(error),
            "A request of profile 'erp' failed: ConnectionError. It failed after the "
            "call started to go out, before its answer was read whole: the server may "
            "have run the call, so do not repeat it before reading back what it did. "
            "Its message is not shown, as it can quote the profile's values.",
        )
        tcp_socket.sendall.assert_called_once()

    def test_a_json2_tls_eof_as_the_answer_comes_says_it_may_have_run(self):
        # A server that ran the call, then dropped the connection without
        # TLS's close_notify.
        tls_socket = mock.create_autospec(ssl.SSLSocket, instance=True)
        tls_socket.makefile.return_value = FailingAnswer(
            ssl.SSLEOFError(8, "EOF occurred in violation of protocol")
        )
        error = self.json2_https_call(tls_socket)
        self.assertIsInstance(error, requests.exceptions.SSLError)
        self.assertEqual(
            str(error),
            "A request of profile 'erp' failed: SSLError. It failed after the call "
            "started to go out, before its answer was read whole: the server may have "
            "run the call, so do not repeat it before reading back what it did. Its "
            "message is not shown, as it can quote the profile's values.",
        )
        self.assertTrue(sent(tls_socket).endswith(b'{"domain": []}'))
        self.assertNeverShows(error, KEY)
        self.assertNoFrameHolds(error, KEY)

    def test_a_redirect_whose_connection_cannot_open_says_the_call_may_have_run(self):
        # The call's request goes out and is answered with a redirect within
        # the origin, on a connection the answer closes: the redirect's
        # request goes on a new connection, whose handshake fails. The server
        # may have run the call: a 303 is the answer to a request it
        # processed, which requests follows with a GET.
        for status in (303, 307):
            with self.subTest(status=status):
                redirecting = mock.create_autospec(ssl.SSLSocket, instance=True)
                redirecting.makefile.return_value = io.BytesIO(
                    f"HTTP/1.1 {status} {http.HTTPStatus(status).phrase}\r\n"
                    "Location: /json/2/res.partner/search_count\r\n"
                    "Content-Length: 0\r\nConnection: close\r\n\r\n".encode()
                )
                error = self.json2_https_call(
                    redirecting,
                    ssl.SSLError(1, "[SSL: SSLV3_ALERT_HANDSHAKE_FAILURE] handshake failure"),
                )
                self.assertIsInstance(error, requests.exceptions.SSLError)
                self.assertEqual(
                    str(error),
                    "A request of profile 'erp' failed: SSLError. It failed after the "
                    "call started to go out, before its answer was read whole: the "
                    "server may have run the call, so do not repeat it before reading "
                    "back what it did. Its message is not shown, as it can quote the "
                    "profile's values.",
                )
                self.assertEqual(
                    sent(redirecting).count(
                        b"POST /json/2/res.partner/search_count HTTP/1.1\r\n"
                    ),
                    1,
                )

    def test_a_json2_handshake_that_fails_after_a_call_that_went_out_has_no_note(self):
        # The mark is the call's own: the first call's request goes out, and
        # its answer closes the connection; the second call's new connection
        # fails its handshake before anything of that call is sent.
        answering = mock.create_autospec(ssl.SSLSocket, instance=True)
        answering.makefile.return_value = io.BytesIO(
            b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
            b"Content-Length: 1\r\nConnection: close\r\n\r\n3"
        )
        handshake = ssl.SSLError(1, "[SSL: SSLV3_ALERT_HANDSHAKE_FAILURE] handshake failure")
        with self.json2_https_client(answering, handshake) as client:
            self.assertEqual(client.call("res.partner", "search_count", domain=[]), 3)
            error = self.raised(
                RequestError, lambda: client.call("res.partner", "search_count", domain=[])
            )
        self.assertIsInstance(error, requests.exceptions.SSLError)
        self.assertEqual(
            str(error),
            "A request of profile 'erp' failed: SSLError. Its message is not shown, "
            "as it can quote the profile's values.",
        )

    def test_a_json2_call_reads_no_other_calls_mark(self):
        # Offline, two clients in two threads, their sockets fakes: the first
        # call's request goes out, and its answer is held while the second
        # call's handshake fails; then the first call's connection is reset.
        reached, released = threading.Event(), threading.Event()
        self.addCleanup(released.set)

        def held_answer(*args):
            reached.set()
            released.wait(HOLD)
            return FailingAnswer(ConnectionResetError(errno.ECONNRESET, "Connection reset"))

        first_socket = mock.create_autospec(ssl.SSLSocket, instance=True)
        first_socket.makefile.side_effect = held_answer
        handshake = ssl.SSLError(1, "[SSL: SSLV3_ALERT_HANDSHAKE_FAILURE] handshake failure")
        tcp_sockets = [mock.create_autospec(socket.socket, instance=True) for _ in range(2)]
        first_errors = []

        def first_call():
            try:
                first.call("res.partner", "search_count", domain=[])
            except RequestError as error:
                first_errors.append(error)

        with mock.patch(
            "urllib3.util.connection.create_connection", side_effect=tcp_sockets
        ) as connect:
            with mock.patch.object(
                ssl.SSLContext, "wrap_socket", side_effect=[first_socket, handshake]
            ):
                first = self.make_client("https://127.0.0.1", "json2", KEY, "erp-first")
                second = self.make_client("https://127.0.0.1", "json2", KEY, "erp-second")
                thread = threading.Thread(target=first_call, daemon=True)
                thread.start()
                self.assertTrue(reached.wait(HOLD))
                second_error = self.raised(
                    RequestError,
                    lambda: second.call("res.partner", "search_count", domain=[]),
                )
                released.set()
                thread.join(HOLD)
        self.assertEqual(connect.call_count, 2)
        self.assertEqual(
            [str(error) for error in first_errors],
            [
                "A request of profile 'erp-first' failed: ConnectionError. It failed "
                "after the call started to go out, before its answer was read whole: "
                "the server may have run the call, so do not repeat it before reading "
                "back what it did. Its message is not shown, as it can quote the "
                "profile's values."
            ],
        )
        self.assertEqual(
            str(second_error),
            "A request of profile 'erp-second' failed: SSLError. Its message is not "
            "shown, as it can quote the profile's values.",
        )

    def test_a_json2_body_cut_short_after_an_error_status_says_the_call_may_have_run(self):
        odoo = self.start(Json2Odoo())
        odoo.answers[("res.partner", "write")] = RawAnswer(
            b"HTTP/1.1 500 Internal Server Error\r\nContent-Type: application/json\r\n"
            b"Content-Length: 400\r\n\r\n{"
        )
        client = self.make_client(odoo.url, "json2", KEY)
        error = self.raised(
            RequestError, lambda: client.call("res.partner", "write", [7], vals={})
        )
        self.assertIsInstance(error, requests.exceptions.ChunkedEncodingError)
        self.assertEqual(
            str(error),
            "A request of profile 'erp' failed: ChunkedEncodingError, code 500. It "
            "failed after the call started to go out, before its answer was read "
            "whole: the server may have run the call, so do not repeat it before "
            "reading back what it did. Its message is not shown, as it can quote the "
            "profile's values.",
        )
        self.assertNeverShows(error, KEY)
        self.assertNoFrameHolds(error, KEY)

    def test_a_json2_body_the_connection_brought_whole_has_no_note(self):
        # After an error status, a body sent in bad chunks, which requests
        # raises as it raises a body cut short, or one that is not the gzip it
        # says: the connection did not fail.
        answers = {
            "sent in bad chunks": (
                "ChunkedEncodingError",
                b"Transfer-Encoding: chunked\r\n\r\nzz\r\n3\r\n0\r\n\r\n",
            ),
            "not the gzip it says": (
                "ContentDecodingError",
                b"Content-Encoding: gzip\r\nContent-Length: 3\r\n\r\n[3]",
            ),
        }
        odoo = self.start(Json2Odoo())
        client = self.make_client(odoo.url, "json2", KEY)
        for case, (error_class, rest) in answers.items():
            with self.subTest(case=case):
                odoo.answers[("res.partner", "write")] = RawAnswer(
                    b"HTTP/1.1 500 Internal Server Error\r\n"
                    b"Content-Type: application/json\r\n" + rest
                )
                error = self.raised(
                    RequestError, lambda: client.call("res.partner", "write", [7], vals={})
                )
                self.assertEqual(
                    str(error),
                    f"A request of profile 'erp' failed: {error_class}, code 500. Its "
                    "message is not shown, as it can quote the profile's values.",
                )

    def test_server_proxy_sends_a_call_cut_off_a_second_time(self):
        # A guard of xmlrpc.client's own sending, which ServerProxy keeps.
        odoo = self.start(KeepAliveOdoo({("call", 1): "drop"}))
        self.make_profile(odoo.url, "xmlrpc", PASSWORD)
        with ServerProxy(profile="erp") as proxy:
            self.assertEqual(proxy.execute_kw("res.partner", "search_count", [[]]), 3)
        self.assertEqual(odoo.requests, [(1, "login"), (2, "call"), (3, "call")])


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

    def held(self, receive_buffer=None):
        """The url of a server whose system takes each connection, and which
        never reads a request nor answers it, for HOLD seconds: its listener
        closes then, and the system resets the connections it took. With
        ``receive_buffer``, each connection's receive buffer is set to that
        many bytes, which the system then does not grow."""
        listener = socket.socket()
        self.addCleanup(listener.close)
        if receive_buffer is not None:
            # Set before listen, as each connection the system takes inherits it.
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, receive_buffer)
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
        messages = {
            TypeError: "A Client of profile 'erp' cannot be created: timeout must be a "
            "(connect, read) tuple of two numbers of seconds",
            ValueError: "A Client of profile 'erp' cannot be created: each timeout must "
            "be a number of seconds above 0 and at most threading.TIMEOUT_MAX",
        }
        for case, (timeout, error_class) in cases.items():
            with self.subTest(case=case):
                error = self.raised(
                    ArgumentError, lambda: Client("erp", timeout=timeout)
                )
                self.assertIsInstance(error, error_class)
                self.assertEqual(str(error), messages[error_class])

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
        # A timeout has no code, wherever it fired: the answer's status 200
        # is not its code.
        self.assertEqual((error.timeout_fired, error.code), ("read", None))
        self.assertIn("The read timeout fired", str(error))
        self.assertNeverShows(error, KEY)
        self.assertNoFrameHolds(error, KEY)

    def test_a_json2_call_whose_sending_stops_is_a_connect_timeout_after_it_started(self):
        # A server that never reads, both sockets' buffers set to 16 KB,
        # which the system then does not grow: a body of 1 MB fills them,
        # whatever buffers the system gives by default, and its sending
        # stops. requests sends a request under the connect timeout, and
        # raises its end as a call cut off, over the socket's timeout: the
        # connect timeout fired, after the call started to go out.
        create = urllib3.util.connection.create_connection

        def connect(*args, **kwargs):
            sock = create(*args, **kwargs)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 16384)
            return sock

        self.make_profile(self.held(receive_buffer=16384), "json2", KEY)
        with mock.patch.object(urllib3.util.connection, "create_connection", connect):
            with Client("erp", timeout=(0.2, 5)) as client:
                error = self.raised(
                    RequestError,
                    lambda: client.call(
                        "res.partner", "search_count", domain=["x" * 1_000_000]
                    ),
                )
        self.assertIsInstance(error, requests.ConnectionError)
        self.assertEqual((error.timeout_fired, error.code), ("connect", None))
        self.assertEqual(
            str(error),
            "A request of profile 'erp' failed: ConnectionError. The connect timeout "
            "fired after the call started to go out: the server may have run the "
            "call, so do not repeat it before reading back what it did. Its message "
            "is not shown, as it can quote the profile's values.",
        )
        self.assertNeverShows(error, KEY)
        self.assertNoFrameHolds(error, KEY)

    def test_a_redirects_connect_timeout_says_the_call_may_have_run(self):
        # The call's request goes out and is answered with a redirect within
        # the origin, on a connection the answer closes; the redirect's new
        # connection times out as it opens. A 303 answers a request the
        # server processed.
        odoo = self.start(Json2Odoo())
        odoo.answers[("res.partner", "write")] = Answer(
            303, b"", {"Location": "/json/2/res.partner/search_count"}
        )
        odoo.answers[("res.partner", "search_count")] = 3
        client = self.make_client(odoo.url, "json2", KEY)
        opened = []
        create = urllib3.util.connection.create_connection

        def connect(*args, **kwargs):
            # The first connection opens; the next one's connect times out.
            if opened:
                raise socket.timeout("timed out")
            opened.append(create(*args, **kwargs))
            return opened[0]

        with mock.patch.object(urllib3.util.connection, "create_connection", connect):
            error = self.raised(
                RequestError, lambda: client.call("res.partner", "write", [7], vals={})
            )
        self.assertIsInstance(error, requests.ConnectTimeout)
        self.assertEqual((error.timeout_fired, error.code), ("connect", None))
        self.assertEqual(
            str(error),
            "A request of profile 'erp' failed: ConnectTimeout. The connect timeout "
            "fired after the call started to go out: the server may have run the "
            "call, so do not repeat it before reading back what it did. Its message "
            "is not shown, as it can quote the profile's values.",
        )
        self.assertEqual([request["method"] for request in odoo.requests], ["POST"])
        self.assertNeverShows(error, KEY)
        self.assertNoFrameHolds(error, KEY)

    def test_a_json2_tls_handshake_that_times_out_counts_as_read(self):
        # requests reports it as a ReadTimeout. Nothing of the call was sent.
        tcp_socket = mock.create_autospec(socket.socket, instance=True)
        handshake = socket.timeout("_ssl.c:999: The handshake operation timed out")
        with mock.patch("urllib3.util.connection.create_connection", return_value=tcp_socket):
            with mock.patch.object(ssl.SSLContext, "wrap_socket", side_effect=handshake):
                client = self.make_client("https://127.0.0.1", "json2", KEY)
                error = self.raised(
                    RequestError, lambda: client.call("res.partner", "search_count", domain=[])
                )
        self.assertIsInstance(error, requests.ReadTimeout)
        self.assertEqual((error.timeout_fired, error.code), ("read", None))
        self.assertIn("The read timeout fired: the server may still complete", str(error))
        tcp_socket.sendall.assert_not_called()

    def test_an_xmlrpc_answer_whose_body_stops_coming_is_a_read_timeout(self):
        # Not an answer that cannot be read: the read timeout fired.
        odoo = self.start(RawXmlRpc())
        released = threading.Event()
        self.addCleanup(released.set)
        odoo.call_answer = RawAnswer(
            b"HTTP/1.0 200 OK\r\nContent-Type: text/xml\r\nContent-Length: 400\r\n\r\n"
            b"<?xml version='1.0'?>",
            after=lambda: released.wait(HOLD),
        )
        self.make_profile(odoo.url, "xmlrpc", PASSWORD)
        with Client("erp", timeout=(5, 0.2)) as client:
            error = self.raised(
                RequestError, lambda: client.call("res.partner", "search_count", domain=[])
            )
        self.assertIsInstance(error, socket.timeout)
        self.assertEqual((error.timeout_fired, error.code), ("read", None))
        self.assertNeverShows(error, PASSWORD)
        self.assertNoFrameHolds(error, PASSWORD)

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
                    (
                        1,
                        "",
                        f"Error: A call of profile '{profile}' cannot be sent: 'ids' goes at "
                        "the top of the JSON payload, not in 'kwargs'\n",
                    ),
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
        # The payload's message, which names the profile, the command line's,
        # also one that does not exist, on one line whatever its name holds.
        call = {"model": "res.partner", "method": "read"}
        refused = {
            "[]": "the JSON payload must be an object",
            json.dumps({"method": "read"}): "missing 'model' in the JSON payload",
            json.dumps({"model": "res.partner"}): "missing 'method' in the JSON payload",
            json.dumps({**call, "kwarg": {}}): "unknown keys in the JSON payload: kwarg",
            json.dumps({**call, "ids": [7], "args": [[7]]}): (
                "give 'ids' or 'args' in the JSON payload, not both"
            ),
            json.dumps({**call, "kwargs": []}): "'kwargs' in the JSON payload must be an object",
            json.dumps({**call, "kwargs": {"ids": [7]}}): (
                "'ids' goes at the top of the JSON payload, not in 'kwargs'"
            ),
        }
        profiles = {
            "erp-xmlrpc": "'erp-xmlrpc'",
            "erp-json2": "'erp-json2'",
            "no-such-profile": "'no-such-profile'",
            "no\nsuch": "'no\\nsuch'",
        }
        for payload, message in refused.items():
            with self.subTest(payload=payload):
                for profile, quoted in profiles.items():
                    code, out, err = self.run_cli(profile, payload)
                    self.assertEqual(
                        (code, out, err),
                        (1, "", f"Error: A call of profile {quoted} cannot be sent: {message}\n"),
                    )
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
                "Error: A call of profile 'erp-xmlrpc' cannot be sent: invalid JSON: "
                "Exceeds the limit (4300 digits) for integer string conversion: value "
                "has 5000 digits; use sys.set_int_max_str_digits() to increase the "
                "limit\n",
            ),
        )
        # An array nested deeper than json's parser follows, on any version.
        code, out, err = self.run_cli("erp-xmlrpc", "[" * 100000 + "]" * 100000)
        self.assertEqual((code, out), (1, ""))
        self.assertTrue(
            err.startswith("Error: A call of profile 'erp-xmlrpc' cannot be sent: invalid JSON: ")
        )
        self.assertEqual(err.count("\n"), 1)
        self.assertEqual(self.xmlrpc_odoo.logins, 0)

    def test_an_answer_json_cannot_write_ends_as_one_message(self):
        # Over XML-RPC an answer can hold a dateTime or a base64 value, here
        # echoing the password; Odoo ran the call.
        answers = {
            "a dateTime": xmlrpc.client.DateTime(f"echo {PASSWORD}"),
            "a base64": xmlrpc.client.Binary(f"echo {PASSWORD}".encode()),
        }
        for case, answer in answers.items():
            with self.subTest(case=case):
                self.xmlrpc_odoo.answers[("res.partner", "read")] = [answer]
                code, out, err = self.run_cli(
                    "erp-xmlrpc", {"model": "res.partner", "method": "read", "ids": [7]}
                )
                self.assertEqual((code, out), (1, ""))
                self.assertEqual(
                    err,
                    "Error: Odoo answered a call of profile 'erp-xmlrpc' with a value "
                    "JSON cannot write, such as NaN, or a dateTime or base64 of "
                    "XML-RPC: it ran the call, so do not repeat it before reading back "
                    "what it did\n",
                )

    def test_an_answer_holding_nan_or_an_infinity_ends_as_one_message(self):
        # JSON has neither, though json.dumps writes them by default, as NaN
        # and Infinity: a double of XML-RPC, or what json reads over JSON-2.
        cases = {
            "erp-xmlrpc": (
                self.xmlrpc_odoo,
                "Error: Odoo answered a call of profile 'erp-xmlrpc' with a value "
                "JSON cannot write, such as NaN, or a dateTime or base64 of XML-RPC: "
                "it ran the call, so do not repeat it before reading back what it "
                "did\n",
            ),
            "erp-json2": (
                self.json2_odoo,
                "Error: Odoo answered a call of profile 'erp-json2' with a value "
                "JSON cannot write, such as NaN, or a dateTime or base64 of XML-RPC: "
                "it ran the call, so do not repeat it before reading back what it "
                "did\n",
            ),
        }
        for profile, (odoo, message) in cases.items():
            for case, value in (("NaN", float("nan")), ("an infinity", float("-inf"))):
                with self.subTest(profile=profile, case=case):
                    odoo.answers[("res.partner", "read")] = [{"id": 7, "x": value}]
                    code, out, err = self.run_cli(
                        profile, {"model": "res.partner", "method": "read", "ids": [7]}
                    )
                    self.assertEqual((code, out, err), (1, "", message))

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
