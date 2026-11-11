"""What the tests share: a store under a temporary HOME, and stub servers of
Odoo's two APIs on 127.0.0.1, each on a port the system picks.

No test reaches a real server or a real store. The stubs answer from a
table the test fills and record every request they get, so that a test can
check what was sent, and where.
"""

import contextlib
import http.server
import json
import os
import socket
import tempfile
import threading
import xmlrpc.client
import xmlrpc.server
from unittest import mock

import requests

# Dummy values of the stores; never a real credential. KEY has the shape of
# an Odoo API key, 40 lowercase hexadecimal digits.
KEY = "feedface" * 5
PASSWORD = "odoo_dummy_password_test"
DB = "odoo_db_test"
USER = "odoo_user_test"
UID = 7


class StoreMixin:
    """A store under a temporary HOME, for a unittest.TestCase."""

    def use_home(self, prefix=None):
        """A temporary HOME for the test, whose folder's name starts with
        ``prefix`` when one is given."""
        home = tempfile.TemporaryDirectory(prefix=prefix)
        self.addCleanup(home.cleanup)
        patcher = mock.patch.dict(os.environ, {"HOME": home.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        self._home = home.name
        return home.name

    def make_store(self, text, encoding="utf-8", profile="erp"):
        """Write ``text`` as the profile's file, in the store of the test's
        temporary HOME, made if the test has none yet."""
        folder = getattr(self, "_store_folder", None)
        if folder is None:
            home = getattr(self, "_home", None)
            if home is None:
                home = self.use_home()
            folder = os.path.join(home, ".config", "odoo-xmlrpc-auth")
            os.makedirs(folder)
            os.chmod(folder, 0o700)
            self._store_folder = folder
        path = os.path.join(folder, f"{profile}.conf")
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

    def raised(self, error_class, call):
        """The ``error_class`` that ``call()`` raises, with its traceback,
        which assertRaises drops."""
        try:
            call()
        except error_class as error:
            return error
        self.fail(f"{error_class.__name__} not raised")

    def assertNoFrameHolds(self, error, value):
        """No frame of ``error``'s traceback but the tests' own holds
        ``value`` in a local: a test runner or a debugger prints the repr of
        each (``pytest -l``). Fails naming each frame and local that does.

        ``error`` comes from ``raised``: the one assertRaises keeps has no
        traceback left to check."""
        self.assertIsNotNone(error.__traceback__)
        tests_folder = os.path.dirname(os.path.abspath(__file__))
        holding = []
        traceback = error.__traceback__
        while traceback is not None:
            frame = traceback.tb_frame
            if os.path.dirname(os.path.abspath(frame.f_code.co_filename)) != tests_folder:
                holding += [
                    f"{frame.f_code.co_name}:{name}"
                    for name, local in frame.f_locals.items()
                    if value in _shown(local)
                ]
            traceback = traceback.tb_next
        self.assertEqual(holding, [])


def _shown(local):
    """What a printer of a frame's locals shows of ``local``: its repr, or
    nothing when the repr fails, as it does for an object whose __init__
    raised before setting what its repr reads, or a value nested deeper than
    repr follows (pytest prints a placeholder instead).

    For an answer or a request of requests, whose repr shows only a status
    or a method, also what a debugger shows one attribute down: the
    answer's body, and the request's headers and body, which carry the
    key (``response.request.headers``)."""
    try:
        shown = repr(local)
    except (AttributeError, RecursionError):
        shown = ""
    if isinstance(local, requests.Response):
        shown += repr(local.content) + _shown(local.request)
    elif isinstance(local, requests.PreparedRequest):
        shown += repr(local.headers) + repr(local.body)
    return shown


def free_port():
    """A port of 127.0.0.1 on which nothing listens."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def serve(server):
    """Serve ``server`` in a thread; return what stops it."""
    # A stop waits for the loop to look at its flag: every 10 ms, not 500.
    thread = threading.Thread(target=server.serve_forever, args=(0.01,), daemon=True)
    thread.start()

    def stop():
        server.shutdown()
        server.server_close()
        thread.join()

    return stop


class XmlRpcOdoo:
    """Odoo's /xmlrpc/2/common and /xmlrpc/2/object, as far as Client and
    ServerProxy use them.

    ``authenticate`` raises ``login_fault`` when the test sets one, such as
    Odoo's Fault for a db it does not hold; else it gives UID for DB, USER
    and ``password`` (PASSWORD unless the test sets another), False
    otherwise. ``execute_kw`` checks the same db, uid and password, records
    the call in ``calls`` and answers from ``answers``: the value set for
    its model and method, or a Fault to raise.
    """

    def __init__(self):
        self.answers = {}
        self.calls = []
        self.logins = 0
        self.password = PASSWORD
        self.login_fault = None

        class Handler(xmlrpc.server.SimpleXMLRPCRequestHandler):
            rpc_paths = ("/xmlrpc/2/common", "/xmlrpc/2/object")

            def log_message(self, *args):
                pass

        self.server = xmlrpc.server.MultiPathXMLRPCServer(
            ("127.0.0.1", 0), requestHandler=Handler, logRequests=False
        )
        common = xmlrpc.server.SimpleXMLRPCDispatcher(allow_none=True)
        common.register_function(self.authenticate, "authenticate")
        obj = xmlrpc.server.SimpleXMLRPCDispatcher(allow_none=True)
        obj.register_function(self.execute_kw, "execute_kw")
        self.server.add_dispatcher("/xmlrpc/2/common", common)
        self.server.add_dispatcher("/xmlrpc/2/object", obj)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.stop = serve(self.server)

    def authenticate(self, db, user, password, user_agent_env):
        self.logins += 1
        if self.login_fault is not None:
            raise self.login_fault
        return UID if (db, user, password) == (DB, USER, self.password) else False

    def execute_kw(self, db, uid, password, model, method, args, kwargs):
        if (db, uid, password) != (DB, UID, self.password):
            raise xmlrpc.client.Fault(3, "Access Denied")
        self.calls.append({"model": model, "method": method, "args": args, "kwargs": kwargs})
        answer = self.answers[(model, method)]
        if isinstance(answer, xmlrpc.client.Fault):
            raise answer
        return answer


class Answer:
    """An HTTP answer of a stub: its status, its body (bytes, or a value
    sent as JSON) and its headers."""

    def __init__(self, status, body, headers=None):
        self.status = status
        self.body = body
        self.headers = headers if headers is not None else {}


class RawAnswer:
    """An HTTP answer of a stub written as it is, its status line and its
    headers included (``raw``, bytes), after which the connection closes:
    for answers no HTTP server sends, such as a body cut short. ``after``,
    when given, is called once the bytes are sent and before the
    connection closes: a test holds the connection open there."""

    def __init__(self, raw, after=None):
        self.raw = raw
        self.after = after


class HttpRecorder:
    """An HTTP server that records each request it gets in ``requests``
    (method, path, headers with lower-case names, body) and answers with
    ``answer(request)``: by default 404 with an empty body; None answers
    nothing; a RawAnswer, its bytes."""

    def __init__(self):
        self.requests = []
        recorder = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def handle_one(self):
                length = int(self.headers.get("Content-Length", 0))
                request = {
                    "method": self.command,
                    "path": self.path,
                    "headers": {name.lower(): value for name, value in self.headers.items()},
                    "body": self.rfile.read(length),
                }
                recorder.requests.append(request)
                answer = recorder.answer(request)
                if answer is None:
                    # No answer: the connection closes.
                    return
                if isinstance(answer, RawAnswer):
                    self.close_connection = True
                    self.wfile.write(answer.raw)
                    if answer.after is not None:
                        answer.after()
                    return
                body = (
                    answer.body
                    if isinstance(answer.body, bytes)
                    else json.dumps(answer.body).encode()
                )
                self.send_response(answer.status)
                headers = {"Content-Type": "application/json; charset=utf-8", **answer.headers}
                for name, value in headers.items():
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            do_GET = do_POST = handle_one

            def log_message(self, *args):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.stop = serve(self.server)

    def answer(self, request):
        return Answer(404, b"")


class Json2Odoo(HttpRecorder):
    """Odoo's POST /json/2/<model>/<method>, as far as Client uses it.

    A request without ``Authorization: Bearer KEY`` gets the 401 Odoo sends
    for a key it does not take. Any other answers from ``answers``: the
    Answer or the RawAnswer set for its model and method, or a 200 with the
    value set.
    """

    def __init__(self):
        self.answers = {}
        super().__init__()

    def answer(self, request):
        if request["headers"].get("authorization") != f"Bearer {KEY}":
            return Answer(
                401,
                {
                    "name": "werkzeug.exceptions.Unauthorized",
                    "message": "Invalid apikey",
                    "arguments": ["Invalid apikey", 401],
                    "context": {},
                    "debug": "Traceback (most recent call last): ...",
                },
                {"WWW-Authenticate": "Bearer"},
            )
        _, _, _, model, method = request["path"].split("/")
        answer = self.answers[(model, method)]
        return answer if isinstance(answer, (Answer, RawAnswer)) else Answer(200, answer)


class RawXmlRpc(HttpRecorder):
    """Odoo's /xmlrpc/2/common and /xmlrpc/2/object, answering with the
    Answer the test sets, whatever was asked: ``login_answer`` for the
    first (UID, by default), ``call_answer`` for the second. For answers no
    XML-RPC server sends."""

    def __init__(self):
        self.login_answer = Answer(200, xmlrpc.client.dumps((UID,), methodresponse=True).encode())
        self.call_answer = None
        super().__init__()

    def answer(self, request):
        return self.login_answer if request["path"] == "/xmlrpc/2/common" else self.call_answer


class KeepAliveOdoo:
    """Odoo's two APIs over HTTP/1.1 on raw sockets, keeping each connection
    open from one request to the next, as Odoo's server does and the stubs
    above, which close it after each answer, do not: for a client that keeps
    its connection, and a server that closes it or breaks it off.

    Each request is read in full, then recorded in ``requests`` as (the
    number of its connection, from 1 in the order they open, and what it is:
    "login" for /xmlrpc/2/common, "call" for any other path). The server
    then does what ``plan`` says for it, by what it is and its number among
    those, from 1, such as ``plan[("call", 2)]`` for the second call:

    - "answer", what it does when the plan says nothing: a 200, UID to the
      login and ANSWER to a call over XML-RPC, ANSWER as JSON over JSON-2;
      the connection stays open.
    - "answer, then close": that 200, saying Connection: close; then the
      server closes the connection.
    - "answer, then close idle": that 200; then the server closes its side
      of the connection and sets ``idle_closed``, reads the connection to
      its end, recording in ``after_close`` what each request that still
      comes on it is, and sets ``idle_read``.
    - "drop": the server closes the connection unanswered, as one that ran
      the call and broke off before its answer.
    """

    ANSWER = 3

    def __init__(self, plan=None):
        self.plan = plan if plan is not None else {}
        self.requests = []
        self.after_close = []
        self.idle_closed = threading.Event()
        self.idle_read = threading.Event()
        self._lock = threading.Lock()
        self._connections = []
        self._stopping = threading.Event()
        self._listener = socket.socket()
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(8)
        # accept() gives up every 10 ms, so that it sees a stop.
        self._listener.settimeout(0.01)
        self.url = f"http://127.0.0.1:{self._listener.getsockname()[1]}"
        self._threads = [threading.Thread(target=self._accept, daemon=True)]
        self._threads[0].start()

    def _accept(self):
        while not self._stopping.is_set():
            try:
                connection, _ = self._listener.accept()
            except TimeoutError:
                continue
            connection.settimeout(None)
            with self._lock:
                self._connections.append(connection)
                number = len(self._connections)
            thread = threading.Thread(target=self._serve, args=(connection, number), daemon=True)
            self._threads.append(thread)
            thread.start()

    def _serve(self, connection, number):
        reader = connection.makefile("rb")
        try:
            while (path := self._path_read(reader)) is not None:
                what = self._what(path)
                with self._lock:
                    nth = 1 + [done for _, done in self.requests].count(what)
                    self.requests.append((number, what))
                action = self.plan.get((what, nth), "answer")
                if action == "drop":
                    break
                connection.sendall(self._answer(path, close=action == "answer, then close"))
                if action == "answer, then close":
                    break
                if action == "answer, then close idle":
                    connection.shutdown(socket.SHUT_WR)
                    self.idle_closed.set()
                    while (late := self._path_read(reader)) is not None:
                        self.after_close.append(self._what(late))
                    self.idle_read.set()
                    break
        except OSError:
            # stop() shut the connection down.
            pass
        finally:
            reader.close()
            connection.close()

    @staticmethod
    def _path_read(reader):
        """The path of the next request ``reader`` reads in full, or None at
        the connection's end."""
        line = reader.readline()
        if not line:
            return None
        length = 0
        while (header := reader.readline()) not in (b"\r\n", b""):
            name, _, value = header.partition(b":")
            if name.strip().lower() == b"content-length":
                length = int(value)
        reader.read(length)
        return line.split()[1].decode("ascii")

    @staticmethod
    def _what(path):
        return "login" if path == "/xmlrpc/2/common" else "call"

    def _answer(self, path, close):
        if path.startswith("/xmlrpc/"):
            value = UID if self._what(path) == "login" else self.ANSWER
            body = xmlrpc.client.dumps((value,), methodresponse=True).encode()
            kind = "text/xml"
        else:
            body = json.dumps(self.ANSWER).encode()
            kind = "application/json"
        head = f"HTTP/1.1 200 OK\r\nContent-Type: {kind}\r\nContent-Length: {len(body)}\r\n"
        if close:
            head += "Connection: close\r\n"
        return f"{head}\r\n".encode() + body

    def stop(self):
        self._stopping.set()
        self._threads[0].join()
        self._listener.close()
        with self._lock:
            connections = list(self._connections)
        for connection in connections:
            # A thread still reading its connection then reads its end; one
            # its own thread closed raises.
            with contextlib.suppress(OSError):
                connection.shutdown(socket.SHUT_RDWR)
        for thread in self._threads[1:]:
            thread.join(5)


def xmlrpc_answer(value):
    """An XML-RPC answer whose one value is ``value``, written as XML."""
    return (
        "<?xml version='1.0'?><methodResponse><params><param><value>"
        f"{value}</value></param></params></methodResponse>"
    ).encode()


def odoo_error(name, message):
    """The body of an error Odoo's JSON-2 dispatcher sends."""
    return {
        "name": name,
        "message": message,
        "arguments": [message],
        "context": {},
        "debug": "Traceback (most recent call last): ...",
    }
