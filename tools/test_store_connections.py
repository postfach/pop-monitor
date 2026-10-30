"""The receiver doesn't run out of files:  python3 tools/test_store_connections.py

Every web request runs in a thread of its own with its own database connection; a finished
thread's connection is closed by the next one opened, so open files stay flat however many
requests come in (they grew by one per request until, after a day, the database couldn't be
opened: "sqlite3.OperationalError: unable to open database file")."""
import os
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store  # noqa: E402


def open_files():
    return len(os.listdir("/proc/self/fd"))


class Connections(unittest.TestCase):
    def test_flat_over_many_requests(self):
        s = store.Store(tempfile.mkdtemp(), log=lambda m: None)

        def request():
            s.seen("x")
            s.add({"event_id": f"e{threading.get_ident()}-{os.urandom(4).hex()}", "type": "heartbeat"})
        for _ in range(50):
            t = threading.Thread(target=request)
            t.start()
            t.join()
        before = open_files()
        for _ in range(1000):
            t = threading.Thread(target=request)
            t.start()
            t.join()
        self.assertLessEqual(open_files() - before, 5)
        self.assertLessEqual(s.open_connections(), 3)
        self.assertTrue(s.seen("x") is False)                 # this thread's connection still works


if __name__ == "__main__":
    unittest.main()
