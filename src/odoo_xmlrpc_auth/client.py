import configparser
import functools
import http.client
import os
import stat
import urllib.parse
import xmlrpc.client

_SECTION = "odoo"
_REQUIRED_KEYS = ("url", "db", "user", "password")
_URL_SCHEMES = ("http", "https")
# What a call raises in its transport: the socket's and TLS's errors, a
# malformed HTTP answer, an HTTP status other than 200. An Odoo Fault is the
# server's answer, not one of them.
_TRANSPORT_ERRORS = (OSError, http.client.HTTPException, xmlrpc.client.ProtocolError)


class ConfigError(Exception):
    """Raised when a config file is missing, malformed, or insecure."""


class AuthError(Exception):
    """Raised when XML-RPC authentication fails."""


class RequestError(Exception):
    """Raised instead of the error a call raises in its transport: a name
    that does not resolve, a connection refused or dropped, TLS, a malformed
    HTTP answer, an HTTP status other than 200.

    That error's text can quote the url, so this one carries only the
    profile (``profile``), the error's class name (``error_class``) and its
    errno or HTTP status, if any (``code``). Its class is also a subclass of
    that error's class, so ``except OSError``, ``except
    xmlrpc.client.ProtocolError`` and the like keep catching it.
    """

    def __init__(self, profile, error_class, code=None):
        self.profile = profile
        self.error_class = error_class
        self.code = code
        if isinstance(self, xmlrpc.client.ProtocolError):
            # Its own fields, the status kept, without the url and the
            # server's text.
            super().__init__("", code, "", {})
        else:
            super().__init__(str(self))

    def __str__(self):
        code = f", code {self.code}" if self.code is not None else ""
        return (
            f"A request of profile {self.profile!r} failed: {self.error_class}"
            f"{code}. Its message is not shown, as it can quote the "
            "profile's values."
        )


@functools.lru_cache(maxsize=None)
def _request_error_class(error_class):
    """The class of the RequestError raised instead of an ``error_class``: a
    subclass of both, so that ``except error_class`` keeps catching it."""
    return type(RequestError.__name__, (RequestError, error_class), {})


def _request_error(profile, error):
    """The RequestError raised instead of ``error``, one of _TRANSPORT_ERRORS."""
    if isinstance(error, xmlrpc.client.ProtocolError):
        code = error.errcode
    elif isinstance(error, OSError):
        code = error.errno
    else:
        code = None
    return _request_error_class(type(error))(profile, type(error).__name__, code)


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


def _check_url(url, config_path):
    """Raise ConfigError unless ``url`` names a host after one of
    _URL_SCHEMES, with a port that is a number if it has one and no user or
    password, on one line of printable ASCII without spaces.

    The transport quotes a url it refuses, and a url line that is wrong can
    hold a secret: no message here quotes any part of the url, not even its
    scheme.
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


def _read_config(profile):
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
            f"Create it with a [{_SECTION}] section and keys: "
            + ", ".join(_REQUIRED_KEYS)
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

    config = dict(parser[_SECTION])
    missing = [k for k in _REQUIRED_KEYS if k not in config]
    if missing:
        raise ConfigError(
            f"Missing keys in {config_path}: {', '.join(missing)}"
        )

    _check_url(config["url"], config_path)
    config["url"] = config["url"].rstrip("/")
    return config


def _log_in(profile, config):
    """Log in over XML-RPC with the values of ``config``, the store of
    ``profile``, and return the uid Odoo gives.

    Raises RequestError instead of an error of the transport, and AuthError
    when Odoo refuses the login.
    """
    failure = None
    try:
        with xmlrpc.client.ServerProxy(
            f"{config['url']}/xmlrpc/2/common", allow_none=True
        ) as common:
            uid = common.authenticate(
                config["db"], config["user"], config["password"], {}
            )
    except _TRANSPORT_ERRORS as error:
        # Its text can quote the url: RequestError, raised after this
        # block, does not.
        failure = _request_error(profile, error)
    if failure is not None:
        raise failure

    if not uid:
        # The user and the db are values of the store.
        raise AuthError(f"Authentication failed for profile {profile!r}")
    return uid


def _send(profile, function, *args, **kwargs):
    """Return ``function(*args, **kwargs)``, a call of ``profile``, raising
    RequestError instead of an error of its transport."""
    failure = None
    try:
        result = function(*args, **kwargs)
    except _TRANSPORT_ERRORS as error:
        # Its text can quote the url: RequestError, raised after this
        # block, does not.
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
    transport; an Odoo Fault comes through as it is.

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
            config = _read_config(profile)
            self._profile = profile
            self._db = config["db"]
            self._uid = _log_in(profile, config)
            self._password = config["password"]
            kwargs.setdefault("allow_none", True)
            uri = f"{config['url']}/xmlrpc/2/object"
        else:
            self._profile = None
            self._db = None
            self._uid = None
            self._password = None

        super().__init__(uri, *args, **kwargs)

    def execute_kw(self, *args, **kwargs):
        if self._profile is None:
            return super().__getattr__("execute_kw")(*args, **kwargs)
        return _send(
            self._profile,
            super().__getattr__("execute_kw"),
            self._db,
            self._uid,
            self._password,
            *args,
            **kwargs,
        )
