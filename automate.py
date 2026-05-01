import sys
import argparse
import time
import os
import tempfile
from xml.etree import ElementTree as ET
from datetime import datetime, timedelta, timezone

import ntplib
import adbutils
from adbutils.errors import AdbError, AdbConnectionError

TARGET_TEXT = "Apply for unlocking"
TARGET_TIMEZONE_LIVE = timedelta(hours=8)

NTP_SERVERS =[
    "time.google.com",
    "time.cloudflare.com",
    "time.apple.com",
    "time.windows.com",
    "pool.ntp.org"
]

DEVICE_XML_PATH = "/sdcard/.ui_dump.xml"
ADB_STAY_ON_KEY = "stay_on_while_plugged_in"


class MiUnlockException(Exception):
    pass


class MiUnlocker:
    def __init__(self, serial: str | None = None) -> None:
        try:
            self.client = adbutils.AdbClient(host="127.0.0.1", port=5037)
        except AdbConnectionError as e:
            raise MiUnlockException(f"Failed to connect to ADB client: {e}")

        self.device: adbutils.AdbDevice | None = None
        self.original_timeout: str | None = None
        self._connect_device(serial)

    def __enter__(self):
        try:
            self.original_timeout = self.device.shell("settings get system screen_off_timeout").strip()

            if not self.original_timeout or "null" in self.original_timeout:
                self.original_timeout = "60000"

            self.device.shell(f"settings put global {ADB_STAY_ON_KEY} 3")
            self.device.shell("settings put system screen_off_timeout 2147483647")

            print(f"[+] Screen set to stay on. Original timeout: {self.original_timeout} ms.")
        except AdbError as e:
            raise MiUnlockException(f"ADB error during screen setup: {e}")

        return self

    def _connect_device(self, serial: str | None) -> None:
        devices = self.client.device_list()
        if not devices:
            raise MiUnlockException("No devices found. Ensure ADB server is running and device is connected.")

        if serial:
            self.device = next((d for d in devices if d.serial == serial), None)
            if not self.device:
                raise MiUnlockException(f"Device with serial '{serial}' not found.")
        else:
            self.device = devices[0]

        print(f"[+] Connected to device: {self.device.serial}")

    @staticmethod
    def _find_center_coordinates(xml_file_path: str, target_text: str) -> tuple[int, int] | None:
        print(f"[*] Parsing XML to find '{target_text}' or button ID...")
        try:
            tree = ET.parse(xml_file_path)
            root = tree.getroot()
            element = root.find(f".//node[@text='{target_text}']")

            if element is None:
                element = root.find(".//node[@resource-id='com.mi.global.bbs:id/btnApply']")
            if element is None:
                return None

            bounds_str = element.get("bounds")
            if not bounds_str:
                return None

            coords = bounds_str.replace("[", "").replace("]", ",").split(",")[:-1]
            x1, y1, x2, y2 = map(int, coords)
            center_x, center_y = (x1 + x2) // 2, (y1 + y2) // 2

            print(f"[+] Found element bounds: {bounds_str}. Center: ({center_x}, {center_y})")
            return center_x, center_y
        except Exception as e:
            print(f"[-] Error parsing XML: {e}")
            return None

    def setup_ui_dump_and_find_coords(self) -> tuple[int, int] | None:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".xml") as tmp_file:
            local_xml_path = tmp_file.name

        try:
            self.device.shell(f"uiautomator dump {DEVICE_XML_PATH}")
            self.device.sync.pull(DEVICE_XML_PATH, local_xml_path)
            print("[+] Successfully pulled UI dump.")
            return self._find_center_coordinates(local_xml_path, TARGET_TEXT)
        except AdbError as e:
            print(f"[-] ADB error during UI dump: {e}")
            return None
        finally:
            if os.path.exists(local_xml_path):
                os.remove(local_xml_path)

    def execute_clicks(self, center_x: int, center_y: int, num_clicks: int, click_delay: float) -> None:
        print(f"[*] Firing ADB clicks at {datetime.now().strftime('%H:%M:%S.%f')[:-3]}...")
        try:
            shell_commands =[]
            for click_num in range(num_clicks):
                shell_commands.append(f"input tap {center_x} {center_y}")
                if click_num < num_clicks - 1:
                    shell_commands.append(f"sleep {click_delay}")

            command_batch = "; ".join(shell_commands)
            self.device.shell(command_batch)
            print("[SUCCESS] Click sequence execution finished.")
        except AdbError as e:
            print(f"[-] ADB error during click execution: {e}")

    def __exit__(self, _exc_type, _exc_value, _traceback):
        try:
            if self.original_timeout:
                self.device.shell(f"settings put system screen_off_timeout {self.original_timeout}")
                self.device.shell(f"settings put global {ADB_STAY_ON_KEY} 0")
                print("[+] Restored device screen timeout settings.")
            self.device.shell(f"rm -f {DEVICE_XML_PATH}")
        except AdbError as e:
            print(f"[-] ADB error during cleanup: {e}")


def get_ntp_sync_data(servers: list[str] = NTP_SERVERS) -> tuple[float, float]:
    best_ntp_time = None
    best_perf = None
    min_delay = float('inf')
    best_server = None

    client = ntplib.NTPClient()

    for server in servers:
        try:
            t0 = time.perf_counter()
            response = client.request(server, version=3, timeout=1.0)
            t1 = time.perf_counter()

            delay = t1 - t0

            if delay < min_delay:
                min_delay = delay
                best_ntp_time = response.tx_time
                best_perf = t1
                best_server = server

        except Exception:
            continue

    if best_ntp_time is not None:
        print(f"[+] NTP Sync: выбран сервер {best_server} (пинг: {min_delay*1000:.1f} мс)")
        return best_ntp_time, best_perf

    print("[-] ВНИМАНИЕ: Все NTP серверы недоступны (ошибка сети/таймаут).")
    print("[-] Переход на использование локальных часов вашего ПК.")
    return time.time(), time.perf_counter()


def get_target_perf_counter(target_time_str: str, target_tz_offset: timedelta) -> tuple[float, float]:
    ntp_unix, sync_perf = get_ntp_sync_data()
    current_time_utc = datetime.fromtimestamp(ntp_unix, tz=timezone.utc)
    current_time_target_tz = current_time_utc + target_tz_offset

    target_time_naive = datetime.strptime(target_time_str, "%H:%M:%S.%f").time()
    target_dt_target_tz = datetime.combine(current_time_target_tz.date(), target_time_naive, tzinfo=current_time_target_tz.tzinfo)

    if current_time_target_tz >= target_dt_target_tz:
        tomorrow = current_time_target_tz.date() + timedelta(days=1)
        target_dt_target_tz = datetime.combine(tomorrow, target_time_naive, tzinfo=current_time_target_tz.tzinfo)

    target_dt_utc = target_dt_target_tz - target_tz_offset
    target_unix = target_dt_utc.timestamp()

    time_to_wait_sec = target_unix - ntp_unix
    target_perf_counter = sync_perf + time_to_wait_sec

    return target_perf_counter, time_to_wait_sec


def wait_and_sync_to_target(target_time_str: str, target_tz_offset: timedelta) -> bool:
    print("[*] Initial NTP synchronization...")
    target_perf_counter, time_to_wait_sec = get_target_perf_counter(target_time_str, target_tz_offset)

    if time_to_wait_sec < 0:
        print("[-] Target time has already passed!")
        return False

    print(f"[+] Target time set to: {target_time_str} (TZ Offset: {target_tz_offset})")
    print(f"[+] Initial wait time: {time_to_wait_sec:.3f} seconds.")

    last_print_sec = -1
    resynced = False

    while True:
        remaining = target_perf_counter - time.perf_counter()

        if remaining < 30.0 and not resynced and time_to_wait_sec > 60:
            print("[*] Performing final high-precision NTP resync to fix clock drift...")
            target_perf_counter, _ = get_target_perf_counter(target_time_str, target_tz_offset)
            resynced = True
            continue

        if remaining <= 1.0:
            break

        current_sec = int(remaining)
        if current_sec % 5 == 0 and current_sec != last_print_sec:
            print(f"[*] ~{current_sec} seconds remaining...")
            last_print_sec = current_sec

        time.sleep(0.1)

    print("[*] Entering precision wait mode (final second spin-lock)...")
    while time.perf_counter() < target_perf_counter:
        pass

    return True


def main() -> None:
    parser = argparse.ArgumentParser(description="Automate Mi Community unlock request via ADB")
    parser.add_argument("-s", "--serial", type=str, help="Device serial number (if multiple devices are connected)")
    parser.add_argument("-c", "--clicks", type=int, default=2, help="Number of clicks (default: 2)")
    parser.add_argument("-d", "--delay", type=float, default=2.0, help="Delay between clicks in seconds (default: 2.0)")
    parser.add_argument("--test", action="store_true", help="Run in test mode")
    parser.add_argument("--test-timezone", type=int, help="Timezone offset in hours for test mode")
    parser.add_argument("--test-time", type=str, help="Target time for test mode (HH:MM:SS or HH:MM:SS.fff)")
    parser.add_argument("--offset-ms", type=int, default=-365, help="Offset in milliseconds to compensate ADB latency (default: -365)")
    args = parser.parse_args()

    if args.test:
        if args.test_timezone is None or args.test_time is None:
            parser.error("--test-timezone and --test-time are required when using --test")
        target_time_str = args.test_time if "." in args.test_time else f"{args.test_time}.000"
        target_tz_offset = timedelta(hours=args.test_timezone)
        print(f"[*] Test Mode: GMT{args.test_timezone:+d}")
    else:
        base_target = datetime.strptime("23:59:59.999", "%H:%M:%S.%f") + timedelta(milliseconds=args.offset_ms - 999)
        target_time_str = base_target.strftime("%H:%M:%S.%f")[:-3]
        target_tz_offset = TARGET_TIMEZONE_LIVE
        print("[*] Live Mode: Beijing Time (GMT+8)")
        print(f"[*] ADB Latency Compensation: {args.offset_ms}ms")

    try:
        with MiUnlocker(serial=args.serial) as unlocker:
            click_coords = unlocker.setup_ui_dump_and_find_coords()
            if not click_coords:
                print("[-] Could not determine click coordinates. Ensure the app is open and button is visible.")
                return

            center_x, center_y = click_coords

            if wait_and_sync_to_target(target_time_str, target_tz_offset):
                unlocker.execute_clicks(center_x, center_y, args.clicks, args.delay)
                time.sleep(5)

    except MiUnlockException as e:
        print(f"[-] Initialization Error: {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        print("\n[-] Cancelled by user.")
        sys.exit(0)

if __name__ == "__main__":
    main()
