# odoo-xmlrpc-auth

A minimal CLI proxy and Python library for calls to Odoo, over XML-RPC or
Odoo's External JSON-2 API. Its only job is to **inject credentials** so they
never appear in command history or scripts.

## Install

```bash
pip install git+https://github.com/NuoBiT/odoo-xmlrpc-auth.git
```

Update:

```bash
pip install --upgrade git+https://github.com/NuoBiT/odoo-xmlrpc-auth.git
```

Uninstall:

```bash
pip uninstall odoo-xmlrpc-auth
```

## Upgrading to 2.0

Version 2.0 reads a line every profile must have, `api = xmlrpc` or
`api = json2`, with no default: a profile without it, or with any other
value, is refused before any call. Add the line to every profile **before**
installing 2.0. `api = xmlrpc` keeps a profile working as before.

## CLI usage

```
odoo-xmlrpc-auth --profile <name> '<json>'
         │                │          │
         │                │          └─ single JSON matching execute_kw signature
         │                └─ profile name → reads ~/.config/odoo-xmlrpc-auth/<name>.conf
         └─ authenticates via XML-RPC, executes, returns JSON
```

The JSON payload mirrors Odoo's `execute_kw` directly:

```json
{"model": "...", "method": "...", "args": [...], "kwargs": {...}}
```

### Examples

```bash
# Create a task
odoo-xmlrpc-auth --profile customer1 \
  '{"model":"project.task","method":"create","args":[{"name":"Bug: login broken","project_id":42}]}'

# Search with kwargs (fields, limit)
odoo-xmlrpc-auth --profile customer1 \
  '{"model":"project.task","method":"search_read","args":[[["project_id","=",5]]],"kwargs":{"fields":["name","stage_id"],"limit":10}}'

# Read a record
odoo-xmlrpc-auth --profile customer1 \
  '{"model":"res.partner","method":"read","args":[[1]]}'
```

## Library usage

### Client: one way to write a call, for both APIs

`Client` calls Odoo by name, over the API the profile's `api` line names:

```python
from odoo_xmlrpc_auth.client import Client

with Client(profile="customer1") as odoo:
    companies = odoo.call(
        "res.partner", "search_read",
        domain=[["is_company", "=", True]], fields=["name"], limit=10,
    )
    odoo.call("res.partner", "write", [7], vals={"name": "Example"})
    partners = odoo.call("res.partner", "read", [7, 8], fields=["name"])
```

`call(model, method, ids=None, **named)`:

- `ids` is `None` for a method of the model (`search_read`,
  `search_count`), or the list of the ids of the records a method of
  records works on (`read`, `write`, `unlink`).
- Every other argument goes by its name in the method's signature,
  `context` among them.
- The result is Odoo's. Over JSON-2, records come back as their ids and
  `None` as `None`. Over XML-RPC, a method that returns `None` makes Odoo
  fail the reply after the work is done, an `OdooError` that says
  `cannot marshal None`: read the state back before calling again.

The same code serves both APIs; only the profile's `api` line differs. The
call above goes out as:

| | `api = xmlrpc` | `api = json2` |
|---|---|---|
| Request | `execute_kw(db, uid, password, model, method, [ids], named)` on `/xmlrpc/2/object` (`[]` when `ids` is `None`), after a login at the first call | `POST /json/2/<model>/<method>`, body `{"ids": ids, ...named}`, without `ids` when it is `None` |
| Credential | the profile's `password`: a password or an API key | the profile's `password`, which must be an API key, as `Authorization: Bearer` |
| Database | the profile's `db`, in each call | the profile's `db`, in the `X-Odoo-Database` header |

`Client(profile)` reads the profile and sends nothing; its `api` attribute
says which API it uses. Close it when done, or use it in a `with` block.

On an `api = xmlrpc` profile, `create` cannot be called by name: Odoo's
`call_kw` reads the values of `create` back from its first positional
argument to shape the result, so `call(model, "create", vals_list=[...])`
fails there with an `OdooError` (an `IndexError` on the server, which
creates nothing). Create through `ServerProxy.execute_kw(model, "create",
[vals_list])` on such a profile. Over JSON-2 it works.

### ServerProxy: the XML-RPC drop-in

Drop-in replacement for `xmlrpc.client.ServerProxy`, for `api = xmlrpc`
profiles. Just change the import and add `profile=`:

```python
from odoo_xmlrpc_auth.client import ServerProxy

proxy = ServerProxy(profile="customer1")

# Same as xmlrpc.client, but db/uid/password are injected automatically
tasks = proxy.execute_kw("project.task", "search_read",
                         [[["project_id", "=", 5]]],
                         {"fields": ["name", "stage_id"], "limit": 10})

task_id = proxy.execute_kw("project.task", "create",
                           [{"name": "Bug: login broken", "project_id": 42}])

proxy.execute_kw("project.task", "write",
                 [[task_id], {"name": "Fixed"}])
```

Without `profile=`, it behaves exactly like the original `xmlrpc.client.ServerProxy`.
An `api = json2` profile is refused with a `ConfigError` that names `Client`:
the profile decides the API.

## Errors

Every `Client` call raises the same errors, whatever the API. Each names the
profile and never quotes its password or API key:

- `ConfigError`: the profile is missing, malformed or accessible by
  others, has no valid `api` line, or holds a value its API cannot send
  (below).
- `AuthError`: Odoo refuses the profile's credentials, at the XML-RPC login
  or, over JSON-2, as an HTTP 401 for the API key.
- `RequestError`: a failure of the transport, in place of the error of
  `socket`, `ssl`, `http.client`, `xmlrpc.client` or `requests`: a name that
  does not resolve, a connection refused or dropped, TLS, an HTTP status
  other than 200 that carries no error of Odoo (such as the 404 page Odoo
  sends when `X-Odoo-Database` names no database it serves), an answer that
  is not JSON, a redirect to another origin. It carries the profile
  (`profile`), that error's class name (`error_class`) and its errno or HTTP
  status, if any (`code`), never its text, which can quote the url, nor the
  request of `requests`, which holds the API key. It is also an instance of
  that error's class, so `except OSError`,
  `except xmlrpc.client.ProtocolError`, `except requests.ConnectionError`
  and the like keep catching it; as a `ProtocolError` it keeps its
  `errcode`, while its `url` and `errmsg` are empty.
- `OdooError`: Odoo answers the call with an error, an XML-RPC `Fault` or a
  JSON-2 error body. It carries the profile (`profile`), Odoo's code for it
  (`code`: the Fault's `faultCode`, or the HTTP status over JSON-2) and
  Odoo's message (`message`), which shows `***` wherever it held the
  profile's password or API key.

`ServerProxy` raises `ConfigError`, `AuthError` and `RequestError`; an Odoo
`xmlrpc.client.Fault` comes through it as it is. The CLI prints
`ConfigError`, `AuthError` and `RequestError` on one line.

## Configure a profile

One file per Odoo instance, `chmod 600`:

```bash
mkdir -p ~/.config/odoo-xmlrpc-auth
chmod 700 ~/.config/odoo-xmlrpc-auth

cat > ~/.config/odoo-xmlrpc-auth/customer1.conf << 'CONF'
[odoo]
url=https://customer1.odoo.com
db=prod_db
user=user@example.com
password=your-api-key
api=json2
CONF

chmod 600 ~/.config/odoo-xmlrpc-auth/customer1.conf
```

`api` names the API every call of the profile goes through:

- `json2`, Odoo's External JSON-2 API, from Odoo 19. It takes only an Odoo
  API key, which goes in `password`.
- `xmlrpc`, for Odoo 18 and earlier. Odoo 19 to 21 still serve it, but
  deprecated, and Odoo plans its removal for Odoo 22. On Odoo 19 the server
  logs a deprecation warning for each XML-RPC call, and an error with a
  traceback for each method that returns `None`, after its work is done.

The tool refuses to run if the config directory or files are accessible by
group or others (like `ssh` does with `~/.ssh/`), or if `url` is not an http
or https URL that names a host, with a port that is a number if it has one,
on one line of printable ASCII without spaces, or has a user or password
before its host. The url is not shown, not even its scheme: xmlrpc.client
would quote it, user info included, in its errors. An `api = json2` profile
is refused too if its `password` is not an Odoo API key (40 lowercase
hexadecimal digits) or its `db` is not one line of printable ASCII without
spaces: JSON-2 sends both in headers. Neither is shown.

## Security model

| Data | Leaves your machine? |
|---|---|
| Odoo credentials | No — read locally from `~/.config/`; over JSON-2 the API key goes only to the origin of the profile's url (scheme, host and port) |
| API call parameters | Yes — sent to Odoo via XML-RPC or JSON-2 |
| API results | Yes — returned from Odoo via XML-RPC or JSON-2 |

The wrapper is like `psql` reading `~/.pgpass` — credentials stay local,
only the queries go over the wire. Over JSON-2, a redirect to another origin
is refused before anything is sent there, and the client takes no proxy and
no `~/.netrc` from the environment, as xmlrpc.client does not either.

What the transports take from the environment, and what they do not:

- Over JSON-2, no CA bundle either (`REQUESTS_CA_BUNDLE`,
  `CURL_CA_BUNDLE`): certificates are checked against the bundle of
  `requests` (certifi's), while XML-RPC checks them against the system's
  store. A server whose certificate a private CA signed, one the system
  trusts, works over XML-RPC and not over JSON-2, and the library has no
  setting for another bundle.
- Both write the session keys of TLS to the file `SSLKEYLOGFILE` names,
  when it is set: urllib3 reads it for JSON-2, Python's `ssl` for XML-RPC.
  Whoever reads that file can decrypt the traffic, the password or the key
  included.

## Prerequisites

- **python3 >= 3.8**
- **requests >= 2.27**, for JSON-2

## License

Apache License 2.0 — see [LICENSE](LICENSE) and [NOTICE](NOTICE).

Copyright (c) 2026 NuoBiT Solutions, S.L.
