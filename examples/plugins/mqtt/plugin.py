#!/usr/bin/env python3
"""Publish status (retained) and events to MQTT topics."""
import json
import sys

from qmi_supervisor_sdk import PluginError, connect

try:
    import paho.mqtt.client as mqtt
except ImportError:  # pragma: no cover - depends on the host packages
    mqtt = None

DEFAULT_PREFIX = 'qmi-supervisor'


def modem_label(record):
    return str(record.get('log_label') or record.get('id') or 'unknown')


def main():
    bus = connect()
    config = bus.settings()
    if mqtt is None:
        bus.log('mqtt: python3-paho-mqtt is not installed')
        return 2
    host = config.get('host', '127.0.0.1')
    port = int(config.get('port', '1883'))
    prefix = config.get('prefix', DEFAULT_PREFIX).strip('/') or DEFAULT_PREFIX
    client = mqtt.Client(client_id=config.get('client_id') or 'qmi-supervisor-plugin')
    if config.get('username'):
        client.username_pw_set(config['username'], config.get('password') or None)
    if config.get('tls', 'false').lower() == 'true':
        client.tls_set()
    client.reconnect_delay_set(1, 60)
    client.connect(host, port, keepalive=30)
    client.loop_start()
    bus.subscribe(topics=('status', 'events'), interval=int(config.get('interval', '30')))
    try:
        for message in bus.messages():
            operation = message.get('op')
            if operation == 'status':
                for record in (message.get('data') or {}).get('modems') or []:
                    topic = f'{prefix}/{modem_label(record)}/status'
                    client.publish(topic, json.dumps(record), retain=True)
            elif operation == 'event':
                event = message.get('data') or {}
                modem = str(event.get('modem') or 'unknown')
                client.publish(f'{prefix}/{modem}/events', json.dumps(event))
            elif operation == 'dropped':
                bus.log(f"mqtt: {message.get('count')} messages dropped by the bus")
    finally:
        client.loop_stop()
        client.disconnect()


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (PluginError, OSError) as error:
        print(f'mqtt: {error}', file=sys.stderr)
        raise SystemExit(2)
