import argparse
import json
import sys

from . import __version__
from .client import (
    ArgumentError,
    AuthError,
    Client,
    ConfigError,
    OdooError,
    RequestError,
    _execute_kw,
    _JSON_ERRORS,
    _one_line,
    _READ_BACK,
)

# What a payload holds: model and method, then ids and kwargs for
# Client.call, or args, execute_kw's positional arguments, which only an
# `api = xmlrpc` profile takes.
_PAYLOAD_KEYS = ("model", "method", "ids", "kwargs", "args")


def die(msg):
    print(f"Error: {msg}", file=sys.stderr)
    sys.exit(1)


def _payload(text):
    """The call ``text`` describes, as a dict, once checked as a payload,
    before the profile is read: a JSON object of the keys above, with a
    model and a method, not both ids and args, and without args, kwargs
    that are an object, or null, and hold no ids: the records go in ids,
    Client.call's own parameter. What the call holds, the library checks
    as it sends it."""
    try:
        call = json.loads(text)
    except (ValueError, RecursionError) as e:
        # A JSONDecodeError is a ValueError, and so is an int longer than
        # Python converts (4300 digits by default); a RecursionError, an
        # array or an object nested deeper than the parser follows.
        die(f"invalid JSON: {e}")
    if not isinstance(call, dict):
        die("the JSON payload must be an object")
    unknown = sorted(set(call) - set(_PAYLOAD_KEYS))
    if unknown:
        die(f"unknown keys in the JSON payload: {', '.join(map(_one_line, unknown))}")
    for key in ("model", "method"):
        if key not in call:
            die(f"missing '{key}' in JSON payload")
    if "ids" in call and "args" in call:
        die("give 'ids' or 'args' in the JSON payload, not both")
    # With args, kwargs are execute_kw's named arguments, as they are.
    if "args" not in call and call.get("kwargs") is not None:
        if not isinstance(call["kwargs"], dict):
            die("'kwargs' in the JSON payload must be an object")
        elif "ids" in call["kwargs"]:
            die("'ids' goes at the top of the JSON payload, not in 'kwargs'")
    return call


def _named(call):
    """The named arguments the payload, without args, gives Client.call:
    its kwargs; none when kwargs are left out or null, as the library reads
    them with args."""
    return call["kwargs"] if call.get("kwargs") is not None else {}


def _printed(answer):
    """``answer``, Odoo's result, as the JSON text the CLI prints, or None
    when JSON cannot write it: over XML-RPC, a dateTime, a base64 or a
    bigdecimal value, or one nested deeper than json follows.

    None, not json's error, whose frames hold the answer, which can echo
    the password: the CLI ends with its own error, raised outside them."""
    try:
        text = json.dumps(answer)
    except _JSON_ERRORS:
        text = None
    return text


def main():
    parser = argparse.ArgumentParser(
        description="Odoo call proxy, over the API the profile names — injects "
        "credentials transparently",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "--profile", required=True, help="profile name (matches <name>.conf)"
    )
    parser.add_argument(
        "payload",
        help="JSON with model and method, then ids and kwargs, or args for an "
        "`api = xmlrpc` profile",
    )
    args = parser.parse_args()
    call = _payload(args.payload)

    try:
        with Client(profile=args.profile) as odoo:
            if "args" in call:
                # The form before 2.0, for an `api = xmlrpc` profile only:
                # the library checks it and sends it as it is.
                printed = _printed(
                    _execute_kw(
                        odoo,
                        call["model"],
                        call["method"],
                        call["args"],
                        call.get("kwargs"),
                    )
                )
            else:
                printed = _printed(
                    odoo.call(
                        call["model"], call["method"], call.get("ids"), **_named(call)
                    )
                )
    except (ConfigError, AuthError, RequestError, OdooError, ArgumentError) as e:
        die(e)
    if printed is None:
        die(
            "Odoo answered the call with a value JSON cannot write, such as a "
            f"dateTime or base64 of XML-RPC: it ran the call, so {_READ_BACK}"
        )
    print(printed)
