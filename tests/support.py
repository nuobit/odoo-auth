"""What the tests share: a store under a temporary HOME, and stub servers of
Odoo's two APIs on 127.0.0.1, each on a port the system picks.

No test reaches a real server or a real store. The stubs answer from a
table the test fills and record every request they get, so that a test can
check what was sent, and where.
"""

import http.server
import json
import os
import socket
import tempfile
import threading
import xmlrpc.client
import xmlrpc.server
from unittest import mock

# Dummy values of the stores; never a real credential. KEY has the shape of
# an Odoo API key, 40 lowercase hexadecimal digits.
KEY = "feedface" * 5
PASSWORD = "odoo_dummy_password_test"
DB = "odoo_db_test"
USER = "odoo_user_test"
UID = 7


class StoreMixin:
    """A store under a temporary HOME, for a unittest.TestCase."""

    def use_home(self):
        home = tempfile.TemporaryDirectory()
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
    raised before setting what its repr reads (pytest prints a placeholder
    instead)."""
    try:
        return repr(local)
    except AttributeError:
        return ""


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

    ``authenticate`` gives UID for DB, USER and ``password`` (PASSWORD unless
    the test sets another), False otherwise. ``execute_kw`` checks the same
    db, uid and password, records the call in ``calls`` and answers from
    ``answers``: the value set for its model and method, or a Fault to
    raise.
    """

    def __init__(self):
        self.answers = {}
        self.calls = []
        self.logins = 0
        self.password = PASSWORD

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


class HttpRecorder:
    """An HTTP server that records each request it gets in ``requests``
    (method, path, headers with lower-case names, body) and answers with
    ``answer(request)``: by default 404 with an empty body."""

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
    Answer set for its model and method, or a 200 with the value set.
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
        return answer if isinstance(answer, Answer) else Answer(200, answer)


def odoo_error(name, message):
    """The body of an error Odoo's JSON-2 dispatcher sends."""
    return {
        "name": name,
        "message": message,
        "arguments": [message],
        "context": {},
        "debug": "Traceback (most recent call last): ...",
    }
