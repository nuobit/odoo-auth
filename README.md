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

What else a 1.x caller meets:

- Python 3.10 or later, and `requests` 2.32.3 and `urllib3` 2.3.0 or
  later, which must be importable whatever the profile's API: the client
  imports them when it is imported. `pip install` brings them; a script
  that puts the source checkout on `sys.path` or `PYTHONPATH` needs them in
  its own environment. The suite runs on Python 3.11 with requests 2.34.2
  and urllib3 2.8.0, and on Python 3.13 with requests 2.32.3 and urllib3
  2.3.0; from 3.10, Python names a socket's timeout `TimeoutError`, the
  class a `RequestError` names for a timeout.
- A profile is refused too when its `url` holds a backslash or port 0, or
  when its `url`, `db`, `user` or `api` holds its `password` (§ Configure a
  profile).
- The CLI refuses a payload key other than `model`, `method`, `ids`,
  `kwargs` and `args`, where 1.x ignored it, and prints an error of Odoo as
  `Error: Odoo answered a call of profile 'x' with an error (code N):
  <message>`, the password shown as `***`, where 1.x printed the Fault's
  text alone.

## CLI usage

```
odoo-xmlrpc-auth --profile <name> '<json>'
         │                │          │
         │                │          └─ one JSON object: the call, by name
         │                └─ profile name → reads ~/.config/odoo-xmlrpc-auth/<name>.conf
         └─ calls Odoo over the API the profile names, prints the result as JSON
```

The payload is a `Client.call` (below) written as JSON:

```json
{"model": "...", "method": "...", "ids": [...], "kwargs": {...}}
```

`ids` is left out, or null, for a method of the model; `kwargs` holds the
method's own arguments, by name, and is left out, or null, for none. Any
other key is refused. The CLI calls
under `Client`'s default timeouts (below), and has no option to change
them.

### Examples

```bash
# Read a record
odoo-xmlrpc-auth --profile customer1 \
  '{"model":"res.partner","method":"read","ids":[1],"kwargs":{"fields":["name"]}}'

# Search with fields and a limit
odoo-xmlrpc-auth --profile customer1 \
  '{"model":"project.task","method":"search_read","kwargs":{"domain":[["project_id","=",5]],"fields":["name","stage_id"],"limit":10}}'

# Rename a task
odoo-xmlrpc-auth --profile customer1 \
  '{"model":"project.task","method":"write","ids":[42],"kwargs":{"vals":{"name":"Fixed"}}}'

# Create a task, on either API
odoo-xmlrpc-auth --profile customer1 \
  '{"model":"project.task","method":"create","kwargs":{"vals_list":[{"name":"Bug: login broken","project_id":42}]}}'
```

On an `api = xmlrpc` profile the payload may instead give `execute_kw`'s
positional arguments as `args`, the form before 2.0. They go as they are,
as `ServerProxy` sends them, with `kwargs` as `execute_kw`'s named
arguments, through the same login and with the same errors as a call by
name:

```bash
odoo-xmlrpc-auth --profile customer1 \
  '{"model":"project.task","method":"create","args":[{"name":"Bug: login broken","project_id":42}]}'
```

An `api = json2` profile refuses `args`, a payload cannot hold both `ids`
and `args`, and without `args` it cannot hold `ids` inside `kwargs`: the
records go in `ids`. An error is printed on stderr after `Error:`, and
the CLI exits with status 1, never with a traceback: one of the errors of
`Client` below, in both forms of the payload, a call `Client` refuses
(`ArgumentError`, such as an empty model, or NaN over JSON-2) among them,
or a payload Python's json cannot read (`invalid JSON: …`, also for an int
longer than it converts or nesting deeper than it follows), or an answer
JSON cannot write (over XML-RPC, a `dateTime`, `base64` or `bigdecimal`
value), which Odoo sends after it ran the call. The library's
own messages are one line each; an `OdooError` ends with Odoo's message,
printed as it came, which can span lines (a traceback of the server's,
for one). A value a message quotes, such as a path, the name of a type,
an error's code or a payload's key, shows quoted and escaped as Python's
`repr` writes it when it holds a line break or another character that
does not print, as a profile's name always does.

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
  `search_count`, `create`), or the list of the ids of the records a method
  of records works on (`read`, `write`, `unlink`).
- Every other argument goes by its name in Odoo 19's signature of the
  method, `context` among them. `create` takes its values as `vals_list`,
  on both APIs: `call(model, "create", vals_list=[{...}, ...])` answers the
  list of the new ids (a list needs Odoo 12 or later, below).
- The result is Odoo's. Over JSON-2, records come back as their ids and
  `None` as `None`. Over XML-RPC, a method that returns `None` makes Odoo
  fail the reply after the work is done, an `OdooError` that says
  `cannot marshal None`: read the state back before calling again.

The same code serves both APIs; only the profile's `api` line differs. The
call above goes out as:

| | `api = xmlrpc` | `api = json2` |
|---|---|---|
| Request | `execute_kw(db, uid, password, model, method, [ids], named)` on `/xmlrpc/2/object` (`[]` when `ids` is `None`; the leading arguments of eight ORM methods after the ids, below), after a login at the first call | `POST /json/2/<model>/<method>`, body `{"ids": ids, ...named}`, without `ids` when it is `None` |
| Credential | the profile's `password`: a password or an API key | the profile's `password`, which must be an API key, as `Authorization: Bearer` |
| Database | the profile's `db`, in each call | the profile's `db`, in the `X-Odoo-Database` header |

`Client(profile)` reads the profile and sends nothing; its `api` attribute
says which API it uses. Close it when done, or use it in a `with` block.

Over XML-RPC, eight ORM methods get their leading arguments positionally,
after the ids, however the call names them: `write(vals)` and
`read(fields)`, called with ids, and `create(vals_list)`, `search(domain)`,
`search_count(domain)`, `search_read(domain, fields)`,
`name_search(name, domain)` and `default_get(fields)`, called without.
These are the names Odoo 19's base gives them, the ones JSON-2 requires.
Odoo calls the database's last override of a method with the names the
client sends, and the names differ: older bases give some others (the
domain of `search` and `search_count` is `args` up to Odoo 15, the one of
`name_search` up to Odoo 18; the fields of `default_get` are `fields_list`
up to Odoo 18), so by name these calls fail on every model of those
versions; and overrides rename them (on Odoo 16, `res.company.write` and
`website.write` take `values`, not `vals`). Positionally the name does not
matter. Odoo's `call_kw` also reads `create`'s values back from its first
positional argument to shape the result, so by name it would fail once the
records are made, and roll back. The arguments go up to the last one
given: `search_read(fields=[...])` sends an empty domain, which Odoo reads
as no filter, before the fields, and `name_search(domain=[...])` an empty
name. Every other argument goes by name, as `offset`, `limit`, `order`,
`operator`, `load` and `context` do. A call that gives ids to one of the
methods of the model, or none to `write` or `read`, goes as it is written,
for Odoo to refuse. Over JSON-2 everything goes by name: Odoo binds the
body to the names of the method's signature.

`create` takes one record's values as a dict, `vals_list={...}`, on every
version, and a list of them, `vals_list=[{...}, ...]`, from Odoo 12: up to
Odoo 11 the base's `create` takes one dict. With a single dict, XML-RPC
answers the new id and JSON-2 the list of its id.

Each request of a `Client`, the XML-RPC login included, has 30 seconds to
connect and then 600 to get its answer. `timeout`, given when the `Client`
is created, changes both, as a `(connect, read)` tuple of seconds, the form
`requests` takes:

```python
with Client(profile="customer1", timeout=(10, 1800)) as odoo:
    ...
```

Each value is a number of seconds above 0 and at most
`threading.TIMEOUT_MAX`; anything else is refused with an `ArgumentError`
when the `Client` is created. Over XML-RPC the request itself goes out
under the read timeout; over JSON-2, under the connect timeout, as
`requests` sends it. When a timeout fires, the call raises a `RequestError`
that says which ([Errors](#errors)).

The connect timeout applies to each address the host's name resolves to,
in turn, as both APIs connect (`socket.create_connection` over XML-RPC,
urllib3's over JSON-2): a name with several addresses that do not answer
waits that long for each one. The name's resolution itself is under
neither timeout: it takes as long as the system's resolver takes.

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
the profile decides the API. It sets no timeout, as in 1.x: a request waits
as long as the server takes, unless the script sets one for every socket
(`socket.setdefaulttimeout`), whose error then names no timeout of `Client`.

## Errors

Every `Client` call raises the same errors, whatever the API. Each names the
profile and never quotes its password or API key, and no frame of its
traceback holds them in a local, which a test runner or a debugger can
print (`pytest -l`). `ServerProxy`'s own errors never quote the password
either, and no local of their frames shows it; but a frame that holds the
`ServerProxy`, as each of its calls' frames does, shows it one attribute
down, in `_password`, which stays readable as in 1.x. The errors:

- `ConfigError`: the profile is missing, cannot be read (a folder in its
  place, a file this user may not read), is malformed or accessible by
  others, has no valid `api` line, or holds a value its API cannot send
  (below).
- `AuthError`: Odoo refuses the profile's credentials, at the XML-RPC login
  or, over JSON-2, as an HTTP 401 for the API key.
- `RequestError`: a failure of the transport, in place of the error of
  `socket`, `ssl`, `http.client`, `xmlrpc.client` or `requests`: a name that
  does not resolve, a connection refused or dropped, TLS, an HTTP status
  other than 200 that carries no error of Odoo (such as the 404 page Odoo
  sends when `X-Odoo-Database` names no database it serves), an answer that
  is not JSON, or not XML-RPC (such as a login page), a request or a
  redirect to another origin (`InvalidURL`, `RedirectError`), a redirect to
  a location that cannot be read as a URL (`RedirectError`). It carries the
  profile (`profile`), that error's class name (`error_class`) and its
  errno or HTTP status, if any (`code`), never its text, which can quote
  the url, nor the request of `requests`, which holds the API key. It is
  also an instance of that error's class, so `except OSError`,
  `except xmlrpc.client.ProtocolError`, `except requests.ConnectionError`
  and the like keep catching it; as a `ProtocolError` it keeps its
  `errcode`, while its `url` and `errmsg` are empty. For an answer that is
  not XML-RPC it stands for `xmlrpc.client.ResponseError`, whatever the
  parser raised (an `ExpatError`, a `ValueError`…). Such an answer, and one
  that is not JSON (`JSONDecodeError`, `code` 200), came from a server that
  answered: answering a call, it may have run it, and only its answer was
  not read, so a retry would run it again. Read back what it did before
  repeating it. When one of `Client`'s
  timeouts fires, `timeout_fired` says which, `"connect"` or `"read"`
  (`None` otherwise), and so does the message. After the connect timeout
  nothing was sent. After the read timeout the server may still complete
  the call: read back what it did before repeating it. It is then an
  instance of `socket.timeout` (`TimeoutError`) over XML-RPC, and of
  `requests.ConnectTimeout`, `requests.ReadTimeout` or
  `requests.ConnectionError` over JSON-2, where `requests` reports a TLS
  handshake that times out as a `ReadTimeout`, which counts as read.
- `OdooError`: Odoo answers the call with an error, an XML-RPC `Fault`
  (the login's too, such as Odoo's for a `db` it does not hold) or a JSON-2
  error body. It carries the profile (`profile`), Odoo's code for it
  (`code`: the Fault's `faultCode`, an int as Odoo's are, or else its text,
  masked as the message, a base64 value read as UTF-8 as Odoo writes its
  texts, the message's too, and a value `str()` cannot write, nested deeper
  than it follows, as `<a list str() could not write: RecursionError>`, the
  message's too; the HTTP status over JSON-2) and
  Odoo's message (`message`), which shows `***` wherever it held the
  profile's password or API key: as it is, or escaped as Python's `repr`
  or `ascii` writes it, between either quotes, or its UTF-8 bytes, or as
  JSON writes it, and each of these inside any number of reprs of a text
  that holds it (a `repr` of a `repr`). Another encoder over a `repr`,
  such as the JSON of a text a `repr` wrote, is not covered: such a
  message can show them. Nor is a base64 value that holds the password
  or the API key in another encoding than UTF-8: it is read as UTF-8, as
  Odoo writes its texts, and can show it as another text, such as `pw_é`
  for the Latin-1 bytes of `pw_Ã©`.
- `ArgumentError`: a call `Client` cannot send, refused before anything is
  sent, the login included: `ids` that are not a list, a model or a method
  that is not a name (not a string, empty, `.` or `..`, or holding a
  character UTF-8 cannot encode, a lone surrogate), or a value its API
  cannot carry
  (over XML-RPC, a type `xmlrpc.client` cannot marshal, such as a set or a
  date, or an int beyond 32 bits; over JSON-2, a value JSON cannot hold,
  NaN among them; over either, a value nested deeper than the encoder
  follows). It carries
  the profile (`profile`) and the encoder's own text (`reason`), masked as
  above, and is also an instance of the `TypeError`, `ValueError`,
  `OverflowError` or `RecursionError` it stands for. Over XML-RPC the call's
  own values are marshalled once without the credentials before the call:
  marshalled with them, a value it cannot write would raise from frames
  that hold the password. `Client(...)` raises it too, before the profile
  is read, for a `timeout` that is not a `(connect, read)` tuple of two
  numbers (a `TypeError`) or whose values are not above 0 and at most
  `threading.TIMEOUT_MAX` (a `ValueError`).

`ServerProxy` raises `ConfigError`, `AuthError` and `RequestError`, as in
1.x; an Odoo `xmlrpc.client.Fault`, and an answer that is not XML-RPC,
come through it as xmlrpc.client raises them. So does an error of
xmlrpc.client's marshal, for a value it cannot write (a set, an int beyond
32 bits). Raw, as in 1.x, these keep the traceback of xmlrpc.client, whose
frames hold the call's arguments, the password among them: a test runner
or a debugger that prints each frame's locals (`pytest -l`) shows it.
`Client` raises none of them raw. The CLI, which goes through
`Client`, prints each of its errors on one line but an `OdooError`, whose
message ends with Odoo's, as it came (§ CLI usage).

The library catches no `KeyboardInterrupt`, no `MemoryError`, and no
`RecursionError` raised because the caller's own stack is already close
to Python's recursion limit: they pass through as they are, and frames
of their traceback hold the password or the API key, alone or in the
request, as those of `ServerProxy`'s raw errors do. A library does not
intercept an interrupt or the exhaustion of a resource, and their
tracebacks are how they are diagnosed. (Raised while `Client` encodes a
call's values, a `RecursionError` is an `ArgumentError`, above, even when
the caller's deep stack caused it. Raised while it reads an answer's JSON,
it is an answer that is not JSON, a `RequestError` with `code` 200, even
when the caller's deep stack caused it and the answer was good: the server
ran the call. Their class cannot tell the two causes apart.) The CLI lets
them through too:
Python prints their traceback, which shows no frame's locals.

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
or https URL that names a host, with a port from 1 to 65535 if it has one,
on one line of printable ASCII without spaces, or has a user or password
before its host, or holds a backslash. The url is not shown, not even its
scheme: xmlrpc.client would quote it, user info included, in its errors. A
backslash and port 0 are refused because the two parsers of a url read
them in different ways: requests ends the host at a backslash and sends a
port 0 to the scheme's default port, where the profile's origin is read
otherwise. An `api = json2` profile
is refused too if its `password` is not an Odoo API key (40 lowercase
hexadecimal digits) or its `db` is not one line of printable ASCII without
spaces: JSON-2 sends both in headers. Neither is shown. It is refused as
well if `requests`, which JSON-2 sends through, refuses its `url` as it
prepares a request, as for a host that starts with `*`; XML-RPC sends
through `http.client`, which takes such a host and looks it up.

A profile whose `url`, `db`, `user` or `api` holds its `password`, in any
of the forms an error is masked in (§ Errors, above), is refused as well,
with a message that names the field and shows neither:
a caller may print those values (`Client`'s repr shows the `api`). The
check fails closed, so a password short or common enough to occur in one
of them by chance is refused too, and has to change.

## Security model

| Data | Leaves your machine? |
|---|---|
| Odoo credentials | No — read locally from `~/.config/`; over JSON-2 the API key goes only to the origin of the profile's url (scheme, host and port) |
| API call parameters | Yes — sent to Odoo via XML-RPC or JSON-2 |
| API results | Yes — returned from Odoo via XML-RPC or JSON-2 |

The wrapper is like `psql` reading `~/.pgpass` — credentials stay local,
only the queries go over the wire. Over JSON-2, each request is checked as
`requests` prepared it, the url as the connection reads it: one for another
origin gets no key and is not sent, and a redirect to another origin is
refused before anything is sent there. The client takes no proxy and no
`~/.netrc` from the environment, as xmlrpc.client does not either.

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

JSON-2 goes through `requests` and not the standard library's `urllib`
because of the key: `urllib`'s redirect handler copies a request's headers,
an `Authorization` header among them, to whatever host a redirect names
(`urllib/request.py`, `HTTPRedirectHandler.redirect_request`), and gives no
hook to check where a request goes. `requests` drops `Authorization` when a
redirect changes host, and its session gives the two hooks the client uses:
the auth, called with each request as prepared, which sets the key only for
the profile's origin, and `rebuild_auth`, called on each redirect before
anything is sent, which refuses any other origin.

## Prerequisites

- **python3 >= 3.10**
- **requests >= 2.32.3** and **urllib3 >= 2.3.0**, which the client imports
  whatever the profile's API

## License

Apache License 2.0 — see [LICENSE](LICENSE) and [NOTICE](NOTICE).

Copyright (c) 2026 NuoBiT Solutions, S.L.
