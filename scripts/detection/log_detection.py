import csv
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Set

import gi

gi.require_version("Gst", "1.0")
import hailo
from gi.repository import GLib, Gst
from detection_pipeline import (
    GStreamerDetectionApp,
)
from common.buffer_utils import get_caps_from_pad
from gstreamer.gstreamer_app import app_callback_class

# Paths configured via environment variables
LENGTH_CSV_PATH = os.environ.get("LENGTH_CSV_PATH", "data/length_data.csv")
DIAMETER_CSV_PATH = os.environ.get("DIAMETER_CSV_PATH", "data/diameter_combined.csv")


class user_app_callback_class(app_callback_class):
    def __init__(self):
        super().__init__()
        self.pixels_per_mm = 0.4414
        self.length_threshold_mm = 2200
        self.short_log_max_length_mm = 2550
        self.max_disappeared_frames = 130
        self.exit_buffer_pixels = 1
        self.horizontal_line_y_position = 400
        self.stability_pixel_tolerance = 6
        self.stable_frames_trigger = 50
        self.detection_threshold = 0.7

        self.csv_log_path = LENGTH_CSV_PATH
        self.csv_stats_path = DIAMETER_CSV_PATH

        self.active_logs: Dict[int, Any] = {}
        self.logged_tracker_ids: Set[int] = set()

        self.last_log_date = datetime.now().date()
        self.last_count = 0
        self.current_length = 0.0
        self.avg_mm = 0.0
        self.max_mm = 0.0
        self.min_mm = 0.0

        self.frame_width = 0
        self.frame_height = 0
        self.exit_line = 0
        self.horizontal_line = 0

        self._initialize_csv()
        self.last_count = self._get_last_count()

        if self.last_count == 0:
            self.last_log_date = datetime.now().date()

    def _initialize_csv(self):
        if not os.path.exists(self.csv_log_path):
            header = ["date", "time", "count", "lenght_mm", "avg_mm", "max_mm", "min_mm"]
            with open(self.csv_log_path, mode="w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow(header)

    def _get_last_count(self) -> int:
        if not os.path.exists(self.csv_log_path):
            return 0
        try:
            with open(self.csv_log_path, mode="r", encoding="utf-8") as f:
                reader = list(csv.DictReader(f))
                if not reader:
                    return 0
                last_row = reader[-1]
                last_date_str = last_row.get("date")
                if last_date_str:
                    file_date = datetime.strptime(last_date_str, "%Y-%m-%d").date()
                    if file_date < datetime.now().date():
                        print(f"INFO: New day detected from CSV. Resetting count.")
                        return 0
                    return int(last_row["count"]) if last_row["count"].isdigit() else 0
        except Exception as e:
            print(f"Warning: CSV Read error: {e}")
            return 0
        return 0

    def _read_last_statistics(self):
        if not os.path.exists(self.csv_stats_path):
            return
        try:
            with open(self.csv_stats_path, mode="r", encoding="utf-8") as f:
                last_row = None
                for last_row in csv.DictReader(f):
                    pass
                if last_row:
                    def safe_float(s):
                        try:
                            return float(s)
                        except:
                            return 0.0
                    self.avg_mm = safe_float(last_row.get("avg_mm", 0))
                    self.max_mm = safe_float(last_row.get("max_mm", 0))
                    self.min_mm = safe_float(last_row.get("min_mm", 0))
        except Exception:
            pass

    def _check_daily_reset(self):
        current_date = datetime.now().date()
        if current_date > self.last_log_date:
            print(f"--- MIDNIGHT RESET: {current_date} ---")
            self.last_count = 0
            self.last_log_date = current_date
            self.logged_tracker_ids.clear()
            self.active_logs.clear()

    def _log_data(self, length_mm: float):
        self._check_daily_reset()
        self.last_count += 1
        now = datetime.now()
        row = [now.strftime("%Y-%m-%d"), now.strftime("%H:%M:%S"), self.last_count, f"{length_mm:.2f}", f"{self.avg_mm:.2f}", f"{self.max_mm:.2f}", f"{self.min_mm:.2f}"]
        with open(self.csv_log_path, mode="a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(row)
        print(f"RECORDED | Len: {length_mm:.2f}mm | Total: {self.last_count}")
        self._read_last_statistics()


def update_log_count_overlay(pipeline, user_data):
    def set_text(name, text):
        el = pipeline.get_by_name(name)
        if el:
            el.set_property("text", text)
    set_text("log_count_overlay", f"Daily Count: {user_data.last_count}")
    set_text("log_length_overlay", f"Current Length: {user_data.current_length:.2f} mm")
    set_text("log_avg_mm_overlay", f"AVG: {user_data.avg_mm:.2f} mm")
    set_text("log_max_mm_overlay", f"MAX: {user_data.max_mm:.2f} mm")
    set_text("log_min_mm_overlay", f"MIN: {user_data.min_mm:.2f} mm")
    return True


def app_callback(pad, info, user_data):
    buffer = info.get_buffer()
    if buffer is None:
        return Gst.PadProbeReturn.OK
    user_data.increment()
    format, width, height = get_caps_from_pad(pad)
    if width is None:
        return Gst.PadProbeReturn.OK
    if user_data.frame_width == 0:
        user_data.frame_width, user_data.frame_height = width, height
        user_data.exit_line = width - user_data.exit_buffer_pixels
        user_data.horizontal_line = user_data.horizontal_line_y_position
    roi = hailo.get_roi_from_buffer(buffer)
    detections = roi.get_objects_typed(hailo.HAILO_DETECTION)
    current_frame_track_ids = set()
    for detection in detections:
        label = detection.get_label()
        bbox = detection.get_bbox()
        conf = detection.get_confidence()
        if label == "log" and conf >= user_data.detection_threshold:
            track_objs = detection.get_objects_typed(hailo.HAILO_UNIQUE_ID)
            if not track_objs:
                continue
            track_id = track_objs[0].get_id()
            current_frame_track_ids.add(track_id)
            if track_id in user_data.logged_tracker_ids:
                continue
            x1 = int(bbox.xmin() * width)
            x2 = int((bbox.xmin() + bbox.width()) * width)
            y2 = int((bbox.ymin() + bbox.height()) * height)
            curr_len_mm = (x2 - x1) / user_data.pixels_per_mm
            user_data.current_length = curr_len_mm
            if track_id not in user_data.active_logs:
                user_data.active_logs[track_id] = {"best_length": curr_len_mm, "disappeared_frames": 0, "last_length": curr_len_mm, "last_x1": x1, "stable_frames": 0}
            else:
                log = user_data.active_logs[track_id]
                log["disappeared_frames"] = 0
                log["best_length"] = max(log["best_length"], curr_len_mm)
                if abs(curr_len_mm - log["last_length"]) < user_data.stability_pixel_tolerance:
                    log["stable_frames"] += 1
                else:
                    log["stable_frames"] = 0
                log["last_length"], log["last_x1"] = curr_len_mm, x1
            log_data = user_data.active_logs[track_id]
            is_exit = x2 >= user_data.exit_line and y2 >= user_data.horizontal_line
            is_stable = (log_data["stable_frames"] >= user_data.stable_frames_trigger and log_data["best_length"] < user_data.short_log_max_length_mm)
            if (is_exit or is_stable) and log_data["best_length"] > user_data.length_threshold_mm:
                user_data._log_data(log_data["best_length"])
                user_data.logged_tracker_ids.add(track_id)
    disappeared = set(user_data.active_logs.keys()) - current_frame_track_ids
    for tid in list(disappeared):
        user_data.active_logs[tid]["disappeared_frames"] += 1
        if user_data.active_logs[tid]["disappeared_frames"] > user_data.max_disappeared_frames:
            log = user_data.active_logs[tid]
            if tid not in user_data.logged_tracker_ids and log["best_length"] > user_data.length_threshold_mm:
                user_data._log_data(log["best_length"])
                user_data.logged_tracker_ids.add(tid)
            del user_data.active_logs[tid]
    return Gst.PadProbeReturn.OK


if __name__ == "__main__":
    os.system("echo 4 | sudo tee /sys/class/thermal/cooling_device0/cur_state > /dev/null 2>&1")
    user_data = user_app_callback_class()
    app = GStreamerDetectionApp(app_callback, user_data)
    GLib.timeout_add(1000, update_log_count_overlay, app.pipeline, user_data)
    try:
        app.run()
    except KeyboardInterrupt:
        print("\nStopping application...")
        os.system("echo 0 | sudo tee /sys/class/thermal/cooling_device0/cur_state > /dev/null 2>&1")
