#!/usr/bin/env python3
import csv
import json
import logging
import logging.handlers
import os
import signal
import sys
import time
from argparse import ArgumentParser
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path
from threading import Lock
from typing import Dict, List, Optional, Tuple

import yaml


class Config:
    def __init__(self, config_path: str):
        with open(config_path, 'r') as f:
            self.data = yaml.safe_load(f)

    def get(self, key: str, default=None):
        keys = key.split('.')
        value = self.data
        for k in keys:
            if isinstance(value, dict):
                value = value.get(k)
            else:
                return default
        return value if value is not None else default


def setup_logging(log_dir: str, log_level: str = "INFO") -> logging.Logger:
    log_path = Path(log_dir) / "combine-measurement.log"
    logger = logging.getLogger("CombineMeasurement")
    logger.setLevel(getattr(logging, log_level.upper()))
    handler = logging.handlers.RotatingFileHandler(log_path, maxBytes=50 * 1024 * 1024, backupCount=10)
    formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setFormatter(formatter)
    logger.addHandler(stderr_handler)
    return logger


class CountManager:
    def __init__(self, state_file: str, logger: logging.Logger):
        self.state_file = state_file
        self.logger = logger
        self.current_count = 0
        self.current_date = None
        self._load_state()

    def _load_state(self):
        try:
            if os.path.exists(self.state_file):
                with open(self.state_file, 'r') as f:
                    state = json.load(f)
                    self.current_count = state.get('count', 0)
                    self.current_date = state.get('date', None)
        except Exception as e:
            self.logger.warning(f"Failed to load count state: {e}")
            self.current_count = 0
            self.current_date = None

    def _save_state(self):
        try:
            with open(self.state_file, 'w') as f:
                json.dump({'count': self.current_count, 'date': self.current_date}, f)
        except Exception as e:
            self.logger.warning(f"Failed to save count state: {e}")

    def get_next_count(self, record_date: str) -> int:
        if self.current_date != record_date:
            self.current_date = record_date
            self.current_count = 1
            self._save_state()
            self.logger.info(f"[DAILY RESET] New day detected ({record_date}). Count reset to 1.")
        else:
            self.current_count += 1
            self._save_state()
        return self.current_count


class CSVFileMonitor:
    def __init__(self, file_path: str, logger: logging.Logger):
        self.file_path = file_path
        self.logger = logger
        self.last_line_number = 0
        self.lock = Lock()
        self.state_file = Path(file_path).parent / f".{Path(file_path).name}.state"
        self._load_state()

    def _load_state(self):
        try:
            if self.state_file.exists():
                with open(self.state_file, 'r') as f:
                    state = json.load(f)
                    self.last_line_number = state.get('line_number', 0)
        except Exception as e:
            self.logger.warning(f"Failed to load state file: {e}")
            self.last_line_number = 0

    def _save_state(self):
        try:
            with open(self.state_file, 'w') as f:
                json.dump({'line_number': self.last_line_number}, f)
        except Exception as e:
            self.logger.warning(f"Failed to save state file: {e}")

    def get_new_rows(self) -> List[Dict]:
        with self.lock:
            try:
                if not os.path.exists(self.file_path):
                    self.logger.warning(f"CSV file not found: {self.file_path}")
                    return []
                rows = []
                with open(self.file_path, 'r', encoding='utf-8', newline='') as f:
                    reader = csv.DictReader(f)
                    if reader.fieldnames is None:
                        return []
                    line_num = 0
                    last_processed_line = self.last_line_number
                    for row in reader:
                        line_num += 1
                        if line_num <= self.last_line_number:
                            continue
                        normalized = {}
                        for k, v in row.items():
                            clean_key = k.strip().lstrip('~') if k else k
                            normalized[clean_key] = v
                        if any(v.strip() for v in normalized.values() if v):
                            rows.append(normalized)
                            last_processed_line = line_num
                    if last_processed_line > self.last_line_number:
                        self.last_line_number = last_processed_line
                        self._save_state()
                return rows
            except Exception as e:
                self.logger.error(f"Error reading CSV {self.file_path}: {e}")
                return []


class FIFOQueue:
    def __init__(self, max_size: int = 10000, logger: logging.Logger = None):
        self.queue = deque()
        self.max_size = max_size
        self.logger = logger or logging.getLogger("FIFOQueue")
        self.lock = Lock()

    def enqueue(self, record: Dict) -> bool:
        with self.lock:
            if len(self.queue) >= self.max_size:
                self.logger.warning(f"Queue full ({self.max_size}), dropping oldest record")
                self.queue.popleft()
            self.queue.append(record)
            return True

    def find_match(self, target_timestamp: datetime, window_before_sec: int, window_after_sec: int) -> Optional[Dict]:
        with self.lock:
            for i, record in enumerate(self.queue):
                try:
                    record_ts = datetime.strptime(f"{record['date']} {record['time']}", "%Y-%m-%d %H:%M:%S")
                    lower = target_timestamp - timedelta(seconds=window_before_sec)
                    upper = target_timestamp + timedelta(seconds=window_after_sec)
                    if lower <= record_ts <= upper:
                        matched = self.queue[i]
                        del self.queue[i]
                        return matched
                except Exception as e:
                    self.logger.error(f"Error parsing timestamp in queue: {e}")
                    continue
            return None

    def dequeue_expired(self, current_time: datetime, timeout_sec: int) -> List[Dict]:
        expired = []
        with self.lock:
            to_remove = []
            for i, record in enumerate(self.queue):
                try:
                    record_ts = datetime.strptime(f"{record['date']} {record['time']}", "%Y-%m-%d %H:%M:%S")
                    age = (current_time - record_ts).total_seconds()
                    if age > timeout_sec:
                        expired.append(record)
                        to_remove.append(i)
                except Exception as e:
                    self.logger.error(f"Error parsing timestamp: {e}")
            for i in reversed(to_remove):
                del self.queue[i]
        return expired

    def size(self) -> int:
        with self.lock:
            return len(self.queue)


class MatchingEngine:
    def __init__(self, window_before_sec: int, window_after_sec: int, logger: logging.Logger = None):
        self.window_before_sec = window_before_sec
        self.window_after_sec = window_after_sec
        self.logger = logger or logging.getLogger("MatchingEngine")

    def match(self, length_record: Dict, diameter_queue: FIFOQueue) -> Tuple[bool, Optional[Dict]]:
        try:
            if not length_record or 'date' not in length_record or 'time' not in length_record:
                return False, None
            length_ts = datetime.strptime(f"{length_record['date']} {length_record['time']}", "%Y-%m-%d %H:%M:%S")
            matched_diameter = diameter_queue.find_match(length_ts, self.window_before_sec, self.window_after_sec)
            if matched_diameter:
                return True, matched_diameter
            return False, None
        except Exception as e:
            self.logger.error(f"Error in match logic: {e}")
            return False, None


class OutputWriter:
    def __init__(self, output_path: str, logger: logging.Logger = None):
        self.output_path = output_path
        self.logger = logger or logging.getLogger("OutputWriter")
        self.lock = Lock()
        self.fieldnames = ['date', 'time', 'count', 'lenght_mm', 'avg_mm', 'max_mm', 'min_mm', 'diameter_matched', 'source_record_ids']
        self._ensure_header()

    def _ensure_header(self):
        try:
            if not os.path.exists(self.output_path):
                with open(self.output_path, 'w', newline='', encoding='utf-8') as f:
                    writer = csv.DictWriter(f, fieldnames=self.fieldnames)
                    writer.writeheader()
        except Exception as e:
            self.logger.error(f"Failed to create output CSV: {e}")

    def write(self, combined_record: Dict) -> bool:
        with self.lock:
            try:
                for field in self.fieldnames:
                    if field not in combined_record:
                        combined_record[field] = ""
                with open(self.output_path, 'a', newline='', encoding='utf-8') as f:
                    writer = csv.DictWriter(f, fieldnames=self.fieldnames)
                    writer.writerow({f: combined_record.get(f, '') for f in self.fieldnames})
                return True
            except Exception as e:
                self.logger.error(f"Failed to write output record: {e}")
                return False


def merge_records(length_record: Dict, diameter_record: Optional[Dict], matched: bool, logger: logging.Logger, count_manager: 'CountManager' = None) -> Dict:
    try:
        if not length_record or 'date' not in length_record or 'time' not in length_record:
            return None
        record_date = length_record.get('date', '')
        if count_manager:
            independent_count = count_manager.get_next_count(record_date)
        else:
            independent_count = length_record.get('count', '')
        merged = {
            'date': length_record.get('date', ''),
            'time': length_record.get('time', ''),
            'count': independent_count,
            'lenght_mm': length_record.get('lenght_mm', ''),
            'avg_mm': diameter_record.get('avg_mm', '') if diameter_record else '',
            'max_mm': diameter_record.get('max_mm', '') if diameter_record else '',
            'min_mm': diameter_record.get('min_mm', '') if diameter_record else '',
            'diameter_matched': 'true' if matched else 'false',
            'source_record_ids': _build_source_ids(length_record, diameter_record)
        }
        return merged
    except Exception as e:
        logger.error(f"Error merging records: {e}")
        return None


def _build_source_ids(length_record: Dict, diameter_record: Optional[Dict]) -> str:
    ids = [f"CAM:{length_record.get('time', 'unknown')}"]
    if diameter_record:
        ids.append(f"DIA:{diameter_record.get('time', 'unknown')}")
    return "|".join(ids)


class UnmatchedCache:
    def __init__(self, timeout_sec: int = 60, logger: logging.Logger = None):
        self.cache = {}
        self.timeout_sec = timeout_sec
        self.logger = logger or logging.getLogger("UnmatchedCache")
        self.lock = Lock()

    def add(self, record: Dict):
        with self.lock:
            timestamp_key = f"{record['date']}_{record['time']}"
            self.cache[timestamp_key] = (record, time.time())

    def get_expired(self) -> List[Dict]:
        expired = []
        current_time = time.time()
        with self.lock:
            keys_to_remove = []
            for key, (record, enqueue_time) in self.cache.items():
                if (current_time - enqueue_time) > self.timeout_sec:
                    expired.append(record)
                    keys_to_remove.append(key)
            for key in keys_to_remove:
                del self.cache[key]
        return expired

    def try_match(self, diameter_record: Dict, window_before_sec: int, window_after_sec: int) -> Optional[Tuple[str, Dict]]:
        with self.lock:
            try:
                diameter_ts = datetime.strptime(f"{diameter_record['date']} {diameter_record['time']}", "%Y-%m-%d %H:%M:%S")
                for key, (length_record, _) in self.cache.items():
                    try:
                        length_ts = datetime.strptime(f"{length_record['date']} {length_record['time']}", "%Y-%m-%d %H:%M:%S")
                        lower = length_ts - timedelta(seconds=window_before_sec)
                        upper = length_ts + timedelta(seconds=window_after_sec)
                        if lower <= diameter_ts <= upper:
                            return key, length_record
                    except Exception as e:
                        self.logger.error(f"Error parsing timestamp in cache: {e}")
                        continue
            except Exception as e:
                self.logger.error(f"Error trying to match: {e}")
            return None

    def remove(self, key: str):
        with self.lock:
            if key in self.cache:
                del self.cache[key]


class CombineMeasurementDaemon:
    def __init__(self, config: Config, logger: logging.Logger):
        self.config = config
        self.logger = logger
        self.running = True
        self.length_monitor = CSVFileMonitor(config.get('files.length_csv'), logger)
        self.diameter_monitor = CSVFileMonitor(config.get('files.diameter_csv'), logger)
        self.output_writer = OutputWriter(config.get('files.output_csv'), logger)
        self.diameter_queue = FIFOQueue(max_size=config.get('matching.max_queue_size', 10000), logger=logger)
        self.unmatched_cache = UnmatchedCache(timeout_sec=config.get('matching.timeout_seconds', 60), logger=logger)
        self.matcher = MatchingEngine(window_before_sec=config.get('matching.window_before', 30), window_after_sec=config.get('matching.window_after', 30), logger=logger)
        output_dir = os.path.dirname(config.get('files.output_csv'))
        count_state_file = os.path.join(output_dir, '.combine_measurement_count.state')
        self.count_manager = CountManager(count_state_file, logger)
        self.last_maintenance = time.time()
        signal.signal(signal.SIGTERM, self._signal_handler)
        signal.signal(signal.SIGINT, self._signal_handler)

    def _signal_handler(self, signum, frame):
        self.logger.info(f"Received signal {signum}, initiating graceful shutdown...")
        self.running = False

    def _perform_maintenance(self):
        current_time = datetime.now()
        expired_diameter = self.diameter_queue.dequeue_expired(current_time, self.config.get('matching.timeout_seconds', 60))
        for record in expired_diameter:
            merged = merge_records(record, None, False, self.logger, self.count_manager)
            if merged:
                self.output_writer.write(merged)
        expired_length = self.unmatched_cache.get_expired()
        for record in expired_length:
            merged = merge_records(record, None, False, self.logger, self.count_manager)
            if merged:
                self.output_writer.write(merged)

    def run(self):
        self.logger.info("Starting CombineMeasurement daemon...")
        self.logger.info(f"Length CSV: {self.config.get('files.length_csv')}")
        self.logger.info(f"Diameter CSV: {self.config.get('files.diameter_csv')}")
        self.logger.info(f"Output CSV: {self.config.get('files.output_csv')}")
        loop_count = 0
        while self.running:
            try:
                current_time = datetime.now()
                loop_count += 1
                if (time.time() - self.last_maintenance) > self.config.get('polling.maintenance_interval_seconds', 300):
                    self._perform_maintenance()
                    self.last_maintenance = time.time()
                diameter_rows = self.diameter_monitor.get_new_rows()
                for row in diameter_rows:
                    try:
                        self.diameter_queue.enqueue(row)
                        match_result = self.unmatched_cache.try_match(row, self.config.get('matching.window_before', 30), self.config.get('matching.window_after', 30))
                        if match_result:
                            key, length_record = match_result
                            merged = merge_records(length_record, row, True, self.logger, self.count_manager)
                            if merged:
                                self.output_writer.write(merged)
                                self.unmatched_cache.remove(key)
                                self.logger.info(f"Matched cached length {length_record['time']} with diameter {row['time']}")
                    except Exception as e:
                        self.logger.error(f"Error processing diameter row: {e}")
                length_rows = self.length_monitor.get_new_rows()
                for row in length_rows:
                    try:
                        matched, diameter_record = self.matcher.match(row, self.diameter_queue)
                        if matched and diameter_record:
                            merged = merge_records(row, diameter_record, True, self.logger, self.count_manager)
                            if merged:
                                self.output_writer.write(merged)
                                self.logger.info(f"Matched length {row['time']} with diameter {diameter_record['time']}")
                        else:
                            self.unmatched_cache.add(row)
                    except Exception as e:
                        self.logger.error(f"Error processing length row: {e}")
                time.sleep(self.config.get('polling.length_poll_interval_ms', 500) / 1000.0)
            except Exception as e:
                self.logger.error(f"Error in main loop: {e}", exc_info=True)
                time.sleep(1)
        self.logger.info("Daemon shutdown complete")
        sys.exit(0)


def main():
    parser = ArgumentParser(description="Combine length and diameter measurements")
    parser.add_argument('--config', default='scripts/measurement/config/config.yaml', help='Path to config.yaml')
    parser.add_argument('--log-level', default='INFO', choices=['DEBUG', 'INFO', 'WARNING', 'ERROR'])
    args = parser.parse_args()
    try:
        config = Config(args.config)
    except Exception as e:
        print(f"Failed to load configuration: {e}")
        sys.exit(1)
    log_dir = config.get('files.log_dir', 'logs')
    os.makedirs(log_dir, exist_ok=True)
    logger = setup_logging(log_dir, args.log_level)
    daemon = CombineMeasurementDaemon(config, logger)
    daemon.run()


if __name__ == '__main__':
    main()
