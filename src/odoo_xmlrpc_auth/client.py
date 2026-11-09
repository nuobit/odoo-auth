import configparser
import decimal
import functools
import http.client
import json
import os
import re
import stat
import urllib.parse
import xml.parsers.expat
import xmlrpc.client
import zlib

import requests

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
# short or corrupt. A body that is not gzip at all is an OSError.
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
    """Raised by Client's XML-RPC transports for an answer that is not
    XML-RPC, whatever their parser raised for it: a RequestError stands for
    it as xmlrpc.client's ResponseError."""


# What a call raises in its transport: the socket's and TLS's errors, a
# malformed HTTP answer, an HTTP status other than 200, every error of
# requests, which are OSErrors, and an answer that is not XML-RPC. An Odoo
# Fault is the server's answer, not one of them.
_TRANSPORT_ERRORS = (
    OSError,
    http.client.HTTPException,
    xmlrpc.client.ProtocolError,
    _InvalidAnswer,
)
# What _loaded returns for a body that is not JSON: None is JSON's null.
_NOT_JSON = object()


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
    ``request`` and ``response`` are None. For an answer that is not XML-RPC,
    Client's stands for xmlrpc.client's ResponseError, whatever the parser
    raised.
    """

    def __init__(self, profile, error_class, code=None):
        self.profile = profile
        self.error_class = error_class
        self.code = code
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
        else:
            super().__init__(str(self))

    def __str__(self):
        code = f", code {self.code}" if self.code is not None else ""
        return (
            f"A request of profile {self.profile!r} failed: {self.error_class}"
            f"{code}. Its message is not shown, as it can quote the "
            "profile's values."
        )


class OdooError(Exception):
    """Raised by Client.call when Odoo answers a call with an error: an
    XML-RPC Fault, or an error body over JSON-2.

    It carries the profile (``profile``), Odoo's code for the error
    (``code``: the Fault's faultCode over XML-RPC, the HTTP status over
    JSON-2) and Odoo's message (``message``), the text a caller debugs with,
    which shows *** wherever it held the profile's password or API key.
    Nothing of the request is kept: not its url, its headers or its
    arguments.
    """

    def __init__(self, profile, code, message):
        self.profile = profile
        self.code = code
        self.message = message
        super().__init__(str(self))

    def __str__(self):
        return (
            f"Odoo answered a call of profile {self.profile!r} with an error "
            f"(code {self.code}): {self.message}"
        )


class RedirectError(requests.RequestException):
    """Raised when an answer over JSON-2 redirects a call to another origin
    than the profile's url (another scheme, host or port), or to a location
    that cannot be read as a URL.

    The redirect is refused, whatever its status, before anything is sent
    there. Client.call raises it as a RequestError, whose ``code`` is the
    redirect's status.
    """


@functools.lru_cache(maxsize=None)
def _request_error_class(error_class):
    """The class of the RequestError raised instead of an ``error_class``: a
    subclass of both, so that ``except error_class`` keeps catching it."""
    return type(RequestError.__name__, (RequestError, error_class), {})


def _request_error(profile, error):
    """The RequestError raised instead of ``error``, one of _TRANSPORT_ERRORS."""
    if isinstance(error, xmlrpc.client.ProtocolError):
        code = error.errcode
    elif isinstance(error, requests.RequestException):
        # The status of the answer it came with, such as a redirect's.
        code = error.response.status_code if error.response is not None else None
    elif isinstance(error, OSError):
        code = error.errno
    else:
        code = None
    # An answer that is not XML-RPC stands for xmlrpc.client's own error
    # for it, as its parser's errors are no class a caller catches.
    stands_for = (
        xmlrpc.client.ResponseError
        if isinstance(error, _InvalidAnswer)
        else type(error)
    )
    return _request_error_class(stands_for)(profile, stands_for.__name__, code)


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
            f"{path} is accessible by others. Fix with: chmod {fix} {path}"
        )


def _check_api(profile, section, config_path):
    """Raise ConfigError unless ``section``, the profile's, has one of
    _ACCEPTED_API_LINES.

    A wrong value is not quoted: the line can hold the password.
    """
    if "api" not in section:
        raise ConfigError(
            f"Profile {profile!r} has no api line: add {_ACCEPTED_API_LINES} "
            f"to the [{_SECTION}] section of {config_path}"
        )
    elif section["api"] not in _APIS:
        raise ConfigError(
            f"The api line of profile {profile!r} must be "
            f"{_ACCEPTED_API_LINES}, in {config_path}. Its value is not shown."
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
                f"The {key} of profile {profile!r}, in {config_path}, holds "
                "its password. Either the password was written on the wrong "
                "line: keep it on the password line only. Or the password is "
                f"so short or so common that it occurs in the {key}: the "
                f"password must change, as the {key} may be printed. Neither "
                "is shown."
            )


def _check_url(url, config_path):
    """Raise ConfigError unless ``url`` names a host after one of
    _URL_SCHEMES, with a port from 1 to 65535 if it has one, no user or
    password and no backslash, on one line of printable ASCII without
    spaces.

    The transport quotes a url it refuses, and a url line that is wrong can
    hold a secret: no message here quotes any part of the url, not even its
    scheme. A backslash and port 0 are read one way by urllib.parse, which
    gives the profile's origin, and another by urllib3, which connects: the
    host ends at a backslash for urllib3, and it drops port 0 for the
    scheme's default.
    """
    failure = None
    try:
        parts = urllib.parse.urlsplit(url)
        # urlsplit leaves the port unchecked: reading it raises ValueError
        # for a port that is not a number.
        _ = parts.port
    except ValueError:
        # Its message can quote the url: ConfigError is raised after this block.
        failure = ConfigError(f"The url in {config_path} cannot be parsed as a URL")
    if failure is not None:
        raise failure
    # urlsplit drops line breaks and tabs, and keeps spaces and characters
    # outside ASCII, which the transport refuses with an error that quotes
    # the url.
    if not _is_one_ascii_line(url):
        raise ConfigError(
            f"The url in {config_path} must be one line of printable ASCII "
            "without spaces"
        )
    elif parts.scheme not in _URL_SCHEMES or not parts.hostname:
        raise ConfigError(
            f"The url in {config_path} must start with "
            + " or ".join(f"{scheme}://" for scheme in _URL_SCHEMES)
            + " and name a host"
        )
    elif "@" in parts.netloc:
        raise ConfigError(
            f"The url in {config_path} must have no user or password before "
            "its host"
        )
    elif "\\" in url:
        raise ConfigError(
            f"The url in {config_path} must hold no backslash: the JSON-2 "
            "transport ends the host at one, and would send the calls to "
            "another host"
        )
    elif parts.port == 0:
        raise ConfigError(
            f"The url in {config_path} must name a port from 1 to 65535, if "
            "it names one"
        )


def _check_json2(profile, config, config_path):
    """Raise ConfigError unless the values JSON-2 sends in headers can go
    there: the password is an Odoo API key, and the db one line of
    printable ASCII without spaces.

    Each is checked before any call, since the transport would refuse it
    with an error that quotes it. Neither is quoted here.
    """
    if not _API_KEY.fullmatch(config["password"].reveal()):
        raise ConfigError(
            f"The password of profile {profile!r}, in {config_path}, must be "
            "an Odoo API key, 40 lowercase hexadecimal digits: `api = json2` "
            "takes no other. It is not shown."
        )
    if not _is_one_ascii_line(config["db"]):
        raise ConfigError(
            f"The db of profile {profile!r}, in {config_path}, must be one "
            "line of printable ASCII without spaces, as JSON-2 sends it in a "
            "header. It is not shown."
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
        raise ConfigError(f"Invalid profile name: {profile}")
    config_dir = os.path.expanduser("~/.config/odoo-xmlrpc-auth")
    config_path = os.path.join(config_dir, f"{profile}.conf")

    try:
        _check_permissions(config_dir)
    except FileNotFoundError:
        pass

    parser = configparser.RawConfigParser()
    failure = None
    try:
        with open(config_path, encoding="utf-8") as f:
            _check_permissions(config_path, fd=f.fileno())
            parser.read_file(f)
    except FileNotFoundError:
        failure = ConfigError(
            f"Config file not found: {config_path}\n"
            f"Create it with a [{_SECTION}] section, the keys "
            + ", ".join(_REQUIRED_KEYS)
            + f", and the line {_ACCEPTED_API_LINES}"
        )
    except (configparser.Error, UnicodeDecodeError):
        # Their text or repr can quote the store, which holds the password.
        failure = ConfigError(
            f"{config_path} is not a valid INI file. Its lines are not "
            "shown, as they can hold the password."
        )
    # Raised in a handler, even `from None`, ConfigError would keep the
    # caught error as its __context__: it is raised after them.
    if failure is not None:
        raise failure
    if not parser.has_section(_SECTION):
        raise ConfigError(f"Missing [{_SECTION}] section in {config_path}")

    section = parser[_SECTION]
    missing = [k for k in _REQUIRED_KEYS if k not in section]
    if missing:
        raise ConfigError(
            f"Missing keys in {config_path}: {', '.join(missing)}"
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
    _check_url(section["url"], config_path)
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
    """For xmlrpc.client's transports: an answer they cannot parse as
    XML-RPC raises _InvalidAnswer, whatever their parser raised for it.
    Client's transports have it; ServerProxy keeps xmlrpc.client's own."""

    def parse_response(self, response):
        failure = None
        try:
            answer = super().parse_response(response)
        except _PARSE_ERRORS:
            # Their text can quote the answer, and their traceback's frames
            # hold the request: _InvalidAnswer, raised after this block,
            # keeps neither, nor does the RequestError raised for it.
            failure = _InvalidAnswer()
        if failure is not None:
            raise failure
        return answer


class _Transport(_AnswerParsing, xmlrpc.client.Transport):
    """xmlrpc.client's transport for http, with _AnswerParsing."""


class _SafeTransport(_AnswerParsing, xmlrpc.client.SafeTransport):
    """xmlrpc.client's transport for https, with _AnswerParsing."""


def _xmlrpc_transport(url):
    """The transport of Client's XML-RPC calls to ``url``, which _check_url
    let through: for its scheme, https or http, as xmlrpc.client picks it."""
    https = urllib.parse.urlsplit(url).scheme == "https"
    return _SafeTransport() if https else _Transport()


def _log_in(profile, config, transport=None):
    """Log in over XML-RPC with the values of ``config``, the store of
    ``profile`` as _load_config gives it, and return the uid Odoo gives.

    It goes through ``transport``, xmlrpc.client's own when None, as for
    ServerProxy. Raises RequestError instead of an error of the transport,
    and AuthError when Odoo refuses the login.
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

    When ``profile`` is not given, behaves exactly like the original.

    Usage::

        from odoo_xmlrpc_auth.client import ServerProxy

        proxy = ServerProxy(profile="customer1")
        tasks = proxy.execute_kw("project.task", "search_read",
                                 [[["project_id", "=", 5]]],
                                 {"fields": ["name"], "limit": 10})
    """

    def __init__(self, uri=None, /, *args, profile=None, **kwargs):
        if profile is not None:
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
            self._uid = _log_in(profile, config)
            # Readable as in 1.x: scripts that log in to Odoo's web
            # themselves read it. An attribute, not a local, so no local
            # shows it; but a frame that holds the proxy, as each of its
            # calls' frames does, shows it one attribute down to a debugger.
            self._password = config["password"].reveal()
            kwargs.setdefault("allow_none", True)
            uri = f"{config['url']}/xmlrpc/2/object"
        else:
            self._profile = None
            self._db = None
            self._uid = None
            self._password = None

        super().__init__(uri, *args, **kwargs)

    def execute_kw(self, *args, **kwargs):
        execute_kw = super().__getattr__("execute_kw")
        if self._profile is None:
            return execute_kw(*args, **kwargs)
        return _send(
            self._profile,
            lambda: execute_kw(self._db, self._uid, self._password, *args, **kwargs),
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
        # is no JSON it reads either.
        return _NOT_JSON


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


class _OriginSession(requests.Session):
    """A requests session whose auth sends the API key only to the origin
    of ``url``, and which follows a redirect only within that origin: a
    request or a redirect for any other raises before anything is sent
    there.

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


class _XmlRpc:
    """Client.call over XML-RPC: execute_kw on /xmlrpc/2/object, after a
    login at the first call, as ServerProxy does it."""

    def __init__(self, profile, config):
        self._profile = profile
        self._config = config
        self._uid = None
        # The login's and the calls': an answer that is not XML-RPC raises
        # _InvalidAnswer, one of _TRANSPORT_ERRORS.
        self._transport = _xmlrpc_transport(config["url"])
        self._object = xmlrpc.client.ServerProxy(
            f"{config['url']}/xmlrpc/2/object",
            allow_none=True,
            transport=self._transport,
        )

    def call(self, model, method, ids, named):
        # execute_kw takes the records as its one positional argument, and
        # none for a method of the model; everything else goes by name.
        args = [] if ids is None else [ids]
        failure = None
        try:
            # The login's Fault too, such as a db the server does not hold,
            # is an error Odoo answers.
            if self._uid is None:
                self._uid = _log_in(self._profile, self._config, self._transport)
            result = _send(
                self._profile,
                lambda: self._object.execute_kw(
                    self._config["db"],
                    self._uid,
                    self._config["password"].reveal(),
                    model,
                    method,
                    args,
                    named,
                ),
            )
        except xmlrpc.client.Fault as fault:
            failure = OdooError(
                self._profile,
                fault.faultCode,
                self._config["password"].hidden(str(fault.faultString)),
            )
        if failure is not None:
            raise failure
        return result

    def close(self):
        self._object("close")()


class _Json2:
    """Client.call over Odoo's External JSON-2 API (Odoo 19 and later): one
    POST to /json/2/<model>/<method> per call, the API key as a bearer
    token and the profile's db in the X-Odoo-Database header."""

    def __init__(self, profile, config):
        self._profile = profile
        self._url = config["url"]
        self._db = config["db"]
        self._key = config["password"]
        self._session = _OriginSession(self._url, self._key)

    def call(self, model, method, ids, named):
        # The body holds named arguments only: the records as ids, then the
        # method's own parameters (Odoo 19, addons/rpc/controllers/json2.py).
        # A value JSON cannot hold raises here, before anything is sent.
        body = json.dumps(
            named if ids is None else {"ids": ids, **named}, allow_nan=False
        )
        path = "/".join(urllib.parse.quote(name, safe="") for name in (model, method))
        response = _send(
            self._profile,
            lambda: self._session.post(
                f"{self._url}/json/2/{path}",
                data=body.encode("ascii"),
                headers={
                    "Content-Type": "application/json",
                    # Without a session, Odoo takes the database from this
                    # header when the server holds several (odoo/http.py,
                    # Request._get_session_and_dbname).
                    "X-Odoo-Database": self._db,
                },
            ),
        )
        return self._result(response)

    def _result(self, response):
        """The result ``response``, the answer to a call, carries, or the
        error it stands for."""
        status = response.status_code
        answer = _loaded(response.content)
        if status == 200:
            if answer is _NOT_JSON:
                raise _request_error_class(requests.JSONDecodeError)(
                    self._profile, "JSONDecodeError", status
                )
            return answer
        elif status == 401:
            # Odoo's answer to a key it does not take (Odoo 19,
            # odoo/addons/base/models/ir_http.py, _auth_method_bearer).
            raise AuthError(f"Authentication failed for profile {self._profile!r}")
        else:
            # The error body of Odoo's JSON-2 dispatcher is an object with
            # the message among its keys (odoo/http.py, serialize_exception);
            # any other answer is the transport's.
            if isinstance(answer, dict) and isinstance(answer.get("message"), str):
                raise OdooError(
                    self._profile, status, self._key.hidden(answer["message"])
                )
            else:
                raise _request_error_class(requests.HTTPError)(
                    self._profile, "HTTPError", status
                )

    def close(self):
        self._session.close()


class Client:
    """Calls to Odoo for one profile, by name, over the API its `api` line
    names: XML-RPC or JSON-2.

    ``Client(profile)`` reads the profile and sends nothing; over XML-RPC
    the first call logs in. Every call raises the same errors whatever the
    API: ConfigError, AuthError, RequestError, and OdooError for an error
    Odoo answers. Close it when done, or use it as a context manager.

    Usage::

        from odoo_xmlrpc_auth.client import Client

        with Client(profile="customer1") as odoo:
            companies = odoo.call(
                "res.partner", "search_read",
                domain=[["is_company", "=", True]], fields=["name"],
            )
            odoo.call("res.partner", "write", [7], vals={"name": "Example"})
    """

    def __init__(self, profile):
        config = _load_config(profile)
        self.profile = profile
        self.api = config["api"]
        # _load_config lets through only the two values of _APIS.
        transport = _XmlRpc if self.api == "xmlrpc" else _Json2
        self._transport = transport(profile, config)

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
        ``search_count``), or the list of the ids of the records a method of
        records works on (``read``, ``write``). ``named`` are the method's
        own parameters, by name, ``context`` among them.
        """
        if ids is not None and not isinstance(ids, (list, tuple)):
            raise TypeError(
                "ids must be None, for a method of the model, or a list of the "
                "records' ids"
            )
        if model in ("", ".", "..") or method in ("", ".", ".."):
            # Over JSON-2 both are segments of the call's url, and such a
            # segment would send the call to another path of the server
            # (requests drops "." and "..").
            raise ValueError(
                "A model and a method are names: neither can be empty, '.' or '..'"
            )
        return self._transport.call(
            model, method, None if ids is None else list(ids), named
        )
