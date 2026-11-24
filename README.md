# odoo-auth

A minimal CLI proxy and Python library for calls to Odoo, over XML-RPC or
Odoo's External JSON-2 API. Its only job is to **inject credentials** so they
never appear in command history or scripts.

## Install

```bash
pip install git+https://github.com/NuoBiT/odoo-auth.git
```

Update:

```bash
pip install --upgrade git+https://github.com/NuoBiT/odoo-auth.git
```

Uninstall:

```bash
pip uninstall odoo-auth
```

## Upgrading to 2.0

Version 2.0 reads a line every profile must have, `api = xmlrpc` or
`api = json2`, with no default: a profile without it, or with any other
value, is refused before any call. Add the line to every profile **before**
installing 2.0. `api = xmlrpc` keeps a profile working as before.

What else a 1.x caller meets:

- The library's new names: the distribution `odoo-auth`, the package
  `odoo_auth` and the command `odoo-auth`. 2.0 ships nothing under 1.x's
  names, so a script written for 1.x must import and run these instead.
- 2.0 is another distribution, so installing it leaves 1.x in place:
  uninstall 1.x first, or its package and command stay installed, and a
  script still on them keeps running 1.x.
- 2.0 reads the profiles from `~/.config/odoo-auth/` only, a folder 1.x
  does not read: each profile must be there, and a script that opens the
  store's folder itself, rather than through the library, must open that
  one.
- Python 3.11.8 or later, but not 3.12.0 or 3.12.1, and `requests`
  2.32.3 and `urllib3` 2.6.0 or later, which must be importable whatever
  the profile's API: the client imports them when it is imported.
  `pip install` brings them; a script that puts the source checkout on
  `sys.path` or `PYTHONPATH` needs them in its own environment. From
  Python 3.11.8 and 3.12.2, `ssl.SSLContext.wrap_socket` closes the TLS
  socket it makes after anything its handshake raises, an interrupt such
  as Ctrl-C among them, where earlier releases close it only after an
  `OSError` or a `ValueError`; a `Client` relies on it to leave no socket
  open when a handshake fails or is interrupted (an interrupt, a
  `MemoryError` or a `RecursionError` in the few steps of `ssl` before the
  handshake starts is another matter: § Library usage). From 2.6.0,
  `urllib3` refuses an answer whose head chains more than five content
  encodings (CVE-2025-66418), which a `Client` relies on too (§ Errors).
  The suite runs on Python 3.11.13 with requests 2.34.2 and urllib3 2.8.0
  or 2.6.0, and on Python 3.13.5 with requests 2.32.3 or 2.34.2 and
  Debian's urllib3 2.3.0, whose `2.3.0-3+deb13u1` and later carry that
  refusal as a backport.
- A profile is refused too when its `url` holds a backslash, port 0, a
  query or a fragment, or a host IDNA refuses (§ Configure a profile).
- `ServerProxy` with a profile refuses `verbose=True`, and the library's
  connections print nothing at `http.client`'s debug level, where 1.x
  printed the password with each request (§ Security model).
- The CLI refuses a payload key other than `model`, `method`, `ids`,
  `kwargs` and `args`, where 1.x ignored it, and prints an error of Odoo as
  `Error: Odoo answered a call of profile 'x' with an error (code N):
  <message>`, the password shown as `***`, where 1.x printed the Fault's
  text alone.

## CLI usage

```
odoo-auth --profile <name> '<json>'
    │                 │       │
    │                 │       └─ one JSON object: the call, by name
    │                 └─ profile name → reads ~/.config/odoo-auth/<name>.conf
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
odoo-auth --profile customer1 \
  '{"model":"res.partner","method":"read","ids":[1],"kwargs":{"fields":["name"]}}'

# Search with fields and a limit
odoo-auth --profile customer1 \
  '{"model":"project.task","method":"search_read","kwargs":{"domain":[["project_id","=",5]],"fields":["name","stage_id"],"limit":10}}'

# Rename a task
odoo-auth --profile customer1 \
  '{"model":"project.task","method":"write","ids":[42],"kwargs":{"vals":{"name":"Fixed"}}}'

# Create a task, on either API
odoo-auth --profile customer1 \
  '{"model":"project.task","method":"create","kwargs":{"vals_list":[{"name":"Bug: login broken","project_id":42}]}}'
```

On an `api = xmlrpc` profile the payload may instead give `execute_kw`'s
positional arguments as `args`, the form before 2.0. They go as they are,
as `ServerProxy` sends them, with `kwargs` as `execute_kw`'s named
arguments, through the same login and with the same errors as a call by
name:

```bash
odoo-auth --profile customer1 \
  '{"model":"project.task","method":"create","args":[{"name":"Bug: login broken","project_id":42}]}'
```

An `api = json2` profile refuses `args`, a payload cannot hold both `ids`
and `args`, and without `args` it cannot hold `ids` inside `kwargs`: the
records go in `ids`. An error is printed on stderr after `Error:`, and
the CLI exits with status 1, never with a traceback. The error is one of
three kinds:

- one of the errors of `Client` below, in both forms of the payload, a
  call `Client` refuses (`ArgumentError`, such as an empty model, or NaN
  over JSON-2) among them;
- a payload the CLI refuses before it reads the profile, whose message
  also names the profile, the command line's: `A call of profile 'x'
  cannot be sent: …`, then what is wrong with the payload, such as
  `invalid JSON: …` for one Python's json cannot read (also an int longer
  than it converts or nesting deeper than it follows);
- an answer JSON cannot write (NaN or an infinity, on either API; over
  XML-RPC, a `dateTime`, `base64` or `bigdecimal` value; or one nested
  deeper than Python's json follows, which `xmlrpc.client` reads at any
  depth), which Odoo sends after it ran the call: `Odoo answered a call of
  profile 'x' with a value JSON cannot write, …: it ran the call, so do
  not repeat it before reading back what it did`.

The library's own messages are one line each; an `OdooError` ends with
Odoo's message, printed as it came, which can span lines (a traceback of
the server's, for one). A value a message quotes, such as a path, the
name of a type, an error's code or a payload's key, shows quoted and
escaped as Python's `repr` writes it when it holds a line break or
another character that does not print, as a profile's name always does.

## Library usage

### Client: one way to write a call, for both APIs

`Client` calls Odoo by name, over the API the profile's `api` line names:

```python
from odoo_auth.client import Client

with Client(profile="customer1") as odoo:
    companies = odoo.call(
        "res.partner", "search_read",
        domain=[["is_company", "=", True]], fields=["name"], limit=10,
    )
    odoo.call("res.partner", "write", [7], vals={"name": "Example"})
    partners = odoo.call("res.partner", "read", [7, 8], fields=["name"])
```

`call(model, method, /, ids=None, **named)`:

- `model` and `method` go by position only (the `/`), so that a method's
  own parameter named `model` or `method` can go by name with the others.
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
| Credential | the profile's `password`: a password or an API key | the profile's `password`, as `Authorization: Bearer`, which Odoo takes only as an API key |
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

A `Client` keeps its connection open from one call to the next, over both
APIs, and checks it before a request goes out on it, as `urllib3` checks a
connection of its pool: when the server has closed it since its last
answer, the request goes out on a new connection. After a failure of the
connection it keeps none, nor after a close of it that one of the three
errors below cut short, and leaves no socket open, but for five cases,
each where one of the three errors the library lets through as they are
(an interrupt such as Ctrl-C, a `MemoryError`, or a `RecursionError` from
the caller's own deep stack, § Errors) comes in a few steps of the code of
Python or `urllib3` that the library runs unchanged. Each leaves the same
state, whichever of the three it is:

- The TCP connect. `socket.create_connection` (XML-RPC) and `urllib3`'s
  (JSON-2) close the socket they make only after an `OSError`, and the
  connection holds that socket only once it comes back to the
  connection's `connect`. One of the three that comes from the socket's
  creation to that moment, an interrupt in the TCP connect above all,
  leaves the socket to Python's garbage collector, which closes it with a
  `ResourceWarning`. An interrupted connect goes on in the system, so the
  socket may end up connected. Nothing of the request was sent on it: the
  call raises the same error, and the next call works.
- The start of the TLS handshake, over https. Python's `ssl` makes its TLS
  socket on the TCP socket's file descriptor, then detaches the TCP
  socket, and only then enters the `try` whose bare `except` closes the
  TLS socket after anything the handshake raises (`SSLSocket._create`).
  One of the three in those few steps leaves the TLS socket to the garbage
  collector, which closes its descriptor later, with a `ResourceWarning`.
  Before the detach, the connection's close has closed that descriptor
  already, through the TCP socket: by the time the collector closes it
  again, the process may have given that number to another file or
  socket, a later call's connection among them, and that close fails
  (`EBADF`) or closes the other one under its owner. Nothing of the
  request was sent: the call raises the same error.
- The answer's file, over both APIs. Once the request has gone out,
  `http.client` makes its answer (`HTTPConnection.getresponse`), which
  first makes a file on the socket (`socket.makefile`): the socket counts
  the file before it hands it back, and the socket's close then waits for
  the file's. One of the three inside `socket.makefile`, once it counted
  the file and before it hands the file back, such as a `MemoryError` as
  it makes the file's buffer, comes before anything can close that file:
  the connection's close leaves the socket open, after `Client.close()`
  too, until the error's traceback is collected, as its frames hold the
  file. (Once `socket.makefile` has handed the file back, an error leaves
  no file open: the file is freed as the frame unwinds, or the answer
  holds it, and the answer's making closes it.) The socket then closes
  without a warning when reference counting frees the traceback. When the
  garbage collector frees the traceback in a reference cycle, the socket
  closes with a `ResourceWarning` if the same collection takes the socket,
  and without one if the socket outlived an earlier collection and a
  younger one frees the cycle. The request went out: the server may have
  run it. The call raises the same error.
- The close of the answer's file, over both APIs. Python's
  `socket.SocketIO.close` marks the file closed, then counts it out of the
  socket (`_decref_socketios`), whose own close waits for its files. One
  of the three between the two, as `http.client` closes the file at the
  end of a body or as the answer closes, leaves the socket counting a file
  that is closed: no close of the socket closes it then, `Client.close()`'s
  included, and it stays open until Python frees it, once neither the
  frames of the error's traceback nor the answer hold it (the connection's
  close drops the answer it keeps), and closes it with a
  `ResourceWarning`. The request went out: the server may have run it.
  The call raises the same error, and the next call works. (Once the file
  is counted out, an error before the socket's own close leaves nothing
  open: the connection's close closes that socket again.)
- The check of a kept connection, over JSON-2. `urllib3` takes a kept
  connection out of its pool, then checks it before it reuses it
  (`HTTPConnectionPool._get_conn`, `is_connection_dropped`). One of the
  three in that check, or in the few steps of `_get_conn` around it, such
  as a `RecursionError` in the calls that check makes, leaves the
  connection out of the pool, held at most by the frames of the error's
  traceback, with its socket open, but for a connection closed before it
  went back to the pool, as after an answer that closed it, which has no
  socket, and for one the check found closed by the server, once the
  check's close of it closed its socket. `Client.close()` closes it, and
  if nothing holds it any more before then, as once that traceback is
  collected, Python's garbage collector closes its socket then, if open,
  with a `ResourceWarning`. Nothing of the request was sent on it: the
  call raises the same error.

Each request goes out once, never again by itself after a failure
([Errors](#errors)).

### ServerProxy: the XML-RPC drop-in

Drop-in replacement for `xmlrpc.client.ServerProxy`, for `api = xmlrpc`
profiles. Just change the import and add `profile=`:

```python
from odoo_auth.client import ServerProxy

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
It sends a request as `xmlrpc.client` does: on its kept connection without
checking it first, and a second time after a connection closed or reset
before the answer, so that a call the server ran runs twice.
With a profile it refuses `verbose=True`, which asks `xmlrpc.client` to
print each request, the password among them, with an `ArgumentError`
before the profile is read. A `transport` given to it is the caller's, and
it keeps it: its connections are not the library's, so at `http.client`'s
debug level 1 they print each request, the password among them. It is the
one exception to the library's connections printing nothing (§ Security
model), and it holds only for a transport the caller gives.

Each call with a profile, the login's too, runs in a context of its own
(`contextvars`), which holds every context variable of the caller's but
those of the library: a variable that a transport the caller gives sets
during a call reaches the caller's context as the call ends, whatever
ends it, a result, an error or an interrupt, as in 1.x, and none of the
library's does. What differs from 1.x:

- A token the caller took before a call cannot be reset during it, and a
  token taken during a call cannot be reset after it: `ContextVar.reset`
  raises `ValueError`, as the token belongs to another context. Raised by
  the transport, that `ValueError` reaches the caller as it is.
- As the call ends, the library sets the variables the call changed in
  the caller's context, one by one. An interrupt such as Ctrl-C, a
  `MemoryError` or a `RecursionError` that comes as they are set leaves
  those not yet set at the values the caller's context had before the
  call, for good, whether or not the call's request went out: the call is
  over by then, and the error is raised instead of its result or its own
  error.

## Errors

Every `Client` call raises the same errors, whatever the API. Each names the
profile and never quotes its password or API key, and no frame of its
traceback holds them in a local, which a test runner or a debugger can
print (`pytest -l`). `ServerProxy`'s own errors never quote the password
either, and no local of their frames shows it; but a frame that holds the
`ServerProxy`, as each of its calls' frames does, shows it one attribute
down, in `_password`, which stays readable as in 1.x. Where the profile's
`url`, `db`, `user` or `api` holds the password, or the password is text a
message writes anyway, such as the profile's name, or a piece of it, more
shows it: § Configure a profile says what. The errors:

- `ConfigError`: the profile is missing, cannot be read (a folder in its
  place, a file this user may not read), is malformed or accessible by
  others, has no valid `api` line, or holds a value its API cannot send
  (below).
- `AuthError`: Odoo refuses the profile's credentials, at the XML-RPC login
  or, over JSON-2, as an HTTP 401 for the password, which Odoo answers for
  any password but one of its API keys, except, on Odoo 19, one with a NUL
  among its first 8 characters, which gets an HTTP 403, an `OdooError`.
- `RequestError`: a failure of the transport, in place of the error of
  `socket`, `ssl`, `http.client`, `xmlrpc.client`, `requests`, or `urllib3`,
  which `requests` raises as it is when it does not know one (such as the
  error of `urllib3` 2.3.0 for a redirect to `<origin>//x:y`): a name that
  does not resolve, a connection refused or dropped, TLS, an HTTP status
  other than 200 that carries no error of Odoo (such as the 404 page Odoo
  sends when `X-Odoo-Database` names no database it serves), an answer that
  is not JSON, or not XML-RPC (such as a login page), a request or a
  redirect to another origin (`InvalidURL`, `RedirectError`), a redirect to
  a location that cannot be read as a URL (`RedirectError`). It carries the
  profile (`profile`), that error's class name (`error_class`) and its
  errno or HTTP status, if any (`code`), never its text, which can quote
  the url, nor the request of `requests`, which holds the profile's
  password. It is also an instance of that error's class, so `except OSError`,
  `except xmlrpc.client.ProtocolError`, `except requests.ConnectionError`
  and the like keep catching it; as a `ProtocolError` it keeps its
  `errcode`, while its `url` and `errmsg` are empty. For an answer that
  cannot be read as XML-RPC it stands for `xmlrpc.client.ResponseError`,
  whatever reading or parsing it raised (an `ExpatError`, a `ValueError`,
  an `IncompleteRead`…). When the answer to a call came with status 200
  and cannot be read, not XML-RPC or not JSON (`JSONDecodeError`), cut
  short, sent in bad chunks, not the gzip it says, or broken off, its
  `code` is 200 on both APIs, and its message says: «The server answered:
  it may have run the call, so do not repeat it before reading back what
  it did.» A retry would run the call again. An answer to the login that
  cannot be read has no `code`: no call was sent. `Client` sends each
  request once, the XML-RPC login's too, and never again by itself after a
  failure, where `xmlrpc.client`, and so `ServerProxy`, sends a request a
  second time after its connection was closed or reset before the answer:
  a call the server ran would run twice. Once the call started to go out,
  even in part, every `RequestError` of it says, on both APIs, but a
  timeout's (below) and that of an answer at status 200 that cannot be
  read (above): «It failed after the call started to go out: the server
  may have run the call, so do not repeat it before reading back what it
  did.» That covers its
  connection reset, closed or failing in TLS, an answer whose head cannot
  be read, and every answer that is not Odoo's own: a status other than
  200 without Odoo's error body (a gateway's 504, even in JSON with a
  `message`, as AWS API Gateway's is, a proxy's 500 or 404, among them
  pages no server ran the call for, which the library cannot tell apart),
  a body that cannot be read after such a status, a redirect
  refused or followed too many times, an error of `urllib3` for a
  redirect. Odoo's own answers raise no `RequestError`: an `OdooError` (an
  XML-RPC `Fault`, a JSON-2 error body of Odoo's), or an `AuthError` (a
  401 over JSON-2, whatever its body, which is not read). Over JSON-2 that
  holds for a redirect's new connection
  too, even one that fails to open, and for a redirect's body: a redirect,
  such as a 303, can answer a request the server processed. `Client`
  reads a redirect's body as it comes, undecoded, and drops it; one that
  cannot be read whole (stopping, breaking off, in bad chunks, or under a
  head naming more content encodings than `urllib3` chains, five from
  2.6.0, which it refuses before it reads) ends the
  call as the body of any answer does, with the redirect's status as its
  `code`, but after the read timeout, which leaves it `None`, and the
  redirect is not followed. A failure before anything
  of the call was sent says nothing of it (a name that does not resolve, a
  connection refused, the connect timeout, a TLS handshake that fails or
  is reset), nor does one of the XML-RPC login, which is not the call.
  Over JSON-2, where `urllib3` raises the same errors before and after a
  request starts to go out, the library's connections mark the call as
  started just before the first byte of one of its requests is sent, and
  the mark ends with the call. When one of
  `Client`'s timeouts fires, `timeout_fired` says which, `"connect"` or
  `"read"` (`None` otherwise), and so does the message, and its `code` is
  `None`. After the connect timeout nothing was sent, unless it fired over
  JSON-2 after the call started to go out: on a redirect's new connection,
  or as a request goes out, which `requests` sends under the connect
  timeout. The message then says: «The connect timeout fired after the
  call started to go out: the server may have run the call, so do not
  repeat it before reading back what it did.» After the read
  timeout the server may still complete the call, so do not repeat it
  before reading back what it did; after the read timeout of the XML-RPC
  login, which is not the call, the message says instead: «The read
  timeout fired at the login: the call was not sent.» It is then an
  instance of `socket.timeout` (`TimeoutError`) over XML-RPC, and of
  `requests.ConnectTimeout`, `requests.ReadTimeout` or
  `requests.ConnectionError` over JSON-2, where `requests` reports a TLS
  handshake that times out as a `ReadTimeout`, which counts as read.
- `OdooError`: Odoo answers the call with an error, an XML-RPC `Fault`
  (the login's too, such as Odoo's for a `db` it does not hold) or a JSON-2
  error body: the object Odoo's JSON-2 dispatcher answers, whose `name`
  and `message` are strings and whose `arguments` are a list. It carries
  the profile (`profile`), Odoo's code for it
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
  `threading.TIMEOUT_MAX` (a `ValueError`); `ServerProxy(profile=...)`, for
  `verbose=True` (a `ValueError`). Its message then says that the `Client`
  or the `ServerProxy` of the profile cannot be created.

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

The library stops no `KeyboardInterrupt`, no `MemoryError`, and no
`RecursionError` raised because the caller's own stack is already close
to Python's recursion limit: they pass through as they are, and frames
of their traceback hold the password or the API key, alone or in the
request, as those of `ServerProxy`'s raw errors do. A library does not
intercept an interrupt or the exhaustion of a resource, and their
tracebacks are how they are diagnosed. Over XML-RPC, `Client` first
closes the connection of the request one of them ends, as it does after
any failure: it keeps a connection only after an answer read whole, a
result or Odoo's `Fault`. Over JSON-2 it first closes every answer the
call got, its socket and the connection it still holds with it,
whichever step of `urllib3`, `requests` or the library the error stopped:
an answer read whole has given its connection back for the next call.
These closes do not reach what one of them leaves in the five cases of
§ Library usage. One of them in `Client.close()` goes on as it is, over
JSON-2 too, where the client takes the pools of `urllib3` from their pool
managers, closes the session of `requests`, which drops them from those
managers, then closes each pool itself, rather than leave its connections
to the pool's finalizer: `urllib3` 2 closes a dropped pool's connections
in the pool's finalizer, as the pool is collected, where Python reports
such an error as ignored («Exception ignored» on stderr) and stops it.
A closed pool's finalizer still runs as the pool is collected, once
`Client.close()` has closed everything, and finds nothing left to close:
one of the three that comes inside it stops there all the same. (Raised
while `Client` encodes a
call's values, a `RecursionError` is an `ArgumentError`, above, even when
the caller's deep stack caused it. Raised while it reads the JSON of an
answer over JSON-2, it makes the answer one that is not JSON, even when
the caller's deep stack caused it and the answer was good. A 200 is then
a `RequestError` with `code` 200, though the server ran the call. Any
other status but a 401, whose body is not read, is then the
`RequestError` of that status, which says that the server may have run
the call, even when the answer was Odoo's own error, which would have
been an `OdooError`. Their class cannot tell the two causes apart.) The
CLI lets them through too: Python prints their traceback, which shows no
frame's locals.

The finalizer of an answer or of a file, `io.IOBase`'s, which runs as
Python collects it, stops one of the three too, where it runs code of
Python. It reads whether each answer of `urllib3` is closed, which
`urllib3` answers in code of Python, and drops one of the three that
comes there, with nothing on stderr: the answers of a JSON-2 call are
collected as the call ends, and the call then returns its result, or
raises its own error, as if that one had not come; those a frame of an
error holds, as that frame is collected. It also closes an answer of
`http.client`, or a file, still open as it is collected, where Python
reports one of the three that comes inside that close as ignored from
Python 3.13 («Exception ignored» on stderr), and drops it silently
before, but in its development mode (`-X dev`) and its debug builds.
`Client` closes each answer of `http.client` its connections make before
then, but two: one whose making one of the three stopped as it began,
before the `try` that would close it, which only the frames of that
error's traceback hold, and whose close closes nothing; and the last
answer of a `Client` over XML-RPC that is collected without being
closed. The one file is that of § Library usage's third case.
`ServerProxy` with a profile reads and closes its answers as
`xmlrpc.client`'s own `ServerProxy` does: the reading closes none of
them; closing a connection the server keeps open, as `ServerProxy`'s
close does, closes the last answer on it; and the finalizer closes each
one still open as it is collected, where one of the three that comes
inside that close is reported as ignored from Python 3.13, and dropped
silently before, but in Python's development mode and its debug builds.

## Configure a profile

One file per Odoo instance, `chmod 600`:

```bash
mkdir -p ~/.config/odoo-auth
chmod 700 ~/.config/odoo-auth

cat > ~/.config/odoo-auth/customer1.conf << 'CONF'
[odoo]
url=https://customer1.odoo.com
db=prod_db
user=user@example.com
password=your-api-key
api=json2
CONF

chmod 600 ~/.config/odoo-auth/customer1.conf
```

`api` names the API every call of the profile goes through:

- `json2`, Odoo's External JSON-2 API, from Odoo 19. It takes only an Odoo
  API key, which goes in `password`. That is Odoo's rule, which Odoo
  enforces: the library sends the `password` the profile holds, and Odoo
  answers one that is not one of its API keys with an HTTP 401, an
  `AuthError` whose message says that JSON-2 takes only an API key
  (§ Errors). Odoo 19 answers one with a NUL among its first 8 characters
  with an HTTP 403 instead, an `OdooError`. The HTTP server that serves
  Odoo, or a proxy in front of it, can refuse the header line that
  carries the password before Odoo reads it, for its length or for a
  character it does not take, with another status of its own, a
  `RequestError`: a server built on Python's `http.server` answers a line
  longer than 65,536 bytes with an HTTP 431.
- `xmlrpc`, for Odoo 18 and earlier. Odoo 19 to 21 still serve it, but
  deprecated, and Odoo plans its removal for Odoo 22. On Odoo 19 the server
  logs a deprecation warning for each XML-RPC call, and an error with a
  traceback for each method that returns `None`, after its work is done.

The tool refuses to run if the config directory or files are accessible by
group or others (like `ssh` does with `~/.ssh/`), or if `url` is not an http
or https URL that names a host, with a port from 1 to 65535 if it has one,
on one line of printable ASCII without spaces, or has a user or password
before its host, or holds a backslash, or a query or a fragment, even an
empty one (a lone `?` or `#`), or names a host IDNA refuses, with an empty
label between its dots or one longer than 63 characters. The url is not
shown, not even its scheme: xmlrpc.client would quote it, user info
included, in its errors. Both APIs encode the host with Python's IDNA
codec before they look it up, and its refusal would quote the host. A
backslash is refused because RFC 3986 allows one in a URL only as `%5C`,
and `requests`, which JSON-2 goes through, would end the host at a
backslash in it. Port 0 is refused because no connection goes to it:
`http.client`, which XML-RPC goes through, is refused there, and
`requests` sends it to the scheme's default port. A query or a fragment
would swallow the path each request adds at the url's end
(`/xmlrpc/2/common` and `/xmlrpc/2/object`, or
`/json/2/<model>/<method>`), and no call could work; a path, as for a
server behind a reverse proxy, is kept.
An `api = json2` profile
is refused too if its `password` is not one line of Latin-1 characters, as
an Odoo API key always is, or its `db` is not one line of printable ASCII
without spaces: JSON-2 sends both in headers, where `http.client`, which
sends each request, refuses a line break with an error that quotes the
value, and cannot encode a character outside Latin-1. Neither is shown.
It is refused as
well if `requests`, which JSON-2 sends through, refuses its `url` as it
prepares a request, as for a host that starts with `*`, or prepares it for
another scheme, host or port than the `url` names, as for a host with a
`%`, a `>` or a `{` in it, which it writes otherwise, so that no call could
go; XML-RPC sends through `http.client`, which takes such a host as it is
and looks it up.

A profile whose `url`, `db`, `user` or `api` holds its `password`, such as
one whose `user` and `password` are both `admin`, loads and calls as any
other: whether a user or a password is right is for Odoo to answer. Those
four values are not secret, and the library holds them as they are, so a
password one of them holds shows with them:

- `Client`'s repr shows the profile's name and its `api`, and no other
  value; `ServerProxy`'s shows the `url`'s host and path.
- The frames of an error show the `url` at their own level, which a test
  runner prints (`pytest -l`), where they hold a `ServerProxy`, of
  `xmlrpc.client` or of the library: `common` in the frame of the XML-RPC
  login, `Client`'s or `ServerProxy`'s, and `self` in that of
  `ServerProxy.execute_kw`. One attribute down, which a debugger shows,
  the frames of an error can hold the `url`, the `db`, the `user` and the
  `api`: the profile's values (`config`) in those of the login hold all
  four.
- `ServerProxy` passes Odoo's `Fault` as it is, as in 1.x, so a `Fault`
  whose text quotes such a value shows it. `Client`'s `OdooError` masks
  Odoo's message wherever it holds the password, inside such a value too,
  so that a message that quotes such a `user` shows `***` in its place.
- A password the `url`'s host holds goes with the host to the DNS lookup,
  and over https in the TLS handshake (SNI), before any answer.

A password that is text a message of the library writes anyway, or a
piece of it, shows in that message, as no mask can tell the two apart: the
`api` value, as in `ServerProxy`'s refusal of a profile that has
`api = json2`, the profile's name, which every message names, an HTTP
status, a fixed word.

## Security model

| Data | Leaves your machine? |
|---|---|
| Odoo credentials | No — read locally from `~/.config/`; over JSON-2 the password goes only to the origin of the profile's url (scheme, host and port); a password the url's host holds also goes with the host to the DNS lookup and, over https, in the TLS handshake (SNI), before any answer |
| API call parameters | Yes — sent to Odoo via XML-RPC or JSON-2 |
| API results | Yes — returned from Odoo via XML-RPC or JSON-2 |

The wrapper is like `psql` reading `~/.pgpass` — credentials stay local,
only the queries go over the wire. Over JSON-2, each request is checked as
`requests` prepared it, the url as the connection reads it: one for another
origin gets no password and is not sent, and a redirect to another origin
is refused before anything is sent there. The client takes no proxy and no
`~/.netrc` from the environment, as xmlrpc.client does not either.

The library's own connections, `Client`'s over both APIs and `ServerProxy`'s
with a profile, print nothing, whatever debug level the process sets:
`http.client.HTTPConnection.debuglevel = 1`, the recipe of `requests`' docs
to see the wire, would print each request's headers and body, the profile's
password among them. A `transport` the caller gives `ServerProxy`
is not the library's, and prints them at that level (§ ServerProxy: the
XML-RPC drop-in). To see what a call sends, use a local stub.
While `Client` handles a call over JSON-2, no logger of `urllib3` logs a
record either: at DEBUG, its pool's line for each request names the
location of a redirect, and its warning for headers it cannot parse quotes
them as the server sent them, either of which can carry the profile's
password when the server echoes it, and an application that configured logging
(`logging.basicConfig()`) would print it. The records that `urllib3` logs
in the call's thread or task while the call runs are dropped, whoever
logs them: code that runs there meanwhile, such as a signal handler or a
finalizer, loses those of another connection of `urllib3` too, as a
record does not carry what would tell the library's apart. Every other
record of the process is logged as before, those of `urllib3` logged
outside a call or in another thread or task among them.

What the transports take from the environment, and what they do not:

- Over JSON-2, no CA bundle either (`REQUESTS_CA_BUNDLE`,
  `CURL_CA_BUNDLE`): certificates are checked against the bundle of
  `requests` (certifi's), while XML-RPC checks them against the system's
  store. A server whose certificate a private CA signed, one the system
  trusts, works over XML-RPC and not over JSON-2, and the library has no
  setting for another bundle.
- Both write the session keys of TLS to the file `SSLKEYLOGFILE` names,
  when it is set: urllib3 reads it for JSON-2, Python's `ssl` for XML-RPC.
  Whoever reads that file can decrypt the traffic, the profile's password
  included.

JSON-2 goes through `requests` and not the standard library's `urllib`
because of the profile's password: `urllib`'s redirect handler copies a
request's headers, an `Authorization` header among them, to whatever host
a redirect names (`urllib/request.py`,
`HTTPRedirectHandler.redirect_request`), and gives no hook to check where
a request goes. `requests` drops `Authorization` when a redirect changes
host, and its session gives the two hooks the client uses: the auth,
called with each request as prepared, which sets the password only for
the profile's origin, and `rebuild_auth`, called on each redirect before
anything is sent, which refuses any other origin.

## Prerequisites

- **python3 >= 3.11.8**, other than 3.12.0 and 3.12.1 (§ Upgrading to 2.0
  says why)
- **requests >= 2.32.3** and **urllib3 >= 2.6.0**, which the client imports
  whatever the profile's API (§ Upgrading to 2.0 says why that urllib3)

## License

Apache License 2.0 — see [LICENSE](LICENSE) and [NOTICE](NOTICE).

Copyright (c) 2026 NuoBiT Solutions, S.L.
