import argparse
import json
import sys

from . import __version__
from .client import (
    AuthError,
    Client,
    ConfigError,
    OdooError,
    RequestError,
)

# What a payload holds: model and method, then ids and kwargs for
# Client.call, or args, execute_kw's positional arguments, which only an
# `api = xmlrpc` profile takes.
_PAYLOAD_KEYS = ("model", "method", "ids", "kwargs", "args")


def die(msg):
    print(f"Error: {msg}", file=sys.stderr)
    sys.exit(1)


def _payload(text):
    """The call ``text`` describes, as a dict, once checked."""
    try:
        call = json.loads(text)
    except json.JSONDecodeError as e:
        die(f"invalid JSON: {e}")
    if not isinstance(call, dict):
        die("the JSON payload must be an object")
    unknown = sorted(set(call) - set(_PAYLOAD_KEYS))
    if unknown:
        die(f"unknown keys in the JSON payload: {', '.join(unknown)}")
    for key in ("model", "method"):
        if key not in call:
            die(f"missing '{key}' in JSON payload")
        elif not isinstance(call[key], str):
            die(f"'{key}' in the JSON payload must be a string")
    if "ids" in call and "args" in call:
        die("give 'ids' or 'args' in the JSON payload, not both")
    # No ids, or a null one, is a method of the model.
    if call.get("ids") is not None and not isinstance(call["ids"], list):
        die("'ids' in the JSON payload must be a list")
    if "args" in call and not isinstance(call["args"], list):
        die("'args' in the JSON payload must be a list")
    if "kwargs" in call and not isinstance(call["kwargs"], dict):
        die("'kwargs' in the JSON payload must be an object")
    # In the named form, the records are the payload's ids: Client.call
    # takes no second ids by name.
    if "args" not in call and "ids" in call.get("kwargs", {}):
        die("'ids' goes at the top of the JSON payload, not in 'kwargs'")
    return call


def _execute_kw(odoo, call):
    """The payload's call with its positional args, the CLI's form before
    2.0, for an `api = xmlrpc` profile only, through the open Client: its
    login, its errors."""
    if odoo.api != "xmlrpc":
        die(
            f"'args' is for `api = xmlrpc` profiles, and profile {odoo.profile!r} "
            "has `api = json2`: give 'ids' and 'kwargs' instead"
        )
    # The package's own transport over XML-RPC, which Client.call uses too.
    return odoo._transport.execute_kw(
        call["model"], call["method"], call["args"], call.get("kwargs", {})
    )


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
                result = _execute_kw(odoo, call)
            else:
                result = odoo.call(
                    call["model"],
                    call["method"],
                    call.get("ids"),
                    **call.get("kwargs", {}),
                )
    except (ConfigError, AuthError, RequestError, OdooError) as e:
        die(e)

    print(json.dumps(result))
