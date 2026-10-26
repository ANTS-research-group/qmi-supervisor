"""One owned libqmi session with cancellable, bounded asynchronous calls."""
import logging
import math
import time
from datetime import datetime, timezone
import gi

gi.require_version("Qmi", "1.0")
from gi.repository import Gio, GLib, Qmi


class Session:
    def __init__(self, timeout, stop):
        self.timeout, self.stop = timeout, stop
        self.context = GLib.MainContext.new()
        self.device = None
        self.clients = {}
        self.allocating = set()
        self.cleaning = False
        self.collection_deadline = None

    def wait(self, start, finish, *args):
        loop = GLib.MainLoop.new(self.context, False)
        cancellable = Gio.Cancellable()
        result = {}

        def ready(source, response, user_data=None):
            try:
                result["value"] = finish(response)
            except Exception as error:
                result["error"] = error
            loop.quit()

        def expired(*unused):
            result["error"] = TimeoutError("QMI operation timed out")
            cancellable.cancel()
            loop.quit()
            return False

        def interrupted(*unused):
            if self.stop.is_set() and not self.cleaning:
                result["error"] = InterruptedError("Supervisor is stopping")
                cancellable.cancel()
                loop.quit()
                return False
            return True

        seconds = min(self.timeout, 3) if self.cleaning else self.timeout + 1
        if self.collection_deadline is not None and not self.cleaning:
            seconds = min(seconds, max(0.001, self.collection_deadline - time.monotonic()))
        deadline = GLib.timeout_source_new(max(1, int(seconds * 1000)))
        deadline.set_callback(expired)
        deadline.attach(self.context)
        signal = GLib.timeout_source_new(100)
        signal.set_callback(interrupted)
        signal.attach(self.context)
        self.context.push_thread_default()
        try:
            start(*args, cancellable=cancellable, callback=ready)
            loop.run()
        finally:
            deadline.destroy()
            signal.destroy()
            self.context.pop_thread_default()
        if "error" in result:
            raise result["error"]
        return result.get("value")

    def open(self, path):
        self.device = self.wait(Qmi.Device.new, Qmi.Device.new_finish, Gio.File.new_for_path(path))
        self.wait(self.device.open, self.device.open_finish, Qmi.DeviceOpenFlags.PROXY, self.timeout)

    def client(self, service, cid=Qmi.CID_NONE):
        if service not in self.clients:
            self.allocating.add(service)
            try:
                self.clients[service] = self.wait(self.device.allocate_client, self.device.allocate_client_finish,
                                                 service, cid, self.timeout)
            finally:
                self.allocating.discard(service)
        return self.clients[service]

    def request(self, service, name, value=None):
        client = self.client(service)
        output = self.wait(getattr(client, name), getattr(client, name + "_finish"), value,
                           min(self.timeout, 3) if self.cleaning else self.timeout)
        try:
            output.get_result()
        except GLib.Error as error:
            if name == "start_network":
                # WDS may already have an active context after a previous owner
                # exited. The caller must verify it instead of treating this as
                # an APN activation failure.
                if error.matches(Qmi.protocol_error_quark(), Qmi.ProtocolError.NOEFFECT):
                    raise
                reason = optional(output, "get_call_end_reason")
                verbose = optional(output, "get_verbose_call_end_reason")
                raise RuntimeError(f"APN activation failed: {error}; call-end={reason}, verbose={verbose}. "
                                   "Check APN, SIM provisioning and network availability") from error
            raise
        return output

    def collect(self, requests, *, budget, request_timeout):
        """Issue a bounded group concurrently, recording each host observation window.

        requests: (key, service, method, input) tuples. No parallel Python threads
        touch this QMI session; all completions run on the same GLib context.
        """
        started = time.monotonic()
        deadline_at = started + budget
        previous_deadline = self.collection_deadline
        self.collection_deadline = deadline_at
        loop = GLib.MainLoop.new(self.context, False)
        cancel = Gio.Cancellable()
        outputs, records = {}, {}
        pending = set()
        closed = False

        def stamp():
            return datetime.now(timezone.utc).isoformat()

        def finish_request(key, client, name, response):
            try:
                output = getattr(client, name + '_finish')(response)
                output.get_result()
                error = None
            except Exception as failure:
                output, error = None, failure
            # Cancellation may complete during a later control operation. A closed
            # collection is immutable and must never publish these late values.
            if closed:
                return
            record = records[key]
            record.update(received_at_utc=stamp(), received_monotonic=time.monotonic(),
                          status='ok' if error is None else 'error')
            record['duration_ms'] = (record['received_monotonic'] - record['sent_monotonic']) * 1000
            if error is None:
                outputs[key] = output
            else:
                record['error'] = str(error)
                if isinstance(error, GLib.Error):
                    record['unsupported'] = error.matches(Qmi.protocol_error_quark(), Qmi.ProtocolError.NOTSUPPORTED)
            pending.discard(key)
            if not pending:
                loop.quit()

        def expired(*unused):
            cancel.cancel()
            loop.quit()
            return False

        def interrupted(*unused):
            if self.stop.is_set():
                return expired()
            return True

        timer = GLib.timeout_source_new(max(1, int(budget * 1000)))
        timer.set_callback(expired)
        timer.attach(self.context)
        signal = GLib.timeout_source_new(50)
        signal.set_callback(interrupted)
        signal.attach(self.context)
        self.context.push_thread_default()
        try:
            for key, service, name, value in requests:
                records[key] = {'method': name, 'service': Qmi.service_get_string(service),
                                'queued_at_utc': stamp(), 'sent_at_utc': None,
                                'sent_monotonic': None, 'status': 'pending'}
                if time.monotonic() >= deadline_at or self.stop.is_set() or service in self.allocating:
                    records[key]['status'] = 'not_requested'
                    continue
                try:
                    # Allocate missing clients within the same overall budget.
                    client = self.client(service)
                    if time.monotonic() >= deadline_at or self.stop.is_set():
                        records[key]['status'] = 'not_requested'
                        continue
                    records[key].update(sent_at_utc=stamp(), sent_monotonic=time.monotonic())
                    pending.add(key)
                    timeout = max(1, math.ceil(min(request_timeout, deadline_at - time.monotonic())))
                    getattr(client, name)(value, timeout, cancel,
                        lambda source, response, data=None, k=key, c=client, n=name: finish_request(k, c, n, response), None)
                except Exception as error:
                    records[key].update(status='error', error=str(error))
                    pending.discard(key)
            if pending and time.monotonic() < deadline_at and not self.stop.is_set():
                loop.run()
        finally:
            closed = True
            cancel.cancel()
            timer.destroy()
            signal.destroy()
            self.context.pop_thread_default()
            self.collection_deadline = previous_deadline
        for key in pending:
            records[key].update(status='timeout', error='Collection budget exhausted',
                                duration_ms=(time.monotonic() - records[key]['sent_monotonic']) * 1000)
        report = {'requests': records, 'budget_seconds': budget,
                  'duration_ms': (time.monotonic() - started) * 1000,
                  'complete': all(record['status'] == 'ok' for record in records.values())}
        return outputs, report

    def release(self, service):
        client = self.clients.pop(service, None)
        if client is not None:
            self.wait(self.device.release_client, self.device.release_client_finish, client,
                      Qmi.DeviceReleaseClientFlags.RELEASE_CID, 3)

    def close(self, *, release_clients=True, preserve_cids=()):
        """Drop local clients, retaining firmware CIDs needed for owned recovery."""
        self.cleaning = True
        try:
            for service, client in list(self.clients.items()):
                flags = (Qmi.DeviceReleaseClientFlags.RELEASE_CID
                         if release_clients and service not in preserve_cids
                         else Qmi.DeviceReleaseClientFlags.NONE)
                try:
                    self.wait(self.device.release_client, self.device.release_client_finish, client,
                              flags, 3)
                except Exception as error:
                    logging.getLogger(__name__).warning("Cannot release QMI client: %s", error)
            if self.device is not None:
                try:
                    self.wait(self.device.close_async, self.device.close_finish, 3)
                except Exception as error:
                    logging.getLogger(__name__).warning("Cannot close QMI device: %s", error)
        finally:
            self.device = None
            self.clients.clear()
            self.cleaning = False


def optional(output, getter, default=None):
    """A missing optional TLV must not discard the other fields of a response."""
    try:
        return getattr(output, getter)()
    except (GLib.Error, AttributeError):
        return default
