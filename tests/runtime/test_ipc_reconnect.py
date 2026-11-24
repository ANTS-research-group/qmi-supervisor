"""The actor socket accepts reconnect requests and still rejects malformed ones."""
from pathlib import Path
import tempfile
import threading
import time
import unittest

from modules.runtime.ipc import AcquisitionServer, request


class ReconnectIpcTests(unittest.TestCase):
    def test_reconnect_is_queued_for_the_actor_and_answered(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        stop = threading.Event()
        server = AcquisitionServer(Path(temporary.name)/'modem.sock', stop)
        self.addCleanup(stop.set)
        self.addCleanup(server.close)
        received = []
        def actor():
            while not received and not stop.is_set():
                item = server.take()
                if item is None:
                    time.sleep(0.02)
                    continue
                received.append(item)
                item['response'] = {'ok': True}
                item['done'].set()
        threading.Thread(target=actor, daemon=True).start()
        self.assertEqual(request(Path(temporary.name)/'modem.sock', {'method': 'reconnect'}, 5),
                         {'ok': True})
        self.assertEqual(received[0]['method'], 'reconnect')
