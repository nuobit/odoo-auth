import configparser
import contextlib
import contextvars
import decimal
import functools
import http.client
import inspect
import json
import logging
import os
import re
import socket
import stat
import threading
import urllib.parse
import xml.parsers.expat
import xmlrpc.client
import zlib

import requests
import urllib3

# A Client's timeouts by default, in seconds, as (connect, read): to open
# the connection, then to wait for the answer once connected. A Client
# takes others at its creation.
_DEFAULT_TIMEOUT = (30, 600)
# What a message says to do, after the words "the call", when the server
# may have run the call: RequestError's notes below, and the CLI's message
# for an answer JSON cannot write.
_READ_BACK = "do not repeat it before reading back what it did"
# What a RequestError adds to its message when one of Client's timeouts
# fired, by its timeout_fired.
_TIMEOUT_NOTES = {
    "connect": "The connect timeout fired, before anything was sent. ",
    "read": (
        "The read timeout fired: the server may still complete the call, so "
        f"{_READ_BACK}. "
    ),
}
# What it adds when the read timeout fired at the XML-RPC login, which is
# not the call: the call goes out only once the login answered.
_LOGIN_READ_TIMEOUT_NOTE = "The read timeout fired at the login: the call was not sent. "
# What it adds when the connect timeout fired after the call started to go
# out (_cut_off): over JSON-2, on a redirect's new connection, or as a
# request goes out, which requests sends under the connect timeout.
_LATE_CONNECT_TIMEOUT_NOTE = (
    "The connect timeout fired after the call started to go out: the server "
    f"may have run the call, so {_READ_BACK}. "
)
# What it adds for an answer to a call that came with status 200 and could
# not be read (code 200).
_ANSWERED_NOTE = f"The server answered: it may have run the call, so {_READ_BACK}. "
# What it adds for any other failure of a call after the call started to go
# out (_cut_off): its connection failed, the answer's head could not be
# read, or the answer was not Odoo's own, such as a gateway's.
_CUT_OFF_NOTE = (
    "It failed after the call started to go out: the server may have run "
    f"the call, so {_READ_BACK}. "
)
_SECTION = "odoo"
_REQUIRED_KEYS = ("url", "db", "user", "password")
# The values of a profile a caller may print (Client's repr shows the api):
# none of them may hold the password.
_PUBLIC_KEYS = ("url", "db", "user", "api")
# The values of the line `api = ...` every profile carries, with no default:
# the API each call of a Client goes through, Odoo's XML-RPC (/xmlrpc/2) or
# its External JSON-2 API (/json/2, from Odoo 19).
_APIS = ("xmlrpc", "json2")
_ACCEPTED_API_LINES = " or ".join(f"`api = {api}`" for api in _APIS)
_URL_SCHEMES = ("http", "https")
# The port a url names when it names none: an origin is compared by its port.
_DEFAULT_PORTS = {"http": 80, "https": 443}
# An Odoo API key, the only password JSON-2 takes: 20 random bytes in
# lowercase hexadecimal (Odoo 19, odoo/addons/base/models/res_users.py,
# API_KEY_SIZE and ResUsersApikeys._generate).
_API_KEY = re.compile(r"[0-9a-f]{40}")
# What xmlrpc.client's parser raises for an answer that is not XML-RPC (read
# in Python 3.11's xmlrpc/client.py, the Unmarshaller and
# Transport.parse_response): text that is not XML, such as a login page; XML
# that is not an answer; a value it cannot read (an int, a double, base64);
# a boolean neither 0 nor 1, a struct key that is not a string, a fault that
# is not a struct of faultCode and faultString; a struct name without its
# value, a fault without one (IndexError, a LookupError); XML that names an
# encoding Python does not know (LookupError, from expat's handler of
# unknown encodings); a bigdecimal that is not a number; a gzip body cut
# short or corrupt; a body cut short of its Content-Length (ExpatError),
# which http.client hands over short, without raising, to xmlrpc.client's
# reads in pieces. Reading the body raises the others _AnswerParsing takes:
# http.client's IncompleteRead (an HTTPException) for a body sent in bad
# chunks or cut short in them, gzip's BadGzipFile (an OSError) for one that
# is not the gzip it says, and a socket's OSError for a connection that
# breaks off as it comes.
_PARSE_ERRORS = (
    xml.parsers.expat.ExpatError,
    xmlrpc.client.ResponseError,
    ValueError,
    TypeError,
    LookupError,
    decimal.InvalidOperation,
    EOFError,
    zlib.error,
)


class _InvalidAnswer(Exception):
    """Raised by Client's XML-RPC transports for an answer they cannot read
    as XML-RPC, whatever reading or parsing it raised: a RequestError stands
    for it as xmlrpc.client's ResponseError. ``status`` is 200 for the
    answer to a call, which xmlrpc.client reads only at that status
    (Transport.single_request), and None for the login's, after which no
    call was sent (_XmlRpc._request)."""

    def __init__(self, status=None):
        super().__init__()
        self.status = status


class _ConnectTimeout(socket.timeout):
    """Raised by Client's XML-RPC connections when the connect timeout fires:
    a RequestError stands for it as the socket's timeout it replaces."""


class _ReadTimeout(socket.timeout):
    """Raised by Client's XML-RPC transports when the read timeout fires:
    a RequestError stands for it as the socket's timeout it replaces.
    ``call`` is True for a call's request, whose RequestError says that the
    server may still complete the call (_XmlRpc._request), and False for
    the login's, which is not the call: its RequestError says that the call
    was not sent."""

    def __init__(self, call=False):
        super().__init__()
        self.call = call


class _CutOff(Exception):
    """Raised by Client's XML-RPC transports for a failure once the
    connection is open, after the request started to go out, but for the
    read timeout (_ReadTimeout) and an answer at status 200 that cannot be
    read (_InvalidAnswer): a connection that fails, or an answer whose
    status is not 200 (xmlrpc.client's ProtocolError). A RequestError stands
    for it as the error it replaces, ``stands_for``, with that error's errno
    or status, ``code``, and keeps neither its text nor its frames. ``call``
    is True for a call's request, whose RequestError says that the server
    may have run the call (_XmlRpc._request), and False for the login's,
    which is not the call."""

    def __init__(self, stands_for, code, call=False):
        super().__init__()
        self.stands_for = stands_for
        self.code = code
        self.call = call


# What a call raises in its transport: the socket's and TLS's errors, a
# malformed HTTP answer, an HTTP status other than 200, every error of
# requests, which are OSErrors, every error of urllib3, which requests
# raises as it is when its adapter does not know it (HTTPAdapter.send: under
# requests 2.34.2 and urllib3 2.3.0, the LocationParseError of a redirect
# to <origin>//x:y), an answer that is not XML-RPC, and a failure of
# Client's XML-RPC after the request started to go out (_CutOff). An Odoo
# Fault is the server's answer, not one of them. IDNA's refusal of the host,
# a UnicodeError over XML-RPC, is not either: the load refuses such a host
# with the same codec (_idna_refuses).
_TRANSPORT_ERRORS = (
    OSError,
    http.client.HTTPException,
    urllib3.exceptions.HTTPError,
    xmlrpc.client.ProtocolError,
    _InvalidAnswer,
    _CutOff,
)
# What xmlrpc.client's Marshaller raises for a value of a call it cannot
# write (read in Python 3.11's xmlrpc/client.py): a type it does not know, a
# subclass of one it knows, a dict key that is not a string, a list or a dict
# that holds itself (TypeError); an int beyond 32 bits (OverflowError); a
# value nested deeper than its recursion follows (RecursionError).
_MARSHAL_ERRORS = (TypeError, OverflowError, RecursionError)
# What json.dumps raises for a value JSON cannot hold: a type it does not
# know, a dict key it cannot write (TypeError); NaN or an infinity, a value
# that holds itself (ValueError); a value nested deeper than it follows
# (RecursionError).
_JSON_ERRORS = (TypeError, ValueError, RecursionError)
# What _loaded returns for a body that is not JSON: None is JSON's null.
_NOT_JSON = object()


def _one_line(value):
    """``value``, which a message of the library quotes (a path, the name
    of a type, a code, the text of an encoder), as the message writes it:
    as it is when each of its characters prints, else quoted and escaped as
    repr writes it, so that a line break or another control character in
    it never spreads the message over lines. A profile's name is always
    quoted so, with !r."""
    text = str(value)
    return text if text.isprintable() else repr(text)


class ConfigError(Exception):
    """Raised when a config file is missing, malformed, or insecure, or names
    an API the class that reads it does not speak."""


class AuthError(Exception):
    """Raised when Odoo refuses the profile's credentials: its XML-RPC login,
    or its API key over JSON-2 (an HTTP 401)."""


class RequestError(Exception):
    """Raised instead of the error a call raises in its transport: a name
    that does not resolve, a connection refused or dropped, TLS, a malformed
    HTTP answer, an HTTP status other than 200 that carries no error of
    Odoo, an answer that is not XML-RPC or JSON, a request or a redirect to
    another origin, a redirect to a location that cannot be read.

    That error's text can quote the url, and an error of requests keeps the
    request, which holds the API key, so this one carries only the profile
    (``profile``), the error's class name (``error_class``) and its errno or
    HTTP status, if any (``code``). Its class is also a subclass of that
    error's class, so ``except OSError``, ``except
    xmlrpc.client.ProtocolError``, ``except requests.ConnectionError`` and the
    like keep catching it; standing for an error of requests, its
    ``request`` and ``response`` are None, and standing for one of urllib3,
    it has none of that error's own fields. For an answer that cannot be
    read as XML-RPC, Client's stands for xmlrpc.client's ResponseError,
    whatever reading or parsing it raised.

    When the answer to a call came with status 200 and could not be read,
    not XML-RPC, not JSON (JSONDecodeError), cut short, sent in bad chunks,
    not the gzip it says, or broken off, ``code`` is 200, on both APIs, and
    the message says that the server may have run the call, so that a
    retry would run it again.

    When a call failed after it started to go out, even in part, on either
    API, the message says so, and that the server may have run the call,
    whatever failed, as no RequestError stands for Odoo's own answer (an
    OdooError or an AuthError does): its connection reset, closed or failing
    in TLS, the head of its answer that cannot be read, or an answer that is
    not Odoo's, such as a status other than 200 without Odoo's error body
    (a gateway's 504, a proxy's 500 or 404, where the call may not have run
    at all), or a redirect refused or followed too many times. Over JSON-2
    that holds for a redirect too, for its new connection, even one that
    fails to open, and for its body, which Client reads and drops
    (_drained_redirect): a redirect, such as a 303, can answer a request
    the server processed. A failure before anything of the call was sent
    says nothing of it (a name that does not resolve, a connection refused,
    the connect timeout, a TLS handshake that fails or is reset), nor does
    one of the XML-RPC login, which is not the call (_cut_off).

    When one of Client's timeouts fired, ``timeout_fired`` says which,
    "connect" or "read" (None otherwise), and so does the message, in place
    of the note above: after a read timeout the server may still complete
    the call, but for the read timeout of the XML-RPC login, which is not
    the call, after which the message says that the call was not sent.
    After the connect timeout the message says that nothing was sent,
    unless it fired over JSON-2 after the call started to go out, on a
    redirect's new connection or as a request goes out, which requests
    sends under the connect timeout: it then says that the server may have
    run the call. Its ``code`` is None, wherever the timeout fired.
    """

    def __init__(
        self,
        profile,
        error_class,
        code=None,
        timeout_fired=None,
        *,
        cut_off=False,
        at_login=False,
    ):
        self.profile = profile
        self.error_class = error_class
        self.code = code
        self.timeout_fired = timeout_fired
        # Whether the call failed after it started to go out (_cut_off): the
        # message says so, unless a timeout fired or an answer at status 200
        # could not be read, whose notes it gives instead.
        self._cut_off = cut_off
        # Whether it stands for the read timeout of the XML-RPC login
        # (_ReadTimeout), which is not the call.
        self._at_login = at_login
        if isinstance(self, xmlrpc.client.ProtocolError):
            # Its own fields, the status kept, without the url and the
            # server's text.
            super().__init__("", code, "", {})
        elif isinstance(self, requests.RequestException):
            # RequestException's own __init__, which sets request and
            # response to None: the next one in the class's order can take
            # other arguments (requests' JSONDecodeError takes a document
            # and a position).
            requests.RequestException.__init__(self, str(self))
        elif isinstance(self, urllib3.exceptions.HTTPError):
            # urllib3's own __init__s take what each error describes (a
            # pool, a url, a location, a count of bytes), none of which this
            # one keeps.
            Exception.__init__(self, str(self))
        else:
            super().__init__(str(self))

    def __repr__(self):
        # Its message: the class it stands for can write its repr from the
        # fields this one does not keep (urllib3's IncompleteRead).
        return f"{type(self).__name__}({str(self)!r})"

    def __str__(self):
        code = f", code {_one_line(self.code)}" if self.code is not None else ""
        if self.timeout_fired == "connect":
            # The connect timeout's own note says that nothing was sent.
            note = (
                _LATE_CONNECT_TIMEOUT_NOTE if self._cut_off else _TIMEOUT_NOTES["connect"]
            )
        elif self.timeout_fired == "read":
            # The read timeout's own note speaks of the call.
            note = (
                _LOGIN_READ_TIMEOUT_NOTE if self._at_login else _TIMEOUT_NOTES["read"]
            )
        elif self.code == 200:
            note = _ANSWERED_NOTE
        elif self._cut_off:
            note = _CUT_OFF_NOTE
        else:
            note = ""
        return (
            f"A request of profile {self.profile!r} failed: "
            f"{_one_line(self.error_class)}{code}. {note}Its message is not "
            "shown, as it can quote the profile's values."
        )


class OdooError(Exception):
    """Raised by Client.call when Odoo answers a call with an error: an
    XML-RPC Fault, or an error body over JSON-2.

    It carries the profile (``profile``), Odoo's code for the error
    (``code``: over XML-RPC the Fault's faultCode, an int as Odoo's are, or
    else its text, masked as the message; over JSON-2 the HTTP status) and
    Odoo's message (``message``), the text a caller debugs with, which shows
    *** wherever it held the profile's password or API key. A Fault's code
    or text that is a base64 value is read as UTF-8, as Odoo writes its
    texts, and one str() cannot write, nested deeper than it follows, is
    written as that (_fault_text). Nothing of the request is kept: not its
    url, its headers or its arguments.
    """

    def __init__(self, profile, code, message):
        self.profile = profile
        self.code = code
        self.message = message
        super().__init__(str(self))

    def __str__(self):
        # Odoo's message ends it as it came, over lines if it has several.
        return (
            f"Odoo answered a call of profile {self.profile!r} with an error "
            f"(code {_one_line(self.code)}): {self.message}"
        )


class ArgumentError(Exception):
    """Raised by Client.call for a call it cannot send, before anything is
    sent: ids that are not a list, a model or a method that is not a name,
    or a value its API cannot carry (over XML-RPC, a type xmlrpc.client
    cannot marshal or an int beyond 32 bits; over JSON-2, a value JSON
    cannot hold, NaN among them; over either, one nested deeper than the
    encoder follows). Raised by Client too, before the profile is read, for
    a timeout that is not a (connect, read) tuple of positive numbers, and
    by ServerProxy with a profile for verbose, which would print each
    request, the password among them: its message then says that the
    Client or the ServerProxy cannot be created (``constructor``, the
    class's name), not that a call cannot be sent.

    It carries the profile (``profile``) and the reason (``reason``), for a
    value the encoder's own text, which shows *** wherever it held the
    profile's password or API key. Its class is also a subclass of the error
    it stands for, a TypeError, a ValueError, an OverflowError or a
    RecursionError, so ``except TypeError`` and the like keep catching it.
    """

    def __init__(self, profile, reason, constructor=None):
        self.profile = profile
        self.reason = reason
        self._constructor = constructor
        super().__init__(str(self))

    def __str__(self):
        if self._constructor is not None:
            refused = (
                f"A {self._constructor} of profile {self.profile!r} cannot be created"
            )
        else:
            refused = f"A call of profile {self.profile!r} cannot be sent"
        return f"{refused}: {_one_line(self.reason)}"


class RedirectError(requests.RequestException):
    """Raised when an answer over JSON-2 redirects a call to another origin
    than the profile's url (another scheme, host or port), or to a location
    that cannot be read as a URL.

    The redirect is refused, whatever its status, before anything is sent
    there. Client.call raises it as a RequestError, whose ``code`` is the
    redirect's status.
    """


@functools.lru_cache(maxsize=None)
def _error_class(base, error_class):
    """The class of the error of the library, ``base``, raised instead of an
    ``error_class``: a subclass of both, so that ``except error_class`` keeps
    catching it."""
    return type(base.__name__, (base, error_class), {})


def _request_error(profile, error, status=None):
    """The RequestError raised instead of ``error``, one of _TRANSPORT_ERRORS.

    ``status`` is given when ``error`` was raised reading the body of an
    answer to a call, or stands for an answer the library refuses
    (_Json2._outcome), and is that answer's status: its code, as the server
    answered (an answer of XML-RPC that cannot be read gives its own,
    _InvalidAnswer). A timeout keeps its code None, wherever it fired:
    timeout_fired reports it."""
    answered = error.status if isinstance(error, _InvalidAnswer) else status
    fired = _timeout_fired(error)
    if fired is not None:
        # Also one raised with the answer whose body stopped coming, such as
        # a redirect's (_drained_redirect).
        code = None
    elif answered is not None:
        code = answered
    else:
        code = _error_code(error)
    # The library's own signals stand for the public class they are: an
    # answer that is not XML-RPC for xmlrpc.client's own error for it, as
    # its parser's errors are no class a caller catches; a timeout of
    # Client's for the socket's timeout it replaces; a connection cut off
    # for the error it replaces.
    if isinstance(error, _InvalidAnswer):
        stands_for = xmlrpc.client.ResponseError
    elif isinstance(error, (_ConnectTimeout, _ReadTimeout)):
        stands_for = socket.timeout
    elif isinstance(error, _CutOff):
        stands_for = error.stands_for
    else:
        stands_for = type(error)
    return _error_class(RequestError, stands_for)(
        profile,
        stands_for.__name__,
        code,
        fired,
        cut_off=_cut_off(error),
        # Client's XML-RPC transports raise the login's read timeout as it
        # is, and _XmlRpc._request the call's as the call's.
        at_login=isinstance(error, _ReadTimeout) and not error.call,
    )


def _error_code(error):
    """The errno or the HTTP status of ``error``, one of _TRANSPORT_ERRORS,
    if it has one, else None."""
    if isinstance(error, xmlrpc.client.ProtocolError):
        code = error.errcode
    elif isinstance(error, requests.RequestException):
        # The status of the answer it came with, such as a redirect's.
        code = error.response.status_code if error.response is not None else None
    elif isinstance(error, OSError):
        code = error.errno
    elif isinstance(error, _CutOff):
        code = error.code
    else:
        code = None
    return code


def _cut_off(error):
    """Whether ``error``, one of _TRANSPORT_ERRORS, came after the call's
    request started to go out, even in part: the server may then have run
    the call. Whatever the error is counts, as none of the transport's is
    Odoo's own answer (OdooError, AuthError): a connection that failed, an
    answer whose head cannot be read, and an answer that is not Odoo's,
    such as a status other than 200 without Odoo's error body, a gateway's
    or a proxy's, or a redirect refused or followed too many times.

    Over XML-RPC, Client's transports open the connection before a request
    goes out (_Sending): a failure to open it raises as it is, before
    anything was sent, and any failure once it is open raises _CutOff, the
    call's in the call's request (_XmlRpc._request), not in the login's,
    which is not the call. Over JSON-2, the error does not say it: urllib3
    2.3.0 and 2.8.0 raise a TLS handshake whose connection is reset as they
    raise a call cut off, a ConnectionError over their ProtocolError
    "Connection aborted.", and a TLS error once connected as a handshake
    that fails, an SSLError over a MaxRetryError. The library's connections
    mark the call as started instead, just before the first byte of one of
    its requests is sent, a redirect's included (_MarkedSending). Only a
    call over JSON-2 sets the mark, and each call has its own, from its
    start to its end (_own_mark): any other error reads it unset, that of
    a call over XML-RPC made in the middle of a call over JSON-2 included.
    """
    if isinstance(error, _CutOff):
        after_sending = error.call
    else:
        after_sending = _REQUEST_STARTED.get()
    return after_sending


def _timeout_fired(error):
    """Which of Client's timeouts ``error``, one of _TRANSPORT_ERRORS, stands
    for: "connect", "read", or None for none, as for every error of
    ServerProxy, which sets no timeout.

    Over XML-RPC, Client's transports raise _ConnectTimeout and
    _ReadTimeout, and a request goes out under the read timeout. Over
    JSON-2, requests raises ConnectTimeout and ReadTimeout, and a
    ConnectionError over the socket's timeout in two more cases. Over
    urllib3's ReadTimeoutError, when an answer's body stops coming
    (requests' Response.iter_content; a redirect's, _drained_redirect): the
    read timeout's. Over urllib3's ProtocolError "Connection aborted.",
    with no ReadTimeoutError, when the request stops going out: the connect
    timeout's, under which requests sends a request, giving the socket the
    read timeout only once it went out whole (urllib3's
    HTTPConnectionPool._make_request). After the call started to go out,
    the connect timeout's message says so (_cut_off), and that the server
    may have run the call. requests also reports a TLS handshake that times
    out as a ReadTimeout (urllib3 2.8's HTTPConnectionPool._make_request),
    which counts as read: its note says what is safe, not what is sure.
    """
    if isinstance(error, (_ConnectTimeout, requests.ConnectTimeout)):
        fired = "connect"
    elif isinstance(error, (_ReadTimeout, requests.ReadTimeout)):
        fired = "read"
    elif isinstance(error, requests.ConnectionError) and _over(error, socket.timeout):
        if _over(error, urllib3.exceptions.ReadTimeoutError):
            fired = "read"
        else:
            fired = "connect"
    else:
        fired = None
    return fired


def _over(error, kind):
    """Whether ``error`` was raised over an error of ``kind``: one is among
    the errors of its chain, by __cause__ or else __context__."""
    chain = []
    cause = error.__cause__ or error.__context__
    # Python keeps a chain free of cycles when it sets __context__; a cause
    # set by hand may close one, which ends the walk too.
    while cause is not None and cause not in chain:
        chain.append(cause)
        cause = cause.__cause__ or cause.__context__
    return any(isinstance(cause, kind) for cause in chain)


def _argument_error(profile, error, causes, secret):
    """The ArgumentError raised instead of ``error``, one of ``causes``, the
    errors an encoder raises: it stands for the first of them that ``error``
    is, and gives ``error``'s text with ``secret``, a _Secret, hidden."""
    stands_for = next(cause for cause in causes if isinstance(error, cause))
    return _error_class(ArgumentError, stands_for)(profile, secret.hidden(str(error)))


# The pieces of a form of a secret, for _at_any_depth: each a run of
# backslashes, maybe empty, and the one character after it, if any.
_FORM_PIECES = re.compile(r"(\\*)([^\\]?)")


def _at_any_depth(form):
    """A regular expression for ``form``, a text in which an error writes a
    secret, and for every text that reprs write of it, any number of times:
    a repr of a text that holds it, a repr of that repr, and so on.

    Each repr doubles every backslash, and puts one more before each quote
    of its own kind: before each ' when it writes between single quotes,
    which it does unless its text holds a ' and no " (JSON puts one before
    each "). So whichever quotes each repr picks, a run of backslashes of
    ``form`` becomes a run at least as long, and a quote that follows none
    may come after a run of any length, and the expression matches any
    such run. So it also matches a few texts no repr writes, such as an
    odd run before a letter, which hold the secret's characters all the
    same and are masked too. A repr writes a character that does not print
    as an escape: for a form that holds one, the expression stands for the
    form as it is only, and the form's repr is another form (_Secret).

    A form that starts with a run, or with a quote, is matched from the
    start of a run of the text only ((?<!\\)), so that a search tries each
    run of the text once, not once from each of its backslashes.

    A run that ends the form matches the whole run of the text there, the
    backslashes of an escape that follows included, such as a \\n's: they
    are masked with it. It cannot tell its own from theirs, as the number
    of each depends on the reprs and their quotes; masking them masks more,
    never less. When the next occurrence starts right there, its leading
    run is taken too, and that occurrence is matched from the start of the
    run, inside the match before, as the search tries every start (_spans).
    """
    pattern = "".join(
        _at_any_depth_piece(run, char) for run, char in _FORM_PIECES.findall(form)
    )
    return (r"(?<!\\)" if _starts_with_a_run(form) else "") + pattern


def _at_any_depth_piece(run, char):
    """The regular expression of a piece of a form (_FORM_PIECES) at any
    depth: a run at least as long as ``run``, or of any length before a
    quote, then ``char``."""
    before = r"\\{%d,}" % len(run) if run or char in ("'", '"') else ""
    return before + re.escape(char)


def _starts_with_a_run(form):
    """Whether ``form`` starts with a run, or with a quote, which a repr can
    put a run before."""
    return form.startswith(("\\", "'", '"'))


def _spans(pattern, text):
    """The (start, end) of each match of ``pattern``, a form's expression
    (_at_any_depth), in ``text``: one from every position a match starts
    at, those inside an earlier match included, so that occurrences that
    overlap, as abcab's do in abcabcab, are each found whole. Each search
    starts one character after the start of the match before: every start
    of the text is tried once, in order, as one search tries them, and the
    search stays linear in the text's length."""
    spans = []
    match = pattern.search(text)
    while match is not None:
        spans.append(match.span())
        match = pattern.search(text, match.start() + 1)
    return spans


def _merged(spans):
    """``spans``, the (start, end) of matches sorted by their start, as the
    stretches they cover: those that overlap merged into one, in order.
    Two that only touch stay apart."""
    stretches = []
    for start, end in spans:
        if stretches and start < stretches[-1][1]:
            stretches[-1] = (stretches[-1][0], max(stretches[-1][1], end))
        else:
            stretches.append((start, end))
    return stretches


class _Secret:
    """A password or an API key of the store.

    Its repr shows only its length. A test runner or a debugger prints the
    locals of every frame of a traceback (``pytest -l``), and a frame of
    this module that holds the profile's values holds this, never the
    value: ``reveal()`` gives the value inside the expression that sends
    it, so that no frame keeps it in a variable.
    """

    __slots__ = ("_value", "_patterns")

    def __init__(self, value):
        self._value = value
        if value == "":
            # An empty value hides nothing.
            self._patterns = ()
        else:
            # The forms in which an error can write the value: as it is, as
            # Python's ascii writes it, the repr of its UTF-8 bytes, and as
            # JSON writes it, with or without its escapes of characters
            # outside ASCII.
            written = (
                value,
                ascii(value)[1:-1],
                repr(value.encode("utf-8"))[2:-1],
                json.dumps(value)[1:-1],
                json.dumps(value, ensure_ascii=False)[1:-1],
            )
            # Then each of them inside any number of reprs of the texts that
            # hold it, a repr of a repr, whichever quotes each repr picks
            # (_at_any_depth). A repr writes a character that does not print
            # as an escape, which _at_any_depth's runs do not stand for: a
            # form that holds one also has its repr as a form, the value's
            # repr among them. Another encoder over a repr, such as JSON over
            # a repr's text, is not covered. Each form once.
            forms = dict.fromkeys(
                [
                    *written,
                    *(repr(form)[1:-1] for form in written if not form.isprintable()),
                ]
            )
            # Each form is searched on its own, from every start of the text
            # (_spans), and the mask covers whatever any of the matches
            # covers (hidden). As the branches of one alternation, the first
            # branch to match takes its text, and no order of the branches is
            # right for every value: a form tried before a longer one it
            # starts cuts that one short (the ascii form of a value that ends
            # in «Ã» (U+00C3), \xc3, starts the repr of its UTF-8 bytes,
            # \xc3\x83), and the longer, tried first, can take the start of
            # the next occurrence of the shorter and leave its rest (that
            # repr, for a value that also starts with the text \x83, takes the
            # \\x83 that starts the next occurrence ascii writes). A search
            # that goes on from the end of each match misses an occurrence
            # that starts inside it: abcab's second in abcabcab, or the
            # ascii form of \x83\x83 right after the repr of its UTF-8 bytes,
            # \xc2\x83\xc2\x83\x83\x83, where the last \x83 of the bytes
            # and the first of the ascii form are an occurrence of that form
            # too, which overlaps the next. Searched from every start, each
            # is found whole.
            self._patterns = tuple(re.compile(_at_any_depth(form)) for form in forms)

    def reveal(self):
        """The value, for the call that sends it."""
        return self._value

    def hidden(self, text):
        """``text`` with the value, in any of its forms, shown as ***: once
        for each stretch that the forms' matches cover, those that overlap
        merged into one (_merged)."""
        stretches = _merged(
            sorted(span for pattern in self._patterns for span in _spans(pattern, text))
        )
        # What shows: the text before the first stretch, between each two,
        # and after the last.
        shown_starts = [0, *(end for _, end in stretches)]
        shown_ends = [*(start for start, _ in stretches), len(text)]
        return "***".join(
            text[start:end] for start, end in zip(shown_starts, shown_ends)
        )

    def occurs_in(self, text):
        """Whether ``text`` holds the value, in any of its forms."""
        return any(pattern.search(text) is not None for pattern in self._patterns)

    def __repr__(self):
        return f"<{len(self._value)} chars>"


def _is_one_ascii_line(text):
    """Whether ``text`` is one line of printable ASCII without spaces."""
    return " " not in text and text.isprintable() and text.isascii()


def _check_permissions(path, fd=None):
    mode = (os.fstat(fd) if fd is not None else os.stat(path)).st_mode
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        fix = "700" if stat.S_ISDIR(mode) else "600"
        raise ConfigError(
            f"{_one_line(path)} is accessible by others. Fix with: chmod {fix} "
            f"{_one_line(path)}"
        )


def _check_api(profile, section, config_path):
    """Raise ConfigError unless ``section``, the profile's, has one of
    _ACCEPTED_API_LINES.

    A wrong value is not quoted: the line can hold the password.
    """
    if "api" not in section:
        raise ConfigError(
            f"Profile {profile!r} has no api line: add {_ACCEPTED_API_LINES} "
            f"to the [{_SECTION}] section of {_one_line(config_path)}"
        )
    elif section["api"] not in _APIS:
        raise ConfigError(
            f"The api line of profile {profile!r} must be "
            f"{_ACCEPTED_API_LINES}, in {_one_line(config_path)}. Its value is "
            "not shown."
        )


def _check_public_fields(profile, section, password, config_path):
    """Raise ConfigError when a value of _PUBLIC_KEYS in ``section``, the
    profile's, holds ``password``, a _Secret, in any of the forms an error
    is masked in.

    A caller may print those values: a password written on the wrong line
    would show through them. The check fails closed: a password short or
    common enough to occur in one by chance is refused too. No message
    quotes a value, and no frame here holds one in a local.
    """
    for key in _PUBLIC_KEYS:
        # _check_api has refused a profile without the api line.
        if password.occurs_in(section[key]):
            raise ConfigError(
                f"The {key} of profile {profile!r}, in {_one_line(config_path)}, holds "
                "its password. Either the password was written on the wrong "
                "line: keep it on the password line only. Or the password is "
                f"so short or so common that it occurs in the {key}: the "
                f"password must change, as the {key} may be printed. Neither "
                "is shown."
            )


def _check_url(section, config_path):
    """Raise ConfigError when the url of ``section``, the profile's, has a
    fault (_url_fault), or, with `api = json2`, when requests does not send
    it to its own origin (_requests_sends_elsewhere): each API's url must be
    one its transport sends, http.client's for XML-RPC, requests' for JSON-2.

    A url line that is wrong can hold a secret, such as another account's
    password before its host: no frame of the error holds the url, and its
    message quotes no part of it.
    """
    fault = _url_fault(section["url"])
    if fault is None and section["api"] == "json2" and _requests_sends_elsewhere(
        section["url"]
    ):
        fault = (
            "must be one requests sends to the same scheme, host and port, as "
            "`api = json2` goes through it: requests refuses a host that starts "
            "with *, and writes some others otherwise, such as one with a % or a "
            "> in it"
        )
    if fault is not None:
        raise ConfigError(f"The url in {_one_line(config_path)} {fault}")


def _url_fault(url):
    """The fault of ``url``, as the end of a sentence about it, or None
    when it names a host after one of _URL_SCHEMES, with a port from 1 to
    65535 if it has one, no user or password, no backslash and no query or
    fragment, on one line of printable ASCII without spaces, and a host IDNA
    takes. A path, such as that of a server behind a reverse proxy, passes.

    The transport quotes a url it refuses: the fault quotes no part of the
    url, not even its scheme, and is returned, never raised, so that the
    frame that raises holds no url. A backslash is in no URL unencoded: RFC
    3986 has it in neither its reserved nor its unreserved characters, and
    writes it %5C. It is refused anywhere in the url, on both APIs; urllib3,
    which JSON-2 goes through, would also end the host at one in it, where
    urllib.parse, which gives the profile's origin, and http.client, which
    XML-RPC goes through, read the whole name. No connection goes to port
    0: http.client is refused there, and requests sends it to the scheme's
    default port. For `api = json2`, requests' own reading of the url is
    checked as well (_requests_sends_elsewhere); for `api = xmlrpc`, whose
    url requests never prepares, these rules are the only ones. A query or a
    fragment, even an empty one, would swallow the API's path each request
    adds at the url's end: over XML-RPC that path goes into the query, or
    after a # in the request line; over JSON-2 into the query, or it is
    dropped with the fragment, and the request goes to the url's own path.
    """
    try:
        parts = urllib.parse.urlsplit(url)
        # urlsplit leaves the port unchecked: reading it raises ValueError
        # for a port that is not a number. Its message can quote the url.
        port = parts.port
    except ValueError:
        parts = None
    if parts is None:
        fault = "cannot be parsed as a URL"
    # urlsplit drops line breaks and tabs, and keeps spaces and characters
    # outside ASCII, which the transport refuses with an error that quotes
    # the url.
    elif not _is_one_ascii_line(url):
        fault = "must be one line of printable ASCII without spaces"
    elif parts.scheme not in _URL_SCHEMES or not parts.hostname:
        fault = (
            "must start with "
            + " or ".join(f"{scheme}://" for scheme in _URL_SCHEMES)
            + " and name a host"
        )
    elif _idna_refuses(parts.hostname):
        fault = (
            "must name a host with no empty label between its dots, and none "
            "longer than 63 characters"
        )
    elif "@" in parts.netloc:
        fault = "must have no user or password before its host"
    elif "\\" in url:
        fault = "must hold no backslash, which RFC 3986 allows in a URL only as %5C"
    # A lone ? or # makes an empty query or fragment, which urlsplit's parts
    # cannot tell from none, and swallows the path all the same: the
    # characters are looked for in the url itself.
    elif "?" in url or "#" in url:
        fault = (
            "must hold no query or fragment, not even a lone ? or #: each "
            "request adds its API's path at the url's end, where either would "
            "swallow it"
        )
    elif port == 0:
        fault = "must name a port from 1 to 65535, if it names one"
    else:
        fault = None
    return fault


def _requests_sends_elsewhere(url):
    """Whether requests, as it prepares a request for ``url``, which
    _url_fault let through, refuses it, or gives it another origin than
    ``url``'s: the auth would refuse each call, as it sets the key only for
    the url's own origin (_BearerKey). requests
    refuses a host that starts with * (PreparedRequest.prepare_url), and
    writes some hosts otherwise than urllib.parse reads them: it quotes a >
    or a {, writes the % of an IPv6 zone as %25 when no escape follows it
    (%eth0), decodes a zone that starts with the escape of a letter, a digit
    or one of -._~ into that character, its % gone (%67 gives g, and so does
    %2567), and decodes %2e into a dot, as it drops port 0 and ends the host
    at a backslash.

    requests owns what it sends: it is asked, not copied. Its error quotes
    the url, and is not raised over. It looks no name up and sends
    nothing."""
    try:
        prepared = requests.PreparedRequest()
        prepared.prepare_url(url, None)
        elsewhere = _origin(prepared.url) != _origin(url)
    except requests.RequestException:
        elsewhere = True
    return elsewhere


def _idna_refuses(hostname):
    """Whether IDNA refuses ``hostname``, the host of a url of printable
    ASCII: for a label empty, or longer than 63 characters.

    Both transports encode the host with Python's IDNA codec before they
    look it up (socket.getaddrinfo over XML-RPC, urllib3's
    create_connection over JSON-2), and its refusal is a UnicodeError, or
    urllib3's LocationParseError, that quotes the host: the load refuses
    such a host, so that no call meets that refusal.
    """
    refused = False
    try:
        hostname.encode("idna")
    except UnicodeError:
        refused = True
    return refused


def _check_json2(profile, config, config_path):
    """Raise ConfigError unless the values JSON-2 sends in headers can go
    there: the password is an Odoo API key, and the db one line of
    printable ASCII without spaces.

    Each is checked before any call, since the transport would refuse it
    with an error that quotes it. Neither is quoted here.
    """
    if not _API_KEY.fullmatch(config["password"].reveal()):
        raise ConfigError(
            f"The password of profile {profile!r}, in {_one_line(config_path)}, "
            "must be an Odoo API key, 40 lowercase hexadecimal digits: "
            "`api = json2` takes no other. It is not shown."
        )
    if not _is_one_ascii_line(config["db"]):
        raise ConfigError(
            f"The db of profile {profile!r}, in {_one_line(config_path)}, must "
            "be one line of printable ASCII without spaces, as JSON-2 sends it "
            "in a header. It is not shown."
        )


def _load_config(profile):
    """The values of the store of ``profile``, checked: url, db, user, api,
    and the password as a _Secret. ConfigError otherwise."""
    return _checked_config(profile, *_read_section(profile))


def _read_section(profile):
    """The [odoo] section of the store of ``profile``, every key of it, and
    the path of its file, once the folder and the file pass their checks
    and the section holds every key of _REQUIRED_KEYS. ConfigError
    otherwise.

    The section, not a dict of it: its repr shows no value.
    """
    if os.sep in profile or profile.startswith("."):
        raise ConfigError(f"Invalid profile name: {profile!r}")
    config_dir = os.path.expanduser("~/.config/odoo-xmlrpc-auth")
    config_path = os.path.join(config_dir, f"{profile}.conf")

    parser = configparser.RawConfigParser()
    failure = None
    try:
        _check_permissions(config_dir)
        with open(config_path, encoding="utf-8") as f:
            _check_permissions(config_path, fd=f.fileno())
            parser.read_file(f)
    except FileNotFoundError:
        # The folder or the file: the file cannot exist without its folder.
        failure = ConfigError(
            f"Config file not found: {_one_line(config_path)}. "
            f"Create it with a [{_SECTION}] section, the keys "
            + ", ".join(_REQUIRED_KEYS)
            + f", and the line {_ACCEPTED_API_LINES}"
        )
    except OSError as error:
        # A file this user may not read, a folder in its place: the reason
        # the system gives, which quotes nothing of the store.
        reason = error.strerror if error.strerror is not None else type(error).__name__
        failure = ConfigError(
            f"{_one_line(config_path)} cannot be read ({_one_line(reason)}): it "
            "must be a file this user can read."
        )
    except (configparser.Error, UnicodeDecodeError):
        # Their text or repr can quote the store, which holds the password.
        failure = ConfigError(
            f"{_one_line(config_path)} is not a valid INI file. Its lines are "
            "not shown, as they can hold the password."
        )
    # Raised in a handler, even `from None`, ConfigError would keep the
    # caught error as its __context__: it is raised after them.
    if failure is not None:
        raise failure
    if not parser.has_section(_SECTION):
        raise ConfigError(
            f"Missing [{_SECTION}] section in {_one_line(config_path)}"
        )

    section = parser[_SECTION]
    missing = [k for k in _REQUIRED_KEYS if k not in section]
    if missing:
        raise ConfigError(
            f"Missing keys in {_one_line(config_path)}: {', '.join(missing)}"
        )
    return section, config_path


def _checked_config(profile, section, config_path):
    """The values of ``section``, the [odoo] section of the store of
    ``profile`` in ``config_path``, checked: url, db, user, api, and the
    password as a _Secret. ConfigError otherwise.

    The checks read the section, whose repr shows no value, and the
    password as a _Secret: no frame of a ConfigError holds the password.
    """
    _check_api(profile, section, config_path)
    password = _Secret(section["password"])
    # Before the checks whose frames hold a public value in a local.
    _check_public_fields(profile, section, password, config_path)
    _check_url(section, config_path)
    config = {
        "url": section["url"].rstrip("/"),
        "db": section["db"],
        "user": section["user"],
        "password": password,
        "api": section["api"],
    }
    # Only JSON-2 sends values of the store in headers.
    if config["api"] == "json2":
        _check_json2(profile, config, config_path)
    return config


def _read_config(profile):
    """The [odoo] section of the store of ``profile``, every key of it, as
    1.2.0 gave it: a dict with the password in clear and the url without its
    trailing slashes, once the profile passes the checks of _load_config.

    The library reads a profile with _load_config. This one is kept for the
    scripts that read a profile's values themselves, such as a web login
    that downloads a report, or a key of their own the section holds: they
    call it by this name.
    """
    section, config_path = _read_section(profile)
    config = _checked_config(profile, section, config_path)
    return {**section, "url": config["url"]}


class _AnswerParsing:
    """For xmlrpc.client's transports: an answer they cannot read as
    XML-RPC raises _InvalidAnswer, whatever reading or parsing it raised but
    the socket's timeout, the read timeout's, which goes on to _Timeouts.
    Client's transports have it; ServerProxy keeps xmlrpc.client's own.

    A connection reset while the answer comes, which the server sent after
    it ran the call, is such an answer: the call's RequestError has code
    200, and says that the server may have run it."""

    def parse_response(self, response):
        failure = None
        try:
            answer = super().parse_response(response)
        except socket.timeout:
            raise
        except (*_PARSE_ERRORS, http.client.HTTPException, OSError):
            # Their text can quote the answer, and their traceback's frames
            # hold the request: _InvalidAnswer, raised after this block,
            # keeps neither, nor does the RequestError raised for it.
            failure = _InvalidAnswer()
        if failure is not None:
            raise failure
        return answer


class _TimedConnection:
    """For http.client's connections: connect under the connection's own
    timeout, the connect timeout (the TLS handshake too, for https), then
    give the socket ``read_timeout`` for everything sent and received once
    connected. A connect that times out raises _ConnectTimeout."""

    def __init__(self, *args, read_timeout, **kwargs):
        super().__init__(*args, **kwargs)
        self._read_timeout = read_timeout

    def connect(self):
        failure = None
        try:
            super().connect()
        except socket.timeout:
            # _ConnectTimeout, raised after this block, tells the connect
            # timeout from the read timeout.
            failure = _ConnectTimeout()
        if failure is not None:
            raise failure
        self.sock.settimeout(self._read_timeout)


class _AnswerClosing:
    """For Client's http.client connections: closing one closes the answer
    it gave last too. When the connection closes after an answer,
    http.client hands that answer its socket (HTTPConnection.getresponse),
    and xmlrpc.client does not close an answer it fails to read: a
    transport that closes its connection after a failure (_Sending) then
    leaves no answer's socket open."""

    _answer = None

    def getresponse(self):
        self._answer = super().getresponse()
        return self._answer

    def close(self):
        super().close()
        if self._answer is not None:
            self._answer.close()
            self._answer = None


# Whether the library handles a call over JSON-2 in this thread or task,
# from its first request to its answer read (_Json2.call).
_IN_JSON2_CALL = contextvars.ContextVar("_IN_JSON2_CALL", default=False)


class _Json2CallRecords(logging.Filter):
    """For urllib3's loggers: drops each record they log in the thread or
    task of a call over JSON-2 while the library handles it
    (_IN_JSON2_CALL), whoever logs it, such as a signal handler or a
    finalizer that runs there meanwhile for another connection, as a record
    does not carry what would tell the library's apart; lets every other
    through as it is. Such a record of the call can carry the API key, which
    any application that configured logging would print: the pool's request
    line, at DEBUG, names a redirect's location, which can echo it, and the
    connection's warning for headers it cannot parse quotes them as the
    server sent them."""

    def filter(self, record):
        return not _IN_JSON2_CALL.get()


_JSON2_CALL_RECORDS = _Json2CallRecords()


def _filter_urllib3_loggers():
    """Give each of urllib3's loggers _JSON2_CALL_RECORDS, once. A logger's
    filter sees only the records the logger itself logs, not those of its
    children: urllib3's own would let theirs through. Each call over JSON-2
    runs it first, for a logger that a module of urllib3 imported since
    made, such as one of its contrib.

    It walks a copy of logging's dict of loggers that dict() makes, which
    holds the dict's entries as they were at one moment, and its count is
    theirs: walking it raises nothing. dict() makes the new dict, then
    copies the entries into it in C, where no code of Python runs. A
    collection of garbage, which the new dict's allocation can start, can
    run a finalizer that makes or drops a logger, as another thread may:
    before the entries are copied or after, never in the middle. dict.copy
    is not so on Python 3.11, for a dict with few deleted entries such as
    logging's: it makes the new dict after it takes the entries and before
    it reads their count, and a finalizer that drops a logger there gives
    a copy whose count is one entry short, whose walk raises RuntimeError;
    one that makes a logger, a copy whose count holds an entry the copy
    does not. Taken one by one, as a list of the dict's items takes them,
    a collection of garbage can run on the way, and with it a finalizer
    that makes a logger, as a thread that runs meanwhile can: the dict
    changes size as it is walked, and Python 3.11 raises RuntimeError.

    A logger made after dict() copied the entries misses that walk, and
    gets the filter at the next call over JSON-2. No logger through which
    urllib3 logs a call is one of them: each of its modules that logs makes
    its logger as it is imported (logging.getLogger(__name__)), the import
    of urllib3 imports every module a call goes through, its pool manager,
    pools, connections, answers and retries, before the library's first
    call, and no step of a call imports a module of urllib3 anew."""
    for name, logger in dict(logging.Logger.manager.loggerDict).items():
        of_urllib3 = name == "urllib3" or name.startswith("urllib3.")
        # A name that only names a parent holds a placeholder, no logger.
        if of_urllib3 and isinstance(logger, logging.Logger):
            logger.addFilter(_JSON2_CALL_RECORDS)


class _Quiet:
    """For every connection of http.client the library makes, Client's
    over both APIs and ServerProxy's with a profile: it prints nothing,
    whatever the process's debug level. At http.client.HTTPConnection's
    debuglevel 1, as requests' docs set it to see the wire, a connection
    prints each request's headers and body, the API key or the password
    among them."""

    debuglevel = 0


class _HTTPConnection(
    _Quiet, _AnswerClosing, _TimedConnection, http.client.HTTPConnection
):
    """http.client's connection for http, with _AnswerClosing and
    _TimedConnection."""


class _HTTPSConnection(
    _Quiet, _AnswerClosing, _TimedConnection, http.client.HTTPSConnection
):
    """http.client's connection for https, with _AnswerClosing and
    _TimedConnection."""


class _ProxyHTTPConnection(_Quiet, http.client.HTTPConnection):
    """http.client's connection for http, for ServerProxy, untimed."""


class _ProxyHTTPSConnection(_Quiet, http.client.HTTPSConnection):
    """http.client's connection for https, for ServerProxy, untimed."""


class _OwnConnections:
    """For the library's xmlrpc.client transports: xmlrpc.client's own
    make_connection (Python 3.11, Transport.make_connection and
    SafeTransport.make_connection), which keeps one connection per host for
    keep-alive, with the connection of its scheme the transport opens in
    _connect_to, one of the library's (_Quiet)."""

    def make_connection(self, host):
        if self._connection and host == self._connection[0]:
            return self._connection[1]
        chost, self._extra_headers, x509 = self.get_host_info(host)
        self._connection = host, self._connect_to(chost, x509)
        return self._connection[1]


class _Timeouts:
    """For Client's XML-RPC transports, whose connections are
    _TimedConnections under ``timeout``, a (connect, read) pair of seconds:
    the socket's timeout raised in single_request, which runs once the
    connection is open (_Sending), the read timeout's, raises _ReadTimeout.
    A timed out request is sent once, as every request of Client."""

    def __init__(self, timeout):
        super().__init__()
        self._timeout = timeout

    def single_request(self, host, handler, request_body, verbose=False):
        failure = None
        try:
            answer = super().single_request(host, handler, request_body, verbose)
        except socket.timeout:
            # Raised once connected, where the socket's timeout is the read
            # timeout. The frames of its traceback hold the request:
            # _ReadTimeout, raised after this block, keeps none of them.
            failure = _ReadTimeout()
        if failure is not None:
            raise failure
        return answer


class _Sending:
    """For Client's XML-RPC transports: how a request goes out. Once, never
    again after a failure: xmlrpc.client sends it a second time after
    http.client's RemoteDisconnected, or an OSError ECONNRESET, ECONNABORTED
    or EPIPE, on any connection (Transport.request, Python 3.11 and 3.13),
    and a server that ran the call before the connection broke off would run
    it twice. On the kept connection, unless the server closed it since its
    last answer, or wrote on it unasked: that one is closed, and the request
    goes out on a new connection. Over JSON-2, urllib3 checks a pooled
    connection so before reusing it (HTTPConnectionPool._get_conn,
    is_connection_dropped), with the check used here, wait_for_read, and
    sends a request once, under requests' Retry(0, read=False).

    The connection is open before the request starts to go out: a failure
    to open it (a name that does not resolve, a connection refused, the
    connect timeout, a TLS handshake that fails) raises as it is, before
    anything was sent, and a failure of the open connection, but for the
    read timeout, or an answer whose status is not 200 raises _CutOff: the
    server may have read the request. After any failure, whatever it
    raises, an interrupt such as Ctrl-C among them, the connection is
    closed before the error goes on, unchanged, and no socket of it is left
    open. A Fault, Odoo's own answer, read whole, keeps it, as any answer
    does. An interrupt from the moment socket.create_connection makes its
    socket to the moment that function returns it to the connection's
    connect, in the TCP connect above all, leaves that socket to the
    garbage collector: that function closes it only after an OSError, and
    the connection, whose sock http.client sets then, never got it. An
    interrupted connect goes on in the system, so the socket may end up
    connected; nothing of the request went out on it, and the next request
    goes out on a new connection. Over https, the close does not reach the
    TLS socket ssl makes on the TCP socket's descriptor either, in the few
    steps before the try that closes it after anything the handshake raises
    (SSLSocket._create, which detaches the TCP socket on the way): an
    interrupt there leaves it to the garbage collector, whose close of the
    descriptor comes later, after the connection's own when the interrupt
    came before the detach, by then perhaps another file's or socket's.

    ServerProxy keeps xmlrpc.client's own sending: it writes a request on
    its kept connection unchecked, and sends it again after those errors."""

    def request(self, host, handler, request_body, verbose=False):
        connection = self.make_connection(host)
        # Readable before a request: at its end, or holding what no request
        # asked for. No answer to this request can come on it.
        if connection.sock is not None and urllib3.util.wait_for_read(
            connection.sock, timeout=0.0
        ):
            self.close()
            connection = self.make_connection(host)
        try:
            if connection.sock is None:
                connection.connect()
            answer = self._answer(host, handler, request_body, verbose)
        except xmlrpc.client.Fault:
            # Read whole, at status 200: the connection is ready for the
            # next request.
            raise
        except BaseException:
            # Anything else leaves the connection unfit for the next request,
            # and xmlrpc.client closes it only after an Exception raised in
            # its own try. For a status other than 200 it reads the answer's
            # body, only when it has a Content-Length, then raises its
            # ProtocolError, both after that try: a body in chunks stays on
            # the connection, and an answer that closes the connection keeps
            # the socket http.client handed it. A TLS handshake that fails or is
            # interrupted leaves the TCP socket in the connection: the one
            # the ssl module detached, on which the check before reuse raises
            # ValueError (its fileno is -1), or, interrupted before ssl takes
            # it over, the plain one, which would carry the next request
            # unencrypted. The TLS socket that took the detached one's
            # descriptor over is closed by ssl itself after anything the
            # handshake raises, from Python 3.11.8 and 3.12.2
            # (SSLSocket._create): the reason the library requires 3.11.8 or
            # later, but not 3.12.0 or 3.12.1. ssl makes it a few steps before
            # the try that closes it, and an interrupt in those steps leaves
            # it to the garbage collector (_Sending). An interrupted send
            # leaves the connection in the middle of a request. The connection
            # is closed here, with the answer it handed its socket to
            # (_AnswerClosing), and the error goes on as it is.
            self.close()
            raise
        return answer

    def _answer(self, host, handler, request_body, verbose):
        """The answer to a request on the connection, open: a failure of
        the connection, but for the read timeout, and an answer whose status
        is not 200 raise _CutOff."""
        failure = None
        try:
            answer = self.single_request(host, handler, request_body, verbose)
        except socket.timeout as timeout:
            # _ReadTimeout (_Timeouts), which says so itself.
            failure = timeout
        except (
            OSError,
            http.client.HTTPException,
            xmlrpc.client.ProtocolError,
        ) as error:
            # xmlrpc.client raises its ProtocolError for an answer whose
            # status is not 200, after the request went out. Its text can
            # quote the url, and its frames hold the request: _CutOff,
            # raised after this block, keeps neither.
            failure = _CutOff(type(error), _error_code(error))
        if failure is not None:
            raise failure
        return answer


class _Transport(
    _Sending, _OwnConnections, _Timeouts, _AnswerParsing, xmlrpc.client.Transport
):
    """xmlrpc.client's transport for http, with _Sending, _Timeouts and
    _AnswerParsing."""

    def _connect_to(self, chost, x509):
        # As xmlrpc.client's Transport.make_connection, which leaves x509 out.
        connect, read = self._timeout
        return _HTTPConnection(chost, timeout=connect, read_timeout=read)


class _SafeTransport(
    _Sending, _OwnConnections, _Timeouts, _AnswerParsing, xmlrpc.client.SafeTransport
):
    """xmlrpc.client's transport for https, with _Sending, _Timeouts and
    _AnswerParsing."""

    def _connect_to(self, chost, x509):
        # As xmlrpc.client's SafeTransport.make_connection.
        connect, read = self._timeout
        return _HTTPSConnection(
            chost,
            None,
            context=self.context,
            timeout=connect,
            read_timeout=read,
            **(x509 or {}),
        )


def _xmlrpc_transport(url, timeout):
    """The transport of Client's XML-RPC calls to ``url``, which _check_url
    let through, with ``timeout``, a (connect, read) pair of seconds: for its
    scheme, https or http, as xmlrpc.client picks it."""
    https = urllib.parse.urlsplit(url).scheme == "https"
    return _SafeTransport(timeout) if https else _Transport(timeout)


class _ProxyTransport(_OwnConnections, xmlrpc.client.Transport):
    """xmlrpc.client's transport for http, for ServerProxy with a profile:
    its own in all but its connections, the library's."""

    def _connect_to(self, chost, x509):
        # As xmlrpc.client's Transport.make_connection, which leaves x509 out.
        return _ProxyHTTPConnection(chost)


class _ProxySafeTransport(_OwnConnections, xmlrpc.client.SafeTransport):
    """xmlrpc.client's transport for https, for ServerProxy with a profile:
    its own in all but its connections, the library's."""

    def _connect_to(self, chost, x509):
        # As xmlrpc.client's SafeTransport.make_connection.
        return _ProxyHTTPSConnection(chost, None, context=self.context, **(x509 or {}))


def _proxy_transport(url, arguments):
    """The transport xmlrpc.client's ServerProxy gives itself for ``url``
    and ``arguments``, ServerProxy's by name, each one not given at its
    default (Python 3.11, ServerProxy.__init__), but whose connections are
    the library's (_Quiet): for ServerProxy with a profile."""
    options = {
        "use_datetime": arguments.get("use_datetime", False),
        "use_builtin_types": arguments.get("use_builtin_types", False),
        "headers": arguments.get("headers", ()),
    }
    if urllib.parse.urlsplit(url).scheme == "https":
        transport = _ProxySafeTransport(**options, context=arguments.get("context"))
    else:
        transport = _ProxyTransport(**options)
    return transport


def _xmlrpc_params(values):
    """The <param> elements of ``values``, a tuple, as xmlrpc.client's
    ServerProxy writes them in a request, with allow_none as Client's
    calls: the <params> block of its Marshaller, without its tags. Raises
    one of _MARSHAL_ERRORS for a value the Marshaller cannot write."""
    block = xmlrpc.client.Marshaller("utf-8", allow_none=True).dumps(values)
    return block.removeprefix("<params>\n").removesuffix("</params>\n")


def _execute_kw_request(db, uid, password, params):
    """The body of the request of execute_kw with ``db``, ``uid`` and
    ``password``, then ``params``, the call's own values as _xmlrpc_params
    wrote them: the bytes ServerProxy sends for the same values (Python
    3.11, xmlrpc.client.dumps and ServerProxy.__request)."""
    request = xmlrpc.client.dumps((db, uid, password), "execute_kw", allow_none=True)
    end = "</params>\n</methodCall>\n"
    return (request.removesuffix(end) + params + end).encode(
        "utf-8", "xmlcharrefreplace"
    )


def _log_in(profile, config, transport):
    """Log in over XML-RPC with the values of ``config``, the store of
    ``profile`` as _load_config gives it, and return the uid Odoo gives.

    It goes through ``transport``, Client's or ServerProxy's. Raises
    RequestError instead of an error of the transport, and AuthError when
    Odoo refuses the login.
    """
    with xmlrpc.client.ServerProxy(
        f"{config['url']}/xmlrpc/2/common", allow_none=True, transport=transport
    ) as common:
        uid = _send(
            profile,
            lambda: common.authenticate(
                config["db"], config["user"], config["password"].reveal(), {}
            ),
        )
    if not uid:
        # The user and the db are values of the store.
        raise AuthError(f"Authentication failed for profile {profile!r}")
    return uid


def _sendable_uid(profile, uid):
    """(None, ``uid``) when ``uid``, the one Client's login of ``profile``
    got, is one every call can send back: an int from 1 to
    xmlrpc.client.MAXINT. Else (the RequestError the login raises, None).

    Odoo answers the id of a row of its users, an int of 32 bits; for any
    other answer the login raises RequestError, standing for an answer that
    is not XML-RPC's, as xmlrpc.client would refuse to marshal an int beyond
    32 bits in each call, from frames that hold the password. Returned, not
    raised: the frame that raises it holds no answer of the login's, which
    can echo the password. ServerProxy keeps the uid it gets, as in 1.x.
    """
    if type(uid) is int and 0 < uid <= xmlrpc.client.MAXINT:
        outcome = (None, uid)
    else:
        outcome = (_request_error(profile, _InvalidAnswer()), None)
    return outcome


def _send(profile, call):
    """Return ``call()``, a call of ``profile``, raising RequestError instead
    of an error of its transport.

    ``call`` takes no argument and reveals the password in the expression
    that sends it: no frame here holds the password, and so no frame the
    RequestError raised gets from here does. A caller's frame can:
    ServerProxy.execute_kw's holds the ServerProxy, which shows it one
    attribute down, in _password, readable as in 1.x.
    """
    failure = None
    try:
        result = call()
    except _TRANSPORT_ERRORS as error:
        # Its text can quote the url, and an error of requests keeps the
        # request, which holds the API key, as the frames of its traceback
        # hold the password: RequestError, raised after this block, keeps
        # none of them.
        failure = _request_error(profile, error)
    if failure is not None:
        raise failure
    return result


class ServerProxy(xmlrpc.client.ServerProxy):
    """Drop-in replacement for xmlrpc.client.ServerProxy with Odoo auth.

    When ``profile`` is given, reads credentials from the config file,
    authenticates, builds the URL automatically, and prepends
    db/uid/password to every ``execute_kw`` call. The authentication and
    each ``execute_kw`` raise RequestError instead of an error of their
    transport; an Odoo Fault comes through as it is. A profile with
    ``api = json2`` is refused with ConfigError: Client calls it.

    With a profile, its connections, the login's too, are the library's,
    which print nothing (_Quiet), unless it is given a transport, whose
    connections are the caller's; and it refuses ``verbose``, which asks
    xmlrpc.client to print each request, the password among them, with
    ArgumentError, before the profile is read.

    When ``profile`` is not given, behaves exactly like the original.

    Usage::

        from odoo_xmlrpc_auth.client import ServerProxy

        proxy = ServerProxy(profile="customer1")
        tasks = proxy.execute_kw("project.task", "search_read",
                                 [[["project_id", "=", 5]]],
                                 {"fields": ["name"], "limit": 10})
    """

    def __init__(self, *args, profile=None, **kwargs):
        if profile is not None:
            # xmlrpc.client.ServerProxy's arguments by their names, whether
            # given by name or in their places, the uri among them, which the
            # profile's url replaces and which may be left out.
            given = inspect.signature(super().__init__).bind_partial(*args, **kwargs)
            if given.arguments.get("verbose"):
                raise _error_class(ArgumentError, ValueError)(
                    profile,
                    "it takes no verbose, which would print each request, the "
                    "password among them",
                    constructor="ServerProxy",
                )
            config = _load_config(profile)
            if config["api"] != "xmlrpc":
                # The profile decides the API. Odoo 19 logs a deprecation
                # warning for each XML-RPC call, and an error for each method
                # that returns None.
                raise ConfigError(
                    f"Profile {profile!r} has `api = json2`, and ServerProxy "
                    "speaks XML-RPC only: call it through "
                    f"odoo_xmlrpc_auth.client.Client(profile={profile!r})"
                )
            self._profile = profile
            self._db = config["db"]
            # The login's, with every argument at its default, as in 1.x, and
            # a mark of its own (_own_mark), as each execute_kw has.
            with _own_mark():
                self._uid = _log_in(
                    profile, config, _proxy_transport(config["url"], {})
                )
            # Readable as in 1.x: scripts that log in to Odoo's web
            # themselves read it. An attribute, not a local, so no local
            # shows it; but a frame that holds the proxy, as each of its
            # calls' frames does, shows it one attribute down to a debugger.
            self._password = config["password"].reveal()
            given.arguments["uri"] = f"{config['url']}/xmlrpc/2/object"
            given.arguments.setdefault("allow_none", True)
            if given.arguments.get("transport") is None:
                given.arguments["transport"] = _proxy_transport(
                    config["url"], given.arguments
                )
            args, kwargs = given.args, given.kwargs
        else:
            self._profile = None
            self._db = None
            self._uid = None
            self._password = None

        # Without a profile, the arguments as they were given, so that
        # xmlrpc.client's ServerProxy takes or refuses them as it would.
        super().__init__(*args, **kwargs)

    def execute_kw(self, *args, **kwargs):
        execute_kw = super().__getattr__("execute_kw")
        if self._profile is None:
            return execute_kw(*args, **kwargs)
        with _own_mark():
            return _send(
                self._profile,
                lambda: execute_kw(
                    self._db, self._uid, self._password, *args, **kwargs
                ),
            )


def _origin(url):
    """The origin of ``url``: its scheme, its host and its port, the scheme's
    default port when it names none. None for a url that cannot be parsed,
    such as one whose port is not a number: it names no origin."""
    try:
        parts = urllib.parse.urlsplit(url)
        port = parts.port
    except ValueError:
        return None
    return (
        parts.scheme,
        parts.hostname,
        port if port is not None else _DEFAULT_PORTS.get(parts.scheme),
    )


def _loaded(content):
    """``content``, the body of an answer, parsed as JSON, or _NOT_JSON."""
    try:
        return json.loads(content)
    except (ValueError, RecursionError):
        # Its doc holds the body, which can echo the request: nothing is
        # raised over it. A body nested deeper than the parser can follow
        # is no JSON it reads either, nor is any body read from a stack
        # already close to the recursion limit: one class, RecursionError,
        # for both causes (README, § Errors).
        return _NOT_JSON


def _is_odoo_error(answer):
    """Whether ``answer``, an answer's body as _loaded gives it, is the
    body of an error of Odoo's JSON-2 dispatcher, which answers each error
    it handles with the object of serialize_exception (Odoo 19,
    odoo/http.py, Json2Dispatcher.handle_error): its name and its message
    are strings, and its arguments a list, beside its context and debug.
    A gateway's error in JSON, such as AWS API Gateway's 504, {"message":
    "Endpoint request timed out"}, is none: an answer that is not Odoo's
    own."""
    return (
        isinstance(answer, dict)
        and isinstance(answer.get("name"), str)
        and isinstance(answer.get("message"), str)
        and isinstance(answer.get("arguments"), list)
    )


class _BearerKey(requests.auth.AuthBase):
    """Puts the API key (``key``, a _Secret) in the Authorization header of
    each request the session prepares for the origin of ``url``, and of no
    other: a request for another origin raises InvalidURL before the key is
    set, and nothing is sent. Its repr shows only the key's length."""

    def __init__(self, key, url):
        self._key = key
        self._origin = _origin(url)

    def goes_to(self, url):
        """Whether the key may go to ``url``: one of its own origin."""
        return _origin(url) == self._origin

    def __call__(self, request):
        # requests calls it with the url as it prepared it, the one the
        # connection reads, after urllib3 parsed it: where urllib3 reads
        # another origin than urllib.parse read in the profile's url, the
        # request goes nowhere.
        if not self.goes_to(request.url):
            raise requests.exceptions.InvalidURL(
                "The request would go to another origin than the profile's "
                "url: the key was not set, and nothing was sent"
            )
        request.headers["Authorization"] = f"Bearer {self._key.reveal()}"
        return request

    def __repr__(self):
        return repr(self._key)


# Whether a request of the JSON-2 call this thread or task runs, a redirect's
# included, started to go out on an open connection (_MarkedSending), for
# _cut_off. It is the call's: each call, over either API, clears it as it
# starts and gives it back its value as it ends (_own_mark), so that no
# other call, and no error raised outside a call over JSON-2, reads it set.
_REQUEST_STARTED = contextvars.ContextVar("_REQUEST_STARTED", default=False)


@contextlib.contextmanager
def _own_mark():
    """Give the call that runs in the block a mark of its own
    (_REQUEST_STARTED): unset as the block starts, and given back its value
    as the block ends, also when it raises. A call made in the middle of a
    call over JSON-2 once its request went out, as a signal handler or a
    finalizer can make one, reads its own mark, and the call over JSON-2
    has its own back afterwards."""
    started = _REQUEST_STARTED.set(False)
    try:
        yield
    finally:
        _REQUEST_STARTED.reset(started)


class _MarkedSending:
    """For the library's urllib3 connections: marks the call as started
    (_REQUEST_STARTED) just before the first byte of one of its requests is
    sent, on an open connection. The mark stays until the call ends: once a
    request of the call went out, the server may have run the call, also
    when it answered with a redirect, such as a 303, which is the answer to
    a request the server processed (RFC 9110, § 15.4.4)."""

    def send(self, data):
        # http.client's send opens a closed connection itself (urllib3 opens
        # an https one before the request): it is opened here first, so that
        # the mark comes once it is open.
        if self.sock is None:
            self.connect()
        _REQUEST_STARTED.set(True)
        super().send(data)


class _Json2HTTPConnection(_MarkedSending, _Quiet, urllib3.connection.HTTPConnection):
    """urllib3's connection for http, for JSON-2, with _MarkedSending."""


class _Json2HTTPSConnection(_MarkedSending, _Quiet, urllib3.connection.HTTPSConnection):
    """urllib3's connection for https, for JSON-2, with _MarkedSending."""


class _Json2HTTPConnectionPool(urllib3.HTTPConnectionPool):
    """urllib3's pool for http, whose connections are the library's."""

    ConnectionCls = _Json2HTTPConnection


class _Json2HTTPSConnectionPool(urllib3.HTTPSConnectionPool):
    """urllib3's pool for https, whose connections are the library's."""

    ConnectionCls = _Json2HTTPSConnection


class _Json2Adapter(requests.adapters.HTTPAdapter):
    """requests' adapter, whose pool manager opens the library's pools, so
    that its connections are the library's (_Quiet)."""

    def init_poolmanager(self, *args, **kwargs):
        super().init_poolmanager(*args, **kwargs)
        self.poolmanager.pool_classes_by_scheme = {
            "http": _Json2HTTPConnectionPool,
            "https": _Json2HTTPSConnectionPool,
        }


# How much of a redirect's body _drained_redirect reads at a time: what
# requests reads of an answer's (models.py, CONTENT_CHUNK_SIZE).
_DRAIN_CHUNK = 10 * 1024
# The error of requests that Response.iter_content raises for each error of
# urllib3 as it reads an answer's body (requests 2.32.3 and 2.34.2,
# models.py), but for urllib3's DecodeError, which a body read undecoded
# cannot raise.
_BODY_ERRORS = {
    urllib3.exceptions.ProtocolError: requests.exceptions.ChunkedEncodingError,
    urllib3.exceptions.ReadTimeoutError: requests.ConnectionError,
    urllib3.exceptions.SSLError: requests.exceptions.SSLError,
}


def _drained_redirect(response, **kwargs):
    """For the session's response hook, which requests calls with each
    answer as it comes, a redirect's included, before it follows it
    (Session.send): read a redirect's body to its end, undecoded, and drop
    it. The library has no use for it.

    requests reads that body decoded as it follows the redirect
    (SessionRedirectMixin.resolve_redirects). After a decoding error it
    reads the rest undecoded, which urllib3 refuses with a RuntimeError
    once part of the body was decoded, and the error of urllib3 that
    reading raises, such as a body that stops coming, reaches the call as
    urllib3 raises it. Read here first, the body leaves nothing for
    requests to decode or read.

    An error of urllib3 as it reads is raised as the error of requests
    Response.iter_content raises for it (_BODY_ERRORS), with the redirect
    as its response: the call reports it as an error of the body of any
    answer, a body that stops coming as the read timeout's, one cut off as
    the connection's that failed after the call started to go out.
    """
    if response.is_redirect:
        cause = None
        try:
            for _ in response.raw.stream(_DRAIN_CHUNK, decode_content=False):
                pass
        except tuple(_BODY_ERRORS) as error:
            cause = error
        if cause is not None:
            stands_for = next(
                instead
                for raised, instead in _BODY_ERRORS.items()
                if isinstance(cause, raised)
            )
            # Raised over urllib3's error, as requests raises its own, so
            # that _timeout_fired and _cut_off read its chain as they read
            # theirs. The RequestError raised for it keeps none of it (_send).
            raise stands_for(cause, response=response) from cause
    # Any other answer is the call's: _Json2._outcome reads its body.


class _OriginSession(requests.Session):
    """A requests session whose auth sends the API key only to the origin
    of ``url``, and which follows a redirect only within that origin: a
    request or a redirect for any other raises before anything is sent
    there. A redirect's body is read undecoded and dropped as it comes
    (_drained_redirect).

    It takes no proxy from the environment, which for an http url would
    receive the key (xmlrpc.client, the other transport, uses none), no
    ~/.netrc and no CA bundle (REQUESTS_CA_BUNDLE, CURL_CA_BUNDLE): requests
    checks certificates against its own, where xmlrpc.client uses the
    system's store. urllib3 still reads SSLKEYLOGFILE, as Python's ssl does
    for xmlrpc.client: the file it names gets the session keys of TLS.
    """

    def __init__(self, url, key):
        super().__init__()
        self.trust_env = False
        self.auth = _BearerKey(key, url)
        self.hooks["response"].append(_drained_redirect)
        for prefix in ("http://", "https://"):
            self.mount(prefix, _Json2Adapter())

    def get_redirect_target(self, resp):
        # requests decodes a redirect's Location as UTF-8, then parses it
        # with urllib.parse, before rebuild_auth sees it: a Location it
        # cannot decode or parse raises a UnicodeDecodeError or a ValueError,
        # which are not errors of requests, and can quote it. It is parsed
        # here as requests will, and refused before.
        failure = None
        try:
            target = super().get_redirect_target(resp)
            if target is not None:
                urllib.parse.urlsplit(target)
        except ValueError:
            # A UnicodeDecodeError is a ValueError.
            failure = RedirectError(
                "A redirect to a location that is not a URL is refused",
                response=resp,
            )
        if failure is not None:
            # The answer refused here was read to its end as it came, and its
            # connection went back to the pool (_drained_redirect).
            raise failure
        return target

    def rebuild_auth(self, prepared_request, response):
        # requests calls it on each redirect with the url it would follow,
        # before it sends anything there. Within the origin, the request
        # keeps the Authorization header it was prepared with.
        if not self.auth.goes_to(prepared_request.url):
            raise RedirectError(
                "A redirect to another origin is refused", response=response
            )


# The ORM methods whose leading parameters go positionally over XML-RPC, by
# the names 19.0's base gives them (odoo/orm/models.py), in order: those of
# records, called with ids, which go first, and those of the model, called
# without. Odoo calls the database's last override of a method with the names
# the client sends, and the names differ: older bases give some others
# (search's and search_count's domain is args up to 15.0, name_search's up to
# 18.0; default_get's fields is fields_list up to 18.0), and overrides rename
# them (on 16, res.company.write and website.write take values, not vals).
# Positionally the name does not matter. Odoo's call_kw also reads create's
# values back from its first positional argument (16.0's odoo/api.py,
# _call_kw_model_create; 19.0's odoo/service/model.py, call_kw). A call that
# gives ids to a method of the model, or none to a method of records, goes as
# it is written, for Odoo to refuse.
_LEADING_ON_RECORDS = {"write": ("vals",), "read": ("fields",)}
_LEADING_ON_THE_MODEL = {
    "create": ("vals_list",),
    "search": ("domain",),
    "search_count": ("domain",),
    "search_read": ("domain", "fields"),
    "name_search": ("name", "domain"),
    "default_get": ("fields",),
}
# What a leading parameter that is not given stands for, when a later one is:
# search_read's domain, empty, which Odoo reads as no filter, and
# name_search's name, empty, the base's default in every version.
_LEADING_LEFT_OUT = {"domain": (), "name": ""}


class _XmlRpc:
    """Client.call over XML-RPC: execute_kw on /xmlrpc/2/object, after a
    login at the first call, as ServerProxy does it, every request under
    ``timeout``, a (connect, read) pair of seconds."""

    def __init__(self, profile, config, timeout):
        self._profile = profile
        self._config = config
        self._uid = None
        # The login's and the calls': an answer that is not XML-RPC raises
        # _InvalidAnswer, one of _TRANSPORT_ERRORS, and each connection takes
        # the timeouts.
        self._transport = _xmlrpc_transport(config["url"], timeout)
        # Where the calls go, as xmlrpc.client's ServerProxy reads them in
        # its url (Python 3.11, ServerProxy.__init__): the host, and the path.
        url = urllib.parse.urlsplit(f"{config['url']}/xmlrpc/2/object")
        self._host = url.netloc
        self._handler = urllib.parse.urlunsplit(("", "", *url[2:]))

    def call(self, model, method, ids, named):
        # execute_kw takes the records as its first positional argument, and
        # none for a method of the model. Then come the method's leading
        # parameters of _LEADING_ON_RECORDS or _LEADING_ON_THE_MODEL, up to
        # the last one given; everything else goes by name.
        leading = (
            _LEADING_ON_THE_MODEL if ids is None else _LEADING_ON_RECORDS
        ).get(method, ())
        given = [index for index, name in enumerate(leading) if name in named]
        positional = leading[: given[-1] + 1] if given else ()
        args = [] if ids is None else [ids]
        args += [
            named[name] if name in named else _LEADING_LEFT_OUT[name]
            for name in positional
        ]
        kwargs = {
            name: value for name, value in named.items() if name not in positional
        }
        return self.execute_kw(model, method, args, kwargs)

    def execute_kw(self, model, method, args, kwargs):
        """execute_kw on /xmlrpc/2/object with ``args`` and ``kwargs`` as
        they are, after a login at the first call: the call of ``call``, and
        of _execute_kw."""
        params = self._marshalled(model, method, args, kwargs)
        failure = None
        try:
            # The login's Fault too, such as a db the server does not hold,
            # is an error Odoo answers. The call, its login included, has a
            # mark of its own (_own_mark), which nothing over XML-RPC sets.
            with _own_mark():
                self._log_in_once()
                result = _send(
                    self._profile,
                    lambda: self._request(
                        _execute_kw_request(
                            self._config["db"],
                            self._uid,
                            self._config["password"].reveal(),
                            params,
                        )
                    ),
                )
        except xmlrpc.client.Fault as fault:
            password = self._config["password"]
            failure = OdooError(
                self._profile,
                # Odoo's codes are ints. The decoder takes any value, a text
                # among them, which can quote the password like the message.
                (
                    fault.faultCode
                    if isinstance(fault.faultCode, int)
                    else password.hidden(_fault_text(fault.faultCode))
                ),
                password.hidden(_fault_text(fault.faultString)),
            )
        if failure is not None:
            raise failure
        return result

    def _log_in_once(self):
        """Log in at the first call, for the uid every call sends. Raises
        the RequestError of a uid no call can send (_sendable_uid) from
        this frame, which holds no answer of the login's."""
        if self._uid is None:
            failure, self._uid = _sendable_uid(
                self._profile, _log_in(self._profile, self._config, self._transport)
            )
            if failure is not None:
                raise failure

    def _marshalled(self, model, method, args, kwargs):
        """The call's own values, marshalled once, before anything is sent:
        _xmlrpc_params of ``model``, ``method``, ``args`` and ``kwargs``.
        Raises ArgumentError for a value xmlrpc.client cannot marshal.

        The request carries these params as they are, after the
        credentials, whose marshal cannot fail: no value of the call is
        marshalled again, with the password in the frames, to fail there,
        such as one nested close to the limit of the recursion, which a
        deeper stack would cross. The error raised here holds no password
        in any frame.
        """
        failure = None
        try:
            params = _xmlrpc_params((model, method, args, kwargs))
        except _MARSHAL_ERRORS as error:
            failure = _argument_error(
                self._profile, error, _MARSHAL_ERRORS, self._config["password"]
            )
        if failure is not None:
            raise failure
        return params

    def _request(self, body):
        """The answer to ``body``, a request of execute_kw, as ServerProxy
        gives it (Python 3.11, ServerProxy.__request): its one value, or
        the tuple of them when it holds another number. An answer it cannot
        read raises _InvalidAnswer at status 200, the read timeout the call's
        _ReadTimeout, and any other failure after the request started to go
        out, an answer whose status is not 200 among them, the call's
        _CutOff: the call's own, which the login's are not."""
        failure = None
        try:
            answer = self._transport.request(self._host, self._handler, body)
        except _InvalidAnswer:
            failure = _InvalidAnswer(200)
        except _ReadTimeout:
            failure = _ReadTimeout(call=True)
        except _CutOff as cut_off:
            failure = _CutOff(cut_off.stands_for, cut_off.code, call=True)
        if failure is not None:
            raise failure
        return answer[0] if len(answer) == 1 else answer

    def close(self):
        self._transport.close()


def _fault_text(value):
    """``value``, a Fault's code that is no int or its text, as the text the
    mask reads: a base64 value's bytes read as UTF-8, in which Odoo writes
    its texts, each byte UTF-8 does not take as a backslash escape, as ascii
    writes it; any other value as str() writes it. str() reads a base64
    value's bytes as Latin-1 (xmlrpc.client's Binary.__str__), a form the
    mask does not take, through which a password outside ASCII would show.
    A password a base64 value holds in another encoding than UTF-8 can
    show as another text, which the mask does not take either: pw_é for
    the Latin-1 bytes of pw_Ã© (the README says so).

    It raises nothing: it runs in the block that handles the Fault, whose
    traceback's frames hold the password, and an error raised there would
    hold the Fault as its context. A value str() cannot write, nested deeper
    than it follows, such as an array of arrays the decoder reads at any
    depth (or written from a stack already close to the recursion limit:
    one class, RecursionError, for both causes), is written as that, with
    its type: <a list str() could not write: RecursionError>."""
    if isinstance(value, xmlrpc.client.Binary):
        text = value.data.decode("utf-8", "backslashreplace")
    else:
        try:
            text = str(value)
        except RecursionError:
            text = f"<a {type(value).__name__} str() could not write: RecursionError>"
    return text


class _Json2:
    """Client.call over Odoo's External JSON-2 API (Odoo 19 and later): one
    POST to /json/2/<model>/<method> per call, the API key as a bearer
    token and the profile's db in the X-Odoo-Database header, under
    ``timeout``, a (connect, read) pair of seconds, as requests takes it."""

    def __init__(self, profile, config, timeout):
        self._profile = profile
        self._url = config["url"]
        self._db = config["db"]
        self._key = config["password"]
        self._timeout = timeout
        self._session = _OriginSession(self._url, self._key)

    def call(self, model, method, ids, named):
        # The body holds named arguments only: the records as ids, then the
        # method's own parameters (Odoo 19, addons/rpc/controllers/json2.py).
        # A value JSON cannot hold is refused here, before anything is sent.
        failure = None
        try:
            body = json.dumps(
                named if ids is None else {"ids": ids, **named}, allow_nan=False
            )
        except _JSON_ERRORS as error:
            failure = _argument_error(self._profile, error, _JSON_ERRORS, self._key)
        if failure is not None:
            raise failure
        path = "/".join(urllib.parse.quote(name, safe="") for name in (model, method))
        # Nothing of this call has gone out yet: its mark is its own
        # (_own_mark), which its connections set (_MarkedSending).
        with _own_mark():
            # No logger of urllib3 logs a record of the call (_Json2CallRecords).
            _filter_urllib3_loggers()
            in_call = _IN_JSON2_CALL.set(True)
            try:
                failure, result = _send(
                    self._profile,
                    lambda: self._outcome(
                        self._session.post(
                            f"{self._url}/json/2/{path}",
                            data=body.encode("ascii"),
                            headers={
                                "Content-Type": "application/json",
                                # Without a session, Odoo takes the database
                                # from this header when the server holds
                                # several (odoo/http.py,
                                # Request._get_session_and_dbname).
                                "X-Odoo-Database": self._db,
                            },
                            timeout=self._timeout,
                            # The body is read in _outcome, where its status
                            # is known.
                            stream=True,
                        )
                    ),
                )
            finally:
                _IN_JSON2_CALL.reset(in_call)
        if failure is not None:
            raise failure
        return result

    def _outcome(self, response):
        """The outcome of ``response``, the answer to a call: (None, the
        result it carries), or (the error it stands for, None). A 401 is an
        AuthError, whatever its body, which is not read. The body of any
        other answer is read, and one that cannot be read, cut short, sent in
        bad chunks, not the gzip it says or broken off, is the error of its
        transport with the answer's status as its code (_request_error).

        Returned, not raised: the frame that raises the error holds neither
        the answer, whose request carries the key in its headers, nor its
        body, which can echo the key, unmasked.
        """
        status = response.status_code
        if status == 401:
            # Odoo's answer to a key it does not take (Odoo 19,
            # odoo/addons/base/models/ir_http.py, _auth_method_bearer). Its
            # body adds nothing to the refusal and is not read, so that one
            # that cannot be read does not stand for it. An answer left unread
            # keeps its connection until it is closed (requests' stream=True).
            response.close()
            return (
                AuthError(f"Authentication failed for profile {self._profile!r}"),
                None,
            )
        failure = None
        try:
            content = response.content
        except _TRANSPORT_ERRORS as error:
            failure = _request_error(self._profile, error, status)
        if failure is not None:
            # An answer read in part keeps its connection until it is closed
            # (requests' stream=True).
            response.close()
            return failure, None
        answer = _loaded(content)
        # An answer the library refuses is the error of requests it stands
        # for, which _request_error reads as it reads any error of the
        # transport: it keeps only its class, with the answer's status.
        if status == 200:
            if answer is _NOT_JSON:
                outcome = (
                    _request_error(
                        self._profile, requests.JSONDecodeError("", "", 0), status
                    ),
                    None,
                )
            else:
                outcome = (None, answer)
        else:
            # Odoo's own error, recognized by the keys of the object its
            # JSON-2 dispatcher answers (_is_odoo_error); any other answer,
            # a gateway's JSON with a message among them, is the transport's.
            if _is_odoo_error(answer):
                outcome = (
                    OdooError(
                        self._profile, status, self._key.hidden(answer["message"])
                    ),
                    None,
                )
            else:
                outcome = (
                    _request_error(self._profile, requests.HTTPError(), status),
                    None,
                )
        return outcome

    def close(self):
        self._session.close()


def _check_names(profile, model, method):
    """Raise ArgumentError, for a call of ``profile``, unless ``model`` and
    ``method`` are names: strings that UTF-8 can encode, neither empty, "."
    nor "..".

    Both APIs send them encoded as UTF-8. Over JSON-2 both are segments of
    the call's url, and such a segment would send the call to another path
    of the server (requests drops "." and ".."); over XML-RPC Odoo takes
    nothing but a name. Neither is quoted: it can be anything.
    """
    names = (model, method)
    if not all(isinstance(name, str) for name in names):
        raise _error_class(ArgumentError, TypeError)(
            profile, "A model and a method are names: both must be strings"
        )
    elif any(name in ("", ".", "..") for name in names):
        raise _error_class(ArgumentError, ValueError)(
            profile,
            "A model and a method are names: neither can be empty, '.' or '..'",
        )
    elif not all(_utf8_encodes(name) for name in names):
        raise _error_class(ArgumentError, ValueError)(
            profile,
            "A model and a method are names: neither can hold a character "
            "UTF-8 cannot encode, such as a lone surrogate",
        )


def _utf8_encodes(text):
    """Whether UTF-8 encodes ``text``: whether it holds no lone surrogate."""
    encodes = True
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        encodes = False
    return encodes


def _check_timeout(profile, timeout):
    """Raise ArgumentError unless ``timeout``, Client's, is a (connect, read)
    tuple of two numbers of seconds above 0 and at most the largest timeout
    Python's blocking calls take (threading.TIMEOUT_MAX): a socket refuses
    more, while it sends the call."""
    if not (
        isinstance(timeout, tuple)
        and len(timeout) == 2
        and all(
            isinstance(seconds, (int, float)) and not isinstance(seconds, bool)
            for seconds in timeout
        )
    ):
        raise _error_class(ArgumentError, TypeError)(
            profile,
            "timeout must be a (connect, read) tuple of two numbers of seconds",
            constructor="Client",
        )
    if not all(0 < seconds <= threading.TIMEOUT_MAX for seconds in timeout):
        raise _error_class(ArgumentError, ValueError)(
            profile,
            "each timeout must be a number of seconds above 0 and at most "
            "threading.TIMEOUT_MAX",
            constructor="Client",
        )


class Client:
    """Calls to Odoo for one profile, by name, over the API its `api` line
    names: XML-RPC or JSON-2.

    ``Client(profile)`` reads the profile and sends nothing: it raises
    ConfigError for a profile it refuses, and ArgumentError for a
    ``timeout`` it refuses. Over XML-RPC the first call logs in. Every call
    raises the same errors whatever the API: AuthError, RequestError,
    OdooError for an error Odoo answers, and ArgumentError for a call it
    cannot send, before anything is sent. Close it when done, or use it as
    a context manager.

    Each request, the login's too, has 30 seconds to connect and then 600
    to get its answer. ``timeout``, a (connect, read) tuple of seconds,
    changes both: ``Client(profile, timeout=(10, 1800))``. When one fires,
    the RequestError says which, and what it means for the call: after the
    read timeout of a request of the call the server may still complete the
    call, and after that of the XML-RPC login the call was not sent.

    Usage::

        from odoo_xmlrpc_auth.client import Client

        with Client(profile="customer1") as odoo:
            companies = odoo.call(
                "res.partner", "search_read",
                domain=[["is_company", "=", True]], fields=["name"],
            )
            odoo.call("res.partner", "write", [7], vals={"name": "Example"})
    """

    def __init__(self, profile, *, timeout=_DEFAULT_TIMEOUT):
        _check_timeout(profile, timeout)
        config = _load_config(profile)
        self.profile = profile
        self.api = config["api"]
        # _load_config lets through only the two values of _APIS.
        transport = _XmlRpc if self.api == "xmlrpc" else _Json2
        self._transport = transport(profile, config, timeout)

    def __repr__(self):
        return f"Client(profile={self.profile!r}, api={self.api!r})"

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()

    def close(self):
        """Close the connections the client keeps open."""
        self._transport.close()

    def call(self, model, method, /, ids=None, **named):
        """Call ``method`` of ``model`` and return Odoo's result.

        ``ids`` is None for a method of the model (``search_read``,
        ``search_count``, ``create``), or the list of the ids of the records
        a method of records works on (``read``, ``write``). ``named`` are the
        method's own parameters, by the names of Odoo 19's base, ``context``
        among them: ``create`` takes ``vals_list``, a list of values (one dict
        up to Odoo 11), and answers the new ids. Over XML-RPC the leading ones
        of eight ORM methods go positionally, so that the call works where an
        older base or an override names them otherwise (_LEADING_ON_RECORDS,
        _LEADING_ON_THE_MODEL). A call it cannot send raises ArgumentError
        before anything is sent.
        """
        if ids is not None and not isinstance(ids, (list, tuple)):
            raise _error_class(ArgumentError, TypeError)(
                self.profile,
                "ids must be None, for a method of the model, or a list of the "
                "records' ids",
            )
        _check_names(self.profile, model, method)
        return self._transport.call(
            model, method, None if ids is None else list(ids), named
        )


def _execute_kw(client, model, method, args, kwargs=None):
    """Call ``method`` of ``model`` through ``client``, a Client, with
    ``args``, the list of execute_kw's positional arguments, and ``kwargs``,
    the dict of its named ones (None for none), as they are, and return
    Odoo's result: XML-RPC's own form, ServerProxy's, which the CLI's
    ``args`` payloads take, for an `api = xmlrpc` profile only.

    Not a method of Client, whose one way to write a call is Client.call:
    it serves the CLI's ``args`` payloads only.

    An `api = json2` profile raises ConfigError, as JSON-2 takes named
    arguments only. A call it cannot send raises ArgumentError before
    anything is sent; its other errors are Client.call's.
    """
    if client.api != "xmlrpc":
        raise ConfigError(
            f"Profile {client.profile!r} has `api = json2`, and execute_kw "
            "sends XML-RPC's positional arguments: give them by name to "
            "Client.call"
        )
    elif not isinstance(args, (list, tuple)):
        raise _error_class(ArgumentError, TypeError)(
            client.profile,
            "args must be the list of execute_kw's positional arguments",
        )
    elif kwargs is not None and not isinstance(kwargs, dict):
        raise _error_class(ArgumentError, TypeError)(
            client.profile,
            "kwargs must be None or the dict of execute_kw's named arguments",
        )
    _check_names(client.profile, model, method)
    return client._transport.execute_kw(
        model, method, list(args), {} if kwargs is None else kwargs
    )
