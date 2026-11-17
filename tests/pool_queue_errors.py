"""Tests of one of the three errors the library lets through as they are (an
interrupt such as Ctrl-C, a MemoryError, a RecursionError from the caller's
deep stack) in the queue where a pool of urllib3 keeps a JSON-2 client's
connections: as a call takes its kept connection out of it or gives it back,
and as Client.close() empties it. In a queue whose lock such an error leaves
taken, every later get or put waits without end, the pool's finalizer's too,
as the pool is collected, even as Python exits.

KeptConnectionTest of test_calls runs this file in a Python of its own, under
a time limit, so that a wait without end fails that test instead of hanging
the run (run_python). Here the steps of each case run in a thread of their
own, joined after 5 s, so that the case fails at the first wait without end,
and the run ends without the finalizers left (os._exit).

Run from the repository root:
    PYTHONPATH=src python3 -B -W error tests/pool_queue_errors.py
"""

import os
import sys
import threading
import unittest
import weakref

import urllib3

from odoo_auth.client import Client
from support import KEY, KeepAliveOdoo
from test_calls import StubTestCase, collect_garbage, collector_paused

GET_CONN = urllib3.connectionpool.HTTPConnectionPool._get_conn.__code__
PUT_CONN = urllib3.connectionpool.HTTPConnectionPool._put_conn.__code__
CLOSE_POOL_CONNECTIONS = urllib3.connectionpool._close_pool_connections.__code__


def raising_at(number, error, interrupted, *callers):
    """A trace function, for sys.settrace: it raises ``error`` at the
    ``number``-th line that runs in or below the get or the put of a pool's
    queue that one of ``callers``, code objects of urllib3, calls, and
    appends that get's or put's name to ``interrupted``."""
    lines = 0

    def queue_operation(frame):
        # The name of the get or the put that ``frame`` runs in or below,
        # called by one of ``callers``; None for any other frame.
        while frame is not None and not (
            frame.f_code.co_name in ("get", "put")
            and frame.f_back is not None
            and frame.f_back.f_code in callers
        ):
            frame = frame.f_back
        return None if frame is None else frame.f_code.co_name

    def tracing(frame, event, arg):
        nonlocal lines
        if event == "line":
            lines += 1
            if lines == number:
                interrupted.append(queue_operation(frame))
                raise error
        return tracing

    def calls(frame, event, arg):
        return tracing if queue_operation(frame) is not None else None

    return calls


def pool_of(client, url):
    """The one pool of ``client``'s session, for ``url``'s origin."""
    manager = client._transport._session.get_adapter(url).poolmanager
    (pool,) = [manager.pools[key] for key in manager.pools.keys()]
    return pool


def ran_in_a_thread(function, *args):
    """Run ``function(*args)`` in a thread of its own, and wait for it 5 s at
    most: one that waits without end is left waiting. Whether it ended. No
    collection of garbage starts by itself meanwhile (collector_paused): it
    could run the finalizer of an earlier client's pool, whose drain of the
    queue would count among the lines raising_at counts."""
    thread = threading.Thread(target=function, args=args, daemon=True)
    with collector_paused():
        thread.start()
        thread.join(5)
    return not thread.is_alive()


class PoolQueueErrorTest(StubTestCase):
    def test_an_error_as_a_call_takes_or_gives_back_its_kept_connection_hangs_nothing(self):
        # A call takes its kept connection out of the pool's queue
        # (HTTPConnectionPool._get_conn) and gives it back once the answer
        # is read (_put_conn). One of the three comes at each line that runs
        # in or below the queue's get or put there, in turn, a new client
        # each time, until the call runs whole. The call raises it, the next
        # call works, Client.close() returns and leaves no socket open, and
        # once the error is dropped, the pool is collected, its finalizer
        # run. The error stays held until the client is closed, as a prompt
        # holds the last error: a connection it moved out of the pool is the
        # client's to close.
        self.fail_on_resource_warnings()
        odoo = self.start(KeepAliveOdoo())
        self.make_profile(odoo.url, "json2", KEY)
        opened = self.record_sockets(urllib3.util.connection)
        for error_class in (KeyboardInterrupt, MemoryError, RecursionError):
            with self.subTest(error=error_class):
                interrupted = []
                steps = []
                number = 0
                while steps[:1] != ["returned 3"]:
                    number += 1
                    client = Client("erp")
                    self.assertEqual(client.call("res.partner", "search_count", domain=[]), 3)
                    collected = weakref.ref(pool_of(client, odoo.url))
                    # Handed to the thread, which drops them.
                    held = [client, error_class()]
                    del client
                    # The case's own list, handed to its thread: one left
                    # waiting past the wait writes into no later case's.
                    steps = []

                    def run(held, number, collected, steps):
                        client, error = held
                        sys.settrace(raising_at(number, error, interrupted, GET_CONN, PUT_CONN))
                        try:
                            steps.append(f"returned {client.call('res.partner', 'search_count', domain=[])}")
                        except BaseException as raised:
                            steps.append("raised it" if raised is error else f"raised {raised!r}")
                        finally:
                            sys.settrace(None)
                        steps.append(f"next call {client.call('res.partner', 'search_count', domain=[])}")
                        client.close()
                        steps.append(f"closed, open sockets {[s for s in opened if s.fileno() != -1]}")
                        held.clear()
                        del client, error
                        collect_garbage()
                        steps.append("collected" if collected() is None else "not collected")

                    ended = ran_in_a_thread(run, held, number, collected, steps)
                    self.assertTrue(ended, f"waited 5 s for the case's steps, which got to {steps}")
                    self.assertEqual(steps[1:], ["next call 3", "closed, open sockets []", "collected"])
                    self.assertIn(steps[0], ["raised it", "returned 3"])
                # Each error came in both, and the last call ran whole.
                self.assertEqual(sorted(set(interrupted)), ["get", "put"])
                self.assertEqual(len(interrupted), number - 1)

    def test_an_error_as_client_close_empties_a_pool_hangs_nothing(self):
        # Client.close() closes each pool of the client, which takes each
        # item out of its queue and closes it (_close_pool_connections): here
        # a pool of each scheme, which the session's pool manager makes for
        # 127.0.0.1 before any connection. One of the three comes at each
        # line that runs in or below the queue's get there, in turn, a new
        # client each time, until the close runs whole. The close raises it,
        # a second Client.close() returns, and once the error is dropped,
        # the pool is collected, its finalizer run, which takes from the same
        # queue whatever the close left in it.
        self.fail_on_resource_warnings()
        self.make_profile("http://127.0.0.1", "json2", KEY)
        for scheme in ("http", "https"):
            for error_class in (KeyboardInterrupt, MemoryError, RecursionError):
                with self.subTest(scheme=scheme, error=error_class):
                    interrupted = []
                    steps = []
                    number = 0
                    while steps[:1] != ["returned None"]:
                        number += 1
                        client = Client("erp")
                        manager = client._transport._session.get_adapter(f"{scheme}://127.0.0.1").poolmanager
                        collected = weakref.ref(manager.connection_from_url(f"{scheme}://127.0.0.1"))
                        # Handed to the thread, which drops them.
                        held = [client, error_class()]
                        del client, manager
                        # The case's own list, handed to its thread: one left
                        # waiting past the wait writes into no later case's.
                        steps = []

                        def run(held, number, collected, steps):
                            client, error = held
                            sys.settrace(raising_at(number, error, interrupted, CLOSE_POOL_CONNECTIONS))
                            try:
                                steps.append(f"returned {client.close()}")
                            except BaseException as raised:
                                steps.append("raised it" if raised is error else f"raised {raised!r}")
                            finally:
                                sys.settrace(None)
                            client.close()
                            steps.append("closed again")
                            held.clear()
                            del client, error
                            collect_garbage()
                            steps.append("collected" if collected() is None else "not collected")

                        ended = ran_in_a_thread(run, held, number, collected, steps)
                        self.assertTrue(ended, f"waited 5 s for the case's steps, which got to {steps}")
                        self.assertEqual(steps[1:], ["closed again", "collected"])
                        self.assertIn(steps[0], ["raised it", "returned None"])
                    # Each error came in the queue's get, and the last close
                    # ran whole.
                    self.assertEqual(set(interrupted), {"get"})
                    self.assertEqual(len(interrupted), number - 1)


if __name__ == "__main__":
    result = unittest.main(exit=False).result
    sys.stdout.flush()
    sys.stderr.flush()
    # Without the exit's finalizers: one of them, a pool's, could wait
    # without end for a lock an error left taken.
    os._exit(0 if result.wasSuccessful() else 1)
