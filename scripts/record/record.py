import os
import subprocess
import threading
import time
import socket
import signal
from datetime import datetime, time as dtime

RTSP_URL = os.environ.get("RTSP_URL", "rtsp://USER:PASSWORD@CAMERA_IP:554/profile1")
SAVE_DIR = os.environ.get("VIDEO_SAVE_DIR", "/mnt/SSD/video")
LOG_DIR = os.environ.get("VIDEO_LOG_DIR", "/mnt/SSD/log")
CAMERA_IP = os.environ.get("CAMERA_IP", "CAMERA_IP")
DURATION = 3600
PING_INTERVAL = 5
INTERNET_PING_INTERVAL = 5
START_TIME = dtime(7, 0)
END_TIME = dtime(0, 0)
MAX_DAYS_VIDEO = 32
MAX_DAYS_LOG = 30

stop_flag = False

def write_log(message):
    os.makedirs(LOG_DIR, exist_ok=True)
    log_file = os.path.join(LOG_DIR, "log.txt")
    with open(log_file, "a") as f:
        f.write(f"{datetime.now()}: {message}\n")

def signal_handler(sig, frame):
    global stop_flag
    write_log("[INFO] Received termination signal. Stopping...")
    stop_flag = True

signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

def is_camera_connected():
    try:
        socket.setdefaulttimeout(2)
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.connect((CAMERA_IP, 554))
        sock.close()
        return True
    except (socket.error, socket.timeout):
        return False

def is_internet_connected(host="8.8.8.8", port=53, timeout=3):
    try:
        socket.setdefaulttimeout(timeout)
        socket.socket(socket.AF_INET, socket.SOCK_STREAM).connect((host, port))
        return True
    except socket.error:
        return False

def is_within_recording_time():
    now = datetime.now().time()
    if START_TIME < END_TIME:
        return START_TIME <= now < END_TIME
    else:
        return now >= START_TIME or now < END_TIME

def is_save_dir_ready():
    if os.access(SAVE_DIR, os.W_OK):
        return True
    else:
        write_log(f"[ERROR] {SAVE_DIR} is not writable.")
        return False

def delete_old_folders(base_path, days_old=MAX_DAYS_VIDEO):
    now = datetime.now()
    if not os.path.exists(base_path):
        return
    for folder_name in os.listdir(base_path):
        folder_path = os.path.join(base_path, folder_name)
        if os.path.isdir(folder_path):
            try:
                try:
                    folder_date = datetime.strptime(folder_name, "%Y-%m-%d")
                except ValueError:
                    continue
                if (now - folder_date).days >= days_old:
                    subprocess.run(["rm", "-rf", folder_path], check=True)
                    write_log(f"[DELETE] Removed old folder: {folder_path}")
            except (subprocess.CalledProcessError) as e:
                write_log(f"[ERROR] Failed to delete folder {folder_path}: {e}")
                continue

def delete_old_logs(days_old=MAX_DAYS_LOG):
    now = datetime.now()
    log_file = os.path.join(LOG_DIR, "log.txt")
    if os.path.exists(log_file):
        if (now - datetime.fromtimestamp(os.path.getmtime(log_file))).days >= days_old:
            os.remove(log_file)
            write_log("[DELETE] Old log file removed.")

def record_video():
    while not stop_flag:
        delete_old_folders(SAVE_DIR)
        delete_old_logs()
        if not is_within_recording_time():
            time.sleep(300)
            continue
        if not is_save_dir_ready():
            time.sleep(10)
            continue
        if not is_camera_connected():
            time.sleep(PING_INTERVAL)
            continue
        today = datetime.now().strftime("%Y-%m-%d")
        daily_save_dir = os.path.join(SAVE_DIR, today)
        os.makedirs(daily_save_dir, exist_ok=True)
        processed_dir = os.path.join(daily_save_dir, "processed")
        os.makedirs(processed_dir, exist_ok=True)
        now_str = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        final_filename = os.path.join(daily_save_dir, f"{now_str}.mp4")
        temp_filename = f"{final_filename}.part"
        cmd = ["ffmpeg", "-rtsp_transport", "tcp", "-i", RTSP_URL, "-t", str(DURATION), "-vcodec", "copy", "-an", "-f", "mp4", "-y", temp_filename]
        write_log(f"[INFO] Start recording: {final_filename}")
        try:
            subprocess.run(cmd, check=True, capture_output=True, text=True)
            if os.path.exists(temp_filename):
                os.rename(temp_filename, final_filename)
                write_log(f"[RECORDING] Saved video file: {final_filename}")
            else:
                write_log(f"[ERROR] Temp file not found after successful run: {temp_filename}")
        except subprocess.CalledProcessError as e:
            write_log(f"[DISCONNECTED] FFmpeg error during recording for {final_filename}.")
            write_log(f"FFmpeg stderr: {e.stderr}")
            if os.path.exists(temp_filename):
                try:
                    os.remove(temp_filename)
                except OSError:
                    pass
            time.sleep(5)
        time.sleep(1)

def monitor_status(check_function, log_prefix, interval):
    status_is_ok = False
    while not stop_flag:
        current_status = check_function()
        if current_status and not status_is_ok:
            write_log(f"[{log_prefix}_RECONNECTED] Connection restored.")
            status_is_ok = True
        elif not current_status and status_is_ok:
            write_log(f"[{log_prefix}_DISCONNECTED] Connection lost.")
            status_is_ok = False
        time.sleep(interval)

if __name__ == "__main__":
    os.makedirs(SAVE_DIR, exist_ok=True)
    os.makedirs(LOG_DIR, exist_ok=True)
    camera_thread = threading.Thread(target=monitor_status, args=(is_camera_connected, "CAMERA", PING_INTERVAL), daemon=True)
    internet_thread = threading.Thread(target=monitor_status, args=(is_internet_connected, "INTERNET", INTERNET_PING_INTERVAL), daemon=True)
    camera_thread.start()
    internet_thread.start()
    record_video()
