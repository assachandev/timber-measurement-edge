import csv
import json
import os
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Deque, Dict, List, Optional, Tuple

import paho.mqtt.client as mqtt

BROKER = os.environ.get("MQTT_BROKER", "YOUR_BROKER_IP")
PORT = int(os.environ.get("MQTT_PORT", 1883))
TOPIC = os.environ.get("MQTT_TOPIC", "YOUR_MQTT_TOPIC")
QOS = 2
CSV_FILENAME = "final-measurement.csv"
STATE_FILENAME = ".mqtt_publisher.offset"
POLL_INTERVAL_S = 1.0
MAX_INFLIGHT = 10
LAST_SENT_PATH = "/tmp/mqtt_publisher_last_sent"

DATA_DIR = Path(os.environ.get("DATA_DIR", "data"))
CSV_PATH = DATA_DIR / CSV_FILENAME
STATE_PATH = DATA_DIR / STATE_FILENAME


def load_offset() -> int:
    if STATE_PATH.exists():
        try:
            return int(STATE_PATH.read_text().strip())
        except ValueError:
            return 0
    return 0


def save_offset(offset: int) -> None:
    STATE_PATH.write_text(str(offset))


def ensure_header_skipped(offset: int) -> int:
    if not CSV_PATH.exists():
        return 0
    if offset > CSV_PATH.stat().st_size:
        offset = 0
    if offset == 0:
        with CSV_PATH.open("r", newline="") as fh:
            fh.readline()
            offset = fh.tell()
        save_offset(offset)
    return offset


def parse_csv_line(line: str) -> Optional[dict]:
    if not line.strip():
        return None
    row = next(csv.reader([line]))
    if len(row) < 7 or row[0].strip().lower() == "date":
        return None
    date_part = row[0].strip()
    time_part = row[1].strip()
    count_raw = row[2].strip()
    lenght_raw = row[3].strip()
    count_val = float(count_raw) if count_raw else None
    lenght_val = float(lenght_raw) if lenght_raw else None
    avg_mm = float(row[4]) if row[4] else None
    max_mm = float(row[5]) if row[5] else None
    min_mm = float(row[6]) if row[6] else None
    return {
        "date": date_part,
        "time": time_part,
        "count": count_val,
        "length": lenght_val,
        "min_mm": min_mm,
        "max_mm": max_mm,
        "avg_mm": avg_mm,
    }


class CsvMqttPublisher:
    def __init__(self) -> None:
        self.client = mqtt.Client()
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_publish = self._on_publish
        self.client.reconnect_delay_set(min_delay=1, max_delay=30)
        self.connected = False
        self.pending: Deque[Tuple[dict, int]] = deque()
        self.inflight: Dict[int, int] = {}
        self.read_offset = ensure_header_skipped(load_offset())
        self.committed_offset = load_offset()

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        self.connected = True
        print(f"[MQTT] Connected (rc={reason_code}). Pending={len(self.pending)}")

    def _on_disconnect(self, client, userdata, reason_code, properties=None):
        self.connected = False
        if reason_code != mqtt.MQTT_ERR_SUCCESS:
            print(f"[MQTT] Unexpected disconnect (rc={reason_code}). Reconnecting...")

    def _on_publish(self, client, userdata, mid: int):
        offset = self.inflight.pop(mid, None)
        if offset is not None:
            self.committed_offset = max(self.committed_offset, offset)
            save_offset(self.committed_offset)
            with open(LAST_SENT_PATH, "w") as f:
                f.write(datetime.now().isoformat())
            print(f"[MQTT] Published message mid={mid}, offset={offset}")

    def collect_new_rows(self) -> None:
        if not CSV_PATH.exists():
            return
        with CSV_PATH.open("r") as fh:
            fh.seek(self.read_offset)
            while True:
                line = fh.readline()
                if not line:
                    break
                offset_after_line = fh.tell()
                parsed = parse_csv_line(line)
                if parsed:
                    payload = self._build_payload(parsed)
                    self.pending.append((payload, offset_after_line))
                    print(f"[QUEUE] Enqueued CSV row @ offset {offset_after_line}")
                self.read_offset = offset_after_line

    def _build_payload(self, parsed_row: dict) -> dict:
        return {
            "date": parsed_row.get("date", ""),
            "time": parsed_row.get("time", ""),
            "count": parsed_row.get("count"),
            "length_mm": parsed_row.get("length"),
            "avg_mm": parsed_row.get("avg_mm"),
            "max_mm": parsed_row.get("max_mm"),
            "min_mm": parsed_row.get("min_mm"),
        }

    def flush_queue(self) -> None:
        if not self.connected:
            return
        while self.pending and len(self.inflight) < MAX_INFLIGHT:
            payload, offset = self.pending[0]
            result = self.client.publish(TOPIC, json.dumps(payload), qos=QOS, retain=False)
            if result.rc == mqtt.MQTT_ERR_SUCCESS:
                self.inflight[result.mid] = offset
                self.pending.popleft()
                print(f"[MQTT] Publishing mid={result.mid}, queue={len(self.pending)}, inflight={len(self.inflight)}")
            else:
                print(f"[MQTT] Publish failed rc={result.rc}, will retry.")
                break

    def run(self) -> None:
        self.client.connect(BROKER, PORT, keepalive=60)
        self.client.loop_start()
        try:
            while True:
                self.collect_new_rows()
                self.flush_queue()
                time.sleep(POLL_INTERVAL_S)
        except KeyboardInterrupt:
            print("[EXIT] Stopping MQTT publisher.")
        finally:
            self.client.loop_stop()
            self.client.disconnect()


if __name__ == "__main__":
    publisher = CsvMqttPublisher()
    if not CSV_PATH.exists():
        raise FileNotFoundError(f"CSV file '{CSV_PATH}' not found. Run sensor collection first.")
    publisher.run()
