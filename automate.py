# HyperOS Universal Sniper (API + ADB Hybrid)
# Copyright (C) 2026 Arseniy1002
# Licensed under the GNU General Public License v3.0

import sys
import argparse
import time
import os
import tempfile
import threading
import hashlib
import random
import json
import webbrowser
from xml.etree import ElementTree as ET
from datetime import datetime, timedelta, timezone

import ntplib
import urllib3
import adbutils
from adbutils.errors import AdbConnectionError
from colorama import init, Fore, Style

init(autoreset=True)

# --- CONSTANTS ---
TARGET_TEXT = "Apply for unlocking"
BEIJING_TZ = timedelta(hours=8)
DEVICE_XML_PATH = "/sdcard/.ui_dump.xml"
ADB_STAY_ON_KEY = "stay_on_while_plugged_in"
API_URL_AUTH = "https://sgp-api.buy.mi.com/bbs/api/global/apply/bl-auth"
API_URL_PREWARM = "https://sgp-api.buy.mi.com/generate_204"
API_URL_STATUS = "https://sgp-api.buy.mi.com/bbs/api/global/user/bl-switch/state"

NTP_SERVERS = ["time.google.com", "time.cloudflare.com", "pool.ntp.org"]
PRINT_LOCK = threading.Lock()

def safe_print(text):
    with PRINT_LOCK:
        print(text)

class MiSession:
    def __init__(self, threads):
        self.http = urllib3.PoolManager(
            maxsize=threads + 10,
            retries=False,
            timeout=urllib3.Timeout(connect=3.0, read=5.0)
        )

    def pre_warm(self, threads_count):
        def _warm():
            try: self.http.request('GET', API_URL_PREWARM, timeout=2.5)
            except: pass

        threads = []
        for _ in range(threads_count):
            t = threading.Thread(target=_warm)
            threads.append(t)
            t.start()
        for t in threads: t.join()

    def make_request(self, method, url, headers=None, body=None):
        try:
            request_headers = {'Content-Type': 'application/json; charset=utf-8'}
            if headers: request_headers.update(headers)
            if method == 'POST':
                if body is None: body = '{"is_retry":true}'.encode('utf-8')
                request_headers.update({
                    'Content-Length': str(len(body)),
                    'User-Agent': 'okhttp/4.12.0',
                    'Connection': 'keep-alive'
                })
            return self.http.request(method, url, headers=request_headers, body=body, preload_content=False)
        except: return None

def generate_device_id():
    return hashlib.sha1(f"{random.random()}-{time.time()}".encode()).hexdigest().upper()

class DeviceManager:
    def __init__(self, serial=None):
        try:
            self.client = adbutils.AdbClient(host="127.0.0.1", port=5037)
        except AdbConnectionError:
            safe_print(f"{Fore.RED}[-] ADB Server not found. Run 'adb start-server'.")
            sys.exit(1)

        devices = self.client.device_list()
        if not devices:
            safe_print(f"{Fore.RED}[-] No devices connected.")
            sys.exit(1)

        self.device = next((d for d in devices if d.serial == serial), None) if serial else devices[0]
        self.original_timeout = None
        safe_print(f"{Fore.GREEN}[+] Connected to: {self.device.serial}")

    def __enter__(self):
        self.original_timeout = self.device.shell("settings get system screen_off_timeout").strip()
        self.device.shell(f"settings put global {ADB_STAY_ON_KEY} 3")
        self.device.shell("settings put system screen_off_timeout 2147483647")
        return self

    def find_button_coords(self):
        with tempfile.NamedTemporaryFile(delete=False, suffix=".xml") as tmp:
            local_xml = tmp.name
        try:
            self.device.shell(f"uiautomator dump {DEVICE_XML_PATH}")
            self.device.sync.pull(DEVICE_XML_PATH, local_xml)
            tree = ET.parse(local_xml)
            elem = tree.getroot().find(f".//node[@text='{TARGET_TEXT}']") or \
                   tree.getroot().find(".//node[@resource-id='com.mi.global.bbs:id/btnApply']")
            if elem is None: return None
            bounds = elem.get("bounds").replace("[", "").replace("]", ",").split(",")[:-1]
            x1, y1, x2, y2 = map(int, bounds)
            return (x1 + x2) // 2, (y1 + y2) // 2
        finally:
            if os.path.exists(local_xml): os.remove(local_xml)

    def tap(self, x, y):
        self.device.shell(f"input tap {x} {y}")

    def __exit__(self, *args):
        if self.original_timeout:
            self.device.shell(f"settings put system screen_off_timeout {self.original_timeout}")
            self.device.shell(f"settings put global {ADB_STAY_ON_KEY} 0")

def check_account(session, token, device_id):
    headers = {"Cookie": f"new_bbs_serviceToken={token};versionCode=500411;versionName=5.4.11;deviceId={device_id};"}
    resp = session.make_request('GET', API_URL_STATUS, headers=headers)
    if resp:
        data = json.loads(resp.data.decode('utf-8'))
        if data.get("code") == 100004:
            safe_print(f"{Fore.RED}[-] Token expired or invalid.")
            return False
        safe_print(f"{Fore.GREEN}[+] Account validated.")
        return True
    return False

def get_ntp_data():
    client = ntplib.NTPClient()
    for srv in NTP_SERVERS:
        try:
            t0 = time.perf_counter()
            res = client.request(srv, version=3, timeout=1.5)
            t1 = time.perf_counter()
            safe_print(f"{Fore.GREEN}[+] NTP Sync: {srv} (Ping: {(t1-t0)*1000:.1f}ms)")
            return res.tx_time, t1
        except: continue
    return time.time(), time.perf_counter()

def calculate_target(offset_delta):
    ntp_unix, sync_perf = get_ntp_data()
    now_utc = datetime.fromtimestamp(ntp_unix, tz=timezone.utc)
    now_bj = now_utc + offset_delta
    target_bj = (now_bj + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    target_unix = (target_bj - offset_delta).timestamp()
    return sync_perf + (target_unix - ntp_unix), target_unix - ntp_unix

def api_attack(session, token, device_id, target_perf, threads, feed, stagger):
    trigger = target_perf - feed
    while time.perf_counter() < trigger: pass

    safe_print(f"{Fore.YELLOW}[API] Flooding {threads} threads...")
    headers = {"Cookie": f"new_bbs_serviceToken={token};versionCode=500411;versionName=5.4.11;deviceId={device_id};"}

    def _req(tid):
        resp = session.make_request('POST', API_URL_AUTH, headers=headers)
        if resp:
            data = json.loads(resp.data.decode('utf-8'))
            safe_print(f"{Fore.WHITE}[T{tid}] Response: {data}")

    for i in range(threads):
        threading.Thread(target=_req, args=(i,)).start()
        if stagger > 0: time.sleep(stagger / 1000.0)

def adb_attack(manager, coords, target_perf, offset_ms):
    trigger = target_perf + (offset_ms / 1000.0)
    while time.perf_counter() < trigger: pass
    manager.tap(coords[0], coords[1])
    safe_print(f"{Fore.CYAN}[ADB] Click executed.")

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--offset", type=int, default=-365, help="ADB ms offset.")
    parser.add_argument("--stagger", type=int, default=5, help="API thread stagger ms.")
    parser.add_argument("--threads", type=int, default=15, help="API threads count.")
    parser.add_argument("--feed", type=float, default=0.400, help="API start lead time (sec).")
    parser.add_argument("--test", action="store_true", help="Run attack in 2 seconds.")
    parser.add_argument("--serial", type=str, help="Specific device serial.")
    args = parser.parse_args()

    print(f"{Fore.MAGENTA}{Style.BRIGHT}--- HyperOS Universal Sniper ---")

    # --- MODE SELECTION ---
    print(f"\n{Fore.WHITE}Select Execution Mode:")
    print(f"{Fore.CYAN}[1] Hybrid (API + ADB) - Recommended")
    print(f"{Fore.CYAN}[2] API Only")
    print(f"{Fore.CYAN}[3] ADB Only")
    mode = input(f"{Fore.YELLOW}Choice [1-3]: ").strip() or "1"

    token = ""
    if mode in ["1", "2"]:
        webbrowser.open("https://account.xiaomi.com/fe/service/login/password?_locale=en_IN&sid=18n_bbs_global")
        token = input(f"{Fore.CYAN}Enter new_bbs_serviceToken: ").strip()

    session = MiSession(args.threads)
    device_id = generate_device_id()

    # Проверка аккаунта только если выбран API
    if mode in ["1", "2"] and not args.test:
        if not check_account(session, token, device_id):
            sys.exit(1)

    # Инициализация ADB если нужно
    manager = None
    coords = None
    if mode in ["1", "3"]:
        manager = DeviceManager(args.serial)
        manager.__enter__()
        coords = manager.find_button_coords()
        if not coords:
            safe_print(f"{Fore.RED}[-] Button not found on screen.")
            manager.__exit__()
            return

    try:
        if args.test:
            target_perf = time.perf_counter() + 2.0
            wait_sec = 2.0
        else:
            target_perf, wait_sec = calculate_target(BEIJING_TZ)
            safe_print(f"{Fore.GREEN}[*] Waiting {wait_sec:.2f}s for 00:00:00 Beijing.")

        warmed = False
        while (target_perf - time.perf_counter()) > 0:
            rem = target_perf - time.perf_counter()
            # Преварм только если выбран API
            if mode in ["1", "2"] and rem < 3.5 and not warmed:
                safe_print(f"{Fore.BLUE}[*] Pre-warming SSL sockets...")
                session.pre_warm(args.threads)
                warmed = True

            if rem > 1: time.sleep(0.1)
            else: break

        safe_print(f"{Fore.RED}[!] ATTACK ENGAGED (Mode: {mode})")

        threads = []
        if mode in ["1", "2"]:
            api_t = threading.Thread(target=api_attack, args=(session, token, device_id, target_perf, args.threads, args.feed, args.stagger))
            threads.append(api_t)

        if mode in ["1", "3"]:
            adb_t = threading.Thread(target=adb_attack, args=(manager, coords, target_perf, args.offset))
            threads.append(adb_t)

        for t in threads: t.start()
        for t in threads: t.join()

    finally:
        if manager:
            manager.__exit__()

    safe_print(f"{Fore.GREEN}[+] Finished.")

if __name__ == "__main__":
    try: main()
    except KeyboardInterrupt: os._exit(0)
