import csv
import os
import time
import json
from datetime import datetime
from pymodbus.client import ModbusSerialClient

HORIZON_SPAN_MM = 888
VERTICAL_SPAN_MM = 810
AXIS_SPAN_MM = {"horizontal": HORIZON_SPAN_MM, "vertical": VERTICAL_SPAN_MM}
SENSOR_RANGE_MM = (200, 600)
STALL_WINDOW = 5
STALL_TOLERANCE_MM = 0.2
SENSORS = {
    1: {"label": "Left", "axis": "horizontal"},
    2: {"label": "Bottom", "axis": "vertical"},
    3: {"label": "Top", "axis": "vertical"},
    4: {"label": "Right", "axis": "horizontal"},
}

DATA_DIR = os.environ.get("DATA_DIR", "data")


def decode_distance(value32):
    magnitude = value32 & 0xFFFFFF
    raw_um = -magnitude if (value32 & 0x80000000) else magnitude
    return 400 + (raw_um / 1000.0)


def read_distance_mm(sensor_id, address):
    result = client.read_holding_registers(address=address, count=2, device_id=sensor_id)
    if result.isError():
        return None
    regs = result.registers
    value32 = (regs[0] << 16) | regs[1]
    return decode_distance(value32)


def classify(distance_mm):
    low, high = SENSOR_RANGE_MM
    return "OK" if distance_mm is not None and low <= distance_mm <= high else "BLIND"


def valid_reading(distance_mm):
    return distance_mm is not None and distance_mm >= 0


STATE_FILE = os.path.join(DATA_DIR, "sensor_S4_count_state.json")


def load_count_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, 'r') as f:
                state = json.load(f)
                return state.get('count', 0), state.get('date', None)
        except Exception as e:
            print(f"[WARN] Failed to load state file: {e}")
            return 0, None
    return 0, None


def save_count_state(count, date):
    try:
        with open(STATE_FILE, 'w') as f:
            json.dump({'count': count, 'date': date}, f)
    except Exception as e:
        print(f"[WARN] Failed to save state file: {e}")


def get_daily_count(current_date, current_count, current_saved_date):
    if current_saved_date != current_date:
        return 1, current_date
    else:
        return current_count + 1, current_date


def in_window(distance_mm):
    low, high = SENSOR_RANGE_MM
    return valid_reading(distance_mm) and low <= distance_mm <= high


def log_in_frame(readings, axis):
    ids = [sid for sid, meta in SENSORS.items() if meta["axis"] == axis]
    return all(in_window(readings.get(sensor_id)) for sensor_id in ids)


def stalled(samples):
    if len(samples) < STALL_WINDOW:
        return False
    left_vals = [pair[0] for pair in samples]
    right_vals = [pair[1] for pair in samples]
    return (
        max(left_vals) - min(left_vals) <= STALL_TOLERANCE_MM
        and max(right_vals) - min(right_vals) <= STALL_TOLERANCE_MM
    )


def summarize(label, values):
    if not values:
        print(f"[SUMMARY] No {label} data collected.")
        return
    minimum = min(values)
    maximum = max(values)
    average = sum(values) / len(values)
    print(f"[SUMMARY] {label}: samples={len(values)} min={minimum:.2f} mm max={maximum:.2f} mm avg={average:.2f} mm")


def save_csv(basename, header, rows):
    if not rows:
        return
    filename = os.path.join(DATA_DIR, basename)
    write_header = not os.path.exists(filename)
    with open(filename, mode="a", newline="") as f:
        writer = csv.writer(f)
        if write_header:
            writer.writerow(header)
        writer.writerows(rows)


def save_diameter_logs(log_date, log_time, horiz, vert, combined, count=None):
    def stats(values):
        if not values:
            return None
        count_val = len(values)
        minimum = min(values)
        maximum = max(values)
        average = sum(values) / count_val
        return [log_date, log_time, str(count) if count is not None else "", "", f"{average:.2f}", f"{maximum:.2f}", f"{minimum:.2f}"]

    h_stats = stats(horiz)
    v_stats = stats(vert)
    c_stats = stats(combined)

    if h_stats:
        save_csv("diameter_horizontal.csv", ["date", "time", "count", "lenght_mm", "avg_mm", "max_mm", "min_mm"], [h_stats])
    if v_stats:
        save_csv("diameter_vertical.csv", ["date", "time", "count", "lenght_mm", "avg_mm", "max_mm", "min_mm"], [v_stats])
    if c_stats:
        save_csv("diameter_combined.csv", ["date", "time", "count", "lenght_mm", "avg_mm", "max_mm", "min_mm"], [c_stats])


client = ModbusSerialClient(port="/dev/ttyACM0", baudrate=9600, parity="N", stopbits=1, bytesize=8, timeout=1)

if client.connect():
    try:
        ADDRESS = 0x9C50
        INTERVAL_S = 0.5

        print("4-SENSOR MONITORING - Press Ctrl+C to stop")
        print("=" * 70)

        saved_count, saved_date = load_count_state()
        now_struct = time.localtime()
        current_date = time.strftime("%Y-%m-%d", now_struct)
        if saved_date != current_date:
            saved_count = 1
            saved_date = current_date
            save_count_state(saved_count, current_date)

        daily_count = saved_count
        horizontal_history = []
        log_active = False
        horizontal_data = []
        vertical_data = []

        while True:
            now_struct = time.localtime()
            current_date = time.strftime("%Y-%m-%d", now_struct)
            current_time = time.strftime("%H:%M:%S", now_struct)

            if saved_date != current_date:
                daily_count = 1
                saved_date = current_date
                save_count_state(daily_count, current_date)
                print(f"\n[{current_time}] [DAILY RESET] New day detected. Count reset to 1.")

            readings = {sensor_id: read_distance_mm(sensor_id, ADDRESS) for sensor_id in SENSORS}

            print(f"\n[{current_time}] [Count: {daily_count}] Sensor snapshot:")
            for sensor_id, meta in SENSORS.items():
                distance = readings[sensor_id]
                status = classify(distance)
                value_txt = f"{distance:7.2f} mm" if distance is not None else "ERROR"
                print(f"  [{meta['label']:>6}] ID:{sensor_id} → {value_txt} ({status})")

            horizontal_ready = log_in_frame(readings, "horizontal")
            vertical_ready = log_in_frame(readings, "vertical")

            if horizontal_ready:
                horizontal_history.append((readings[1], readings[4]))
                if len(horizontal_history) > STALL_WINDOW:
                    horizontal_history.pop(0)
            else:
                horizontal_history.clear()

            if not log_active and horizontal_ready:
                if stalled(horizontal_history):
                    print("[INFO] Horizontal sensors stable -> log stopped, waiting...")
                else:
                    log_active = True
                    horizontal_data.clear()
                    vertical_data.clear()
                    print(f"[INFO] Log detected (Count: {daily_count}), starting measurement.")

            if log_active and stalled(horizontal_history):
                print("[WARN] Log stalled mid-frame, discarding this pass.")
                log_active = False
                horizontal_data.clear()
                vertical_data.clear()
                continue

            if log_active and horizontal_ready:
                right = readings[4]
                left = readings[1]
                if in_window(right) and in_window(left):
                    diameter_h = HORIZON_SPAN_MM - (right + left)
                    if diameter_h >= 0:
                        horizontal_data.append(diameter_h)
                        print(f"[MEASURE] [Count: {daily_count}] Horizontal diameter = {diameter_h:.2f} mm (R={right:.2f} mm, L={left:.2f} mm)")
                    else:
                        print(f"[WARN] Ignoring negative horizontal diameter {diameter_h:.2f} mm (R={right:.2f}, L={left:.2f})")

                if vertical_ready:
                    top = readings[3]
                    bottom = readings[2]
                    if in_window(top) and in_window(bottom):
                        diameter_v = VERTICAL_SPAN_MM - (top + bottom)
                        if diameter_v >= 0:
                            vertical_data.append(diameter_v)
                            print(f"[MEASURE] [Count: {daily_count}] Vertical diameter = {diameter_v:.2f} mm (T={top:.2f} mm, B={bottom:.2f})")
                        else:
                            print(f"[WARN] Ignoring negative vertical diameter {diameter_v:.2f} mm (T={top:.2f}, B={bottom:.2f})")

            if log_active and not horizontal_ready:
                print(f"[INFO] [Count: {daily_count}] Log exited frame; finalising measurements.")
                summarize("Horizontal diameters", horizontal_data)
                summarize("Vertical diameters", vertical_data)
                combined = horizontal_data + vertical_data
                summarize("Combined diameters", combined)
                save_diameter_logs(current_date, current_time, horizontal_data, vertical_data, combined, daily_count)
                print(f"[INFO] [Count: {daily_count}] Diameters saved to CSV (horizontal, vertical, combined).")
                daily_count += 1
                saved_count = daily_count
                save_count_state(saved_count, current_date)
                log_active = False
                horizontal_data.clear()
                vertical_data.clear()

            time.sleep(INTERVAL_S)

    except KeyboardInterrupt:
        print("\n\nStopped by user")
    finally:
        client.close()
else:
    print("Connection failed")
