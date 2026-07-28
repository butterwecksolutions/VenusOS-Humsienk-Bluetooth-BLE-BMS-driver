#!/usr/bin/env python3
"""
Venus OS BLE BMS Bridge – Humsienk / TDT (HiLink)
Version 1.0

Publishes a Humsienk (TDT/HiLink) lithium battery as
com.victronenergy.battery.* on Venus OS over Bluetooth LE.

Protocol reference: aiobmsble tdt_bms.py
Venus paths: dbus-serialbattery / Victron dbus docs

Boot: start via /data/rc.local (see README.md). Do not rely on
daemontools /service links on stock Venus OS images.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
import threading
import time
from typing import List, Optional, Tuple


def ensure_dependencies() -> None:
    try:
        import bleak  # noqa: F401
    except ImportError:
        print("[Setup] 'bleak' missing – installing …")
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", "--upgrade", "bleak"]
        )
        print("[Setup] bleak installed.")


ensure_dependencies()

from dbus.mainloop.glib import DBusGMainLoop
from gi.repository import GLib
from bleak import BleakClient, BleakScanner

sys.path.insert(1, "/opt/victronenergy/dbus-systemcalc-py/ext/velib_python")
try:
    from vedbus import VeDbusService
except ImportError:
    print("[Error] vedbus not found – is this running on Venus OS?")
    sys.exit(1)

# ═══════════════════════ Configuration (this pack type) ══════════════
MAC_ADDRESS = ""  # empty = auto-discovery

NAME_PREFIXES = ("HS", "ECO", "DCH", "TDT")
MANUFACTURER_IDS = (54976,)  # aiobmsble: 0xD6C0

UUID_NOTIFY = "0000fff1-0000-1000-8000-00805f9b34fb"
UUID_WRITE = "0000fff2-0000-1000-8000-00805f9b34fb"
UUID_AUTH = "0000fffa-0000-1000-8000-00805f9b34fb"

CELL_COUNT_DEFAULT = 16
CAPACITY_AH_DEFAULT = 314.0
MAX_CHARGE_VOLTAGE = 58.4
BATTERY_LOW_VOLTAGE = 44.8
MAX_CHARGE_CURRENT = 100.0
MAX_DISCHARGE_CURRENT = 100.0
DEVICE_INSTANCE = 512
PRODUCT_NAME = "Humsienk TDT BMS"
CUSTOM_NAME = "48V314AH"

POLL_INTERVAL_S = 2
STALE_TIMEOUT_S = 30
SCAN_TIMEOUT_S = 12.0
RESCAN_INTERVAL_S = 30

CELL_HIGH_V = 3.65
CELL_LOW_V = 2.80
CELL_IMBALANCE_V = 0.030
TEMP_HIGH_C = 55.0
TEMP_LOW_C = -10.0
SOC_LOW_PCT = 10.0
MAX_CELLS = 32
CELL_POS = 0x08

# aiobmsble: alternative command heads
CMD_HEADS = (0x7E, 0x1E)


# ═══════════════════════ TDT protocol ════════════════════════════════
def _crc_modbus(data: bytes) -> int:
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if (crc & 1) else crc >> 1
    return crc & 0xFFFF


def tdt_cmd(
    sub: int,
    payload: bytes = b"",
    ver: int = 0x00,
    head: int = 0x7E,
) -> bytes:
    body = bytes([head, ver, 0x01, 0x03, 0x00, sub])
    body += len(payload).to_bytes(2, "big") + payload
    return body + _crc_modbus(body).to_bytes(2, "big") + b"\x0d"


def frame_ok(data: bytes) -> bool:
    if len(data) < 10 or data[0] != 0x7E or data[-1] != 0x0D:
        return False
    if data[1] not in (0x00, 0x04):
        return False
    if data[4] != 0:
        print(f"[TDT] BMS frame error code: 0x{data[4]:02X}")
        return False
    calc = _crc_modbus(data[:-3])
    recv = int.from_bytes(data[-3:-1], "big")
    if calc != recv:
        print(f"[TDT] CRC mismatch calc={calc:04X} recv={recv:04X}")
        return False
    return True


def build_poll_commands(head: int = 0x7E) -> List[bytes]:
    # 0x8C required; 0x8D optional (some TDT devices never reply – HA #717)
    return [
        tdt_cmd(0x8C, bytes.fromhex("00050100200020"), ver=0x01, head=head),
        tdt_cmd(0x8D, head=head),
    ]


def build_init_commands(head: int = 0x7E) -> List[bytes]:
    return [
        tdt_cmd(0x1E, head=head),
        tdt_cmd(0x92, head=head),
    ]


shutdown_event = asyncio.Event()


# ═══════════════════════ Discovery ════════════════════════════════════
def _name_matches(name: Optional[str]) -> bool:
    if not name:
        return False
    upper = name.upper()
    return any(upper.startswith(p) for p in NAME_PREFIXES)


async def discover_bms_mac() -> Optional[Tuple[str, str]]:
    print(
        f"[Scan] Looking for BMS ({', '.join(NAME_PREFIXES)}* / mfg {MANUFACTURER_IDS}) "
        f"{SCAN_TIMEOUT_S:.0f}s …"
    )
    try:
        results = await BleakScanner.discover(
            timeout=SCAN_TIMEOUT_S, return_adv=True
        )
    except Exception as e:
        print(f"[Scan] Error: {e}")
        return None

    candidates = []
    for addr, (dev, adv) in results.items():
        name = dev.name or getattr(adv, "local_name", None) or ""
        mfg = getattr(adv, "manufacturer_data", {}) or {}
        match = _name_matches(name)
        if not match and mfg:
            match = any(mid in mfg for mid in MANUFACTURER_IDS)
        if match:
            rssi = getattr(adv, "rssi", None) or getattr(dev, "rssi", None)
            candidates.append((addr, name or addr, rssi))
            print(f"[Scan] Candidate: {name or '?'}  {addr}  RSSI={rssi}")

    if not candidates:
        print("[Scan] No matching device found.")
        return None

    candidates.sort(
        key=lambda x: x[2] if x[2] is not None else -999, reverse=True
    )
    mac, name, _ = candidates[0]
    print(f"[Scan] Selected: {name} @ {mac}")
    return mac, name


# ═══════════════════════ Format callbacks ═════════════════════════════
def _fmt_v(p, v):
    return f"{v:.2f}V" if v is not None else ""


def _fmt_v3(p, v):
    return f"{v:.3f}V" if v is not None else ""


def _fmt_a(p, v):
    return f"{v:.2f}A" if v is not None else ""


def _fmt_w(p, v):
    return f"{v:.0f}W" if v is not None else ""


def _fmt_ah(p, v):
    return f"{v:.1f}Ah" if v is not None else ""


def _fmt_pct(p, v):
    return f"{v:.0f}%" if v is not None else ""


def _fmt_c(p, v):
    return f"{v:.1f}°C" if v is not None else ""


def _fmt_s(p, v):
    if v is None or v < 0:
        return ""
    h, r = divmod(int(v), 3600)
    m, s = divmod(r, 60)
    return f"{h}h {m}m" if h else f"{m}m {s}s"


# ═══════════════════════ D-Bus bridge ═════════════════════════════════
class BatteryDbusBridge:
    def __init__(self, device_instance: int = DEVICE_INSTANCE):
        self.lock = threading.Lock()
        self.last_packet_time = 0.0
        self.cell_count = CELL_COUNT_DEFAULT
        self.n_temps = 0
        self.got_8d = False

        self.service = VeDbusService(
            f"com.victronenergy.battery.bt_bms_{device_instance}",
            register=False,
        )

        self.service.add_path("/Mgmt/ProcessName", __file__)
        self.service.add_path("/Mgmt/ProcessVersion", "7.4")
        self.service.add_path("/Mgmt/Connection", "Bluetooth BLE TDT (auto)")
        self.service.add_path("/DeviceInstance", device_instance)
        self.service.add_path("/ProductId", 0xBA77)
        self.service.add_path("/ProductName", PRODUCT_NAME)
        self.service.add_path("/CustomName", CUSTOM_NAME)
        self.service.add_path("/FirmwareVersion", "—")
        self.service.add_path("/HardwareVersion", "TDT BMS")
        self.service.add_path("/Connected", 0)
        self.service.add_path("/Serial", "")
        self.service.add_path("/Manufacturer", "Humsienk")
        self.service.add_path("/State", 9)  # Running (0 = Initializing in Lynx enum!)
        self.service.add_path("/Mode", 3)
        self.service.add_path("/ErrorCode", 0)
        self.service.add_path("/ConnectionInformation", "Initializing")
        self.service.add_path("/SystemSwitch", 1)

        self.service.add_path(
            "/Capacity", CAPACITY_AH_DEFAULT, gettextcallback=_fmt_ah
        )
        self.service.add_path(
            "/InstalledCapacity", CAPACITY_AH_DEFAULT, gettextcallback=_fmt_ah
        )
        self.service.add_path(
            "/ConsumedAmphours", 0.0, gettextcallback=_fmt_ah
        )

        self.service.add_path("/Dc/0/Voltage", 0.0, gettextcallback=_fmt_v)
        self.service.add_path("/Dc/0/Current", 0.0, gettextcallback=_fmt_a)
        self.service.add_path("/Dc/0/Power", 0.0, gettextcallback=_fmt_w)
        self.service.add_path(
            "/Dc/0/Temperature", 25.0, gettextcallback=_fmt_c
        )

        self.service.add_path("/Soc", 50.0, gettextcallback=_fmt_pct)
        self.service.add_path("/Soh", 100.0, gettextcallback=_fmt_pct)

        self.service.add_path(
            "/Info/BatteryLowVoltage",
            BATTERY_LOW_VOLTAGE,
            gettextcallback=_fmt_v,
        )
        self.service.add_path(
            "/Info/MaxChargeVoltage",
            MAX_CHARGE_VOLTAGE,
            gettextcallback=_fmt_v,
        )
        self.service.add_path(
            "/Info/MaxChargeCellVoltage",
            round(MAX_CHARGE_VOLTAGE / CELL_COUNT_DEFAULT, 3),
            gettextcallback=_fmt_v3,
        )
        self.service.add_path(
            "/Info/MaxChargeCurrent",
            MAX_CHARGE_CURRENT,
            gettextcallback=_fmt_a,
        )
        self.service.add_path(
            "/Info/MaxDischargeCurrent",
            MAX_DISCHARGE_CURRENT,
            gettextcallback=_fmt_a,
        )
        self.service.add_path("/Info/ChargeMode", None)
        self.service.add_path("/Info/ChargeLimitation", None)
        self.service.add_path("/Info/DischargeLimitation", None)

        self.service.add_path(
            "/System/NrOfCellsPerBattery", CELL_COUNT_DEFAULT
        )
        self.service.add_path("/System/NrOfModulesOnline", 1)
        self.service.add_path("/System/NrOfModulesOffline", 0)
        self.service.add_path("/System/NrOfModulesBlockingCharge", 0)
        self.service.add_path("/System/NrOfModulesBlockingDischarge", 0)
        self.service.add_path(
            "/System/MinCellVoltage", 0.0, gettextcallback=_fmt_v3
        )
        self.service.add_path(
            "/System/MaxCellVoltage", 0.0, gettextcallback=_fmt_v3
        )
        self.service.add_path("/System/MinVoltageCellId", "")
        self.service.add_path("/System/MaxVoltageCellId", "")
        self.service.add_path(
            "/System/MinCellTemperature", None, gettextcallback=_fmt_c
        )
        self.service.add_path(
            "/System/MaxCellTemperature", None, gettextcallback=_fmt_c
        )
        self.service.add_path(
            "/System/MOSTemperature", None, gettextcallback=_fmt_c
        )
        self.service.add_path(
            "/System/Temperature1", None, gettextcallback=_fmt_c
        )
        self.service.add_path("/System/Temperature1Name", "Ambient")
        self.service.add_path(
            "/System/Temperature2", None, gettextcallback=_fmt_c
        )
        self.service.add_path("/System/Temperature2Name", "MOSFET")

        self.service.add_path("/Io/AllowToCharge", 1)
        self.service.add_path("/Io/AllowToDischarge", 1)
        self.service.add_path("/Io/AllowToBalance", 1)
        self.service.add_path("/Balancing", 0)

        for a in (
            "LowVoltage",
            "HighVoltage",
            "LowCellVoltage",
            "HighCellVoltage",
            "LowSoc",
            "HighChargeCurrent",
            "HighDischargeCurrent",
            "CellImbalance",
            "InternalFailure",
            "HighChargeTemperature",
            "LowChargeTemperature",
            "HighTemperature",
            "LowTemperature",
            "BmsCable",
        ):
            self.service.add_path(f"/Alarms/{a}", 0)

        for i in range(1, MAX_CELLS + 1):
            self.service.add_path(
                f"/Voltages/Cell{i}", 0.0, gettextcallback=_fmt_v3
            )
        self.service.add_path("/Voltages/Sum", 0.0, gettextcallback=_fmt_v)
        self.service.add_path("/Voltages/Diff", 0.0, gettextcallback=_fmt_v3)

        self.service.add_path("/TimeToGo", None, gettextcallback=_fmt_s)
        self.service.add_path("/History/ChargeCycles", None)

        self.service.register()

        self.latest_voltages: List[float] = []
        self.latest_temps: List[float] = []
        self.latest_current = 0.0
        self.latest_soc = 50.0
        self.latest_soh = 100.0
        self.latest_temp = 25.0
        self.latest_capacity_remain = CAPACITY_AH_DEFAULT
        self.installed_capacity = CAPACITY_AH_DEFAULT
        self.latest_cycles: Optional[int] = None
        self.latest_pack_voltage: Optional[float] = None
        self.latest_problem_code = 0
        self.charge_fet = True
        self.discharge_fet = True

    def set_status(self, connected: int, info: str) -> None:
        self.service["/Connected"] = 1 if connected else 0
        self.service["/ConnectionInformation"] = info
        if connected:
            self.service["/Alarms/BmsCable"] = 0
            self.service["/System/NrOfModulesOnline"] = 1
            self.service["/System/NrOfModulesOffline"] = 0
        else:
            self.service["/Alarms/BmsCable"] = 2
            self.service["/System/NrOfModulesOnline"] = 0
            self.service["/System/NrOfModulesOffline"] = 1
        print(f"[Status] Connected={int(bool(connected))} Info={info!r}")

    def set_connected(self, status: int) -> None:
        self.set_status(1, "Connected") if status else self.set_status(
            0, "Disconnected"
        )

    def commit_values(self) -> bool:
        with self.lock:
            self.last_packet_time = time.time()
            self.service["/Connected"] = 1
            self.service["/ConnectionInformation"] = "Connected"
            self.service["/Alarms/BmsCable"] = 0
            self.service["/SystemSwitch"] = 1
            self.service["/System/NrOfModulesOnline"] = 1
            self.service["/System/NrOfModulesOffline"] = 0

            voltages = list(self.latest_voltages)
            current = self.latest_current
            soc = self.latest_soc
            temp = self.latest_temp
            temps = list(self.latest_temps)
            problem = self.latest_problem_code

            total_v = 0.0
            if voltages and len(voltages) >= 4:
                min_v = min(voltages)
                max_v = max(voltages)
                sum_v = sum(voltages)
                total_v = (
                    self.latest_pack_voltage
                    if self.latest_pack_voltage and self.latest_pack_voltage > 1
                    else sum_v
                )
                delta_v = max_v - min_v
                min_idx = voltages.index(min_v) + 1
                max_idx = voltages.index(max_v) + 1
                n = len(voltages)
                self.cell_count = n

                self.service["/System/NrOfCellsPerBattery"] = n
                self.service["/System/MinCellVoltage"] = round(min_v, 3)
                self.service["/System/MaxCellVoltage"] = round(max_v, 3)
                self.service["/System/MinVoltageCellId"] = f"C{min_idx}"
                self.service["/System/MaxVoltageCellId"] = f"C{max_idx}"

                for i in range(MAX_CELLS):
                    self.service[f"/Voltages/Cell{i + 1}"] = (
                        round(voltages[i], 3) if i < n else 0.0
                    )

                self.service["/Voltages/Sum"] = round(sum_v, 3)
                self.service["/Voltages/Diff"] = round(delta_v, 3)
                self.service["/Dc/0/Voltage"] = round(total_v, 3)

                self.service["/Alarms/CellImbalance"] = (
                    2 if delta_v > CELL_IMBALANCE_V else 0
                )
                self.service["/Alarms/HighCellVoltage"] = (
                    2 if max_v > CELL_HIGH_V else 0
                )
                self.service["/Alarms/LowCellVoltage"] = (
                    2 if min_v < CELL_LOW_V else 0
                )
                self.service["/Alarms/HighVoltage"] = (
                    2 if total_v > MAX_CHARGE_VOLTAGE + 0.5 else 0
                )
                self.service["/Alarms/LowVoltage"] = (
                    2 if total_v < BATTERY_LOW_VOLTAGE else 0
                )
            else:
                total_v = float(self.service["/Dc/0/Voltage"] or 0.0)

            self.service["/Dc/0/Current"] = round(current, 2)
            self.service["/Dc/0/Power"] = round(total_v * current, 1)
            self.service["/Soc"] = round(soc, 1)
            self.service["/Soh"] = round(self.latest_soh, 1)
            self.service["/Dc/0/Temperature"] = round(temp, 1)

            all_t = temps if temps else ([temp] if temp is not None else [])
            if all_t:
                tmin, tmax = min(all_t), max(all_t)
                self.service["/System/MinCellTemperature"] = round(tmin, 1)
                self.service["/System/MaxCellTemperature"] = round(tmax, 1)
                self.service["/Alarms/HighTemperature"] = (
                    2 if tmax > TEMP_HIGH_C else 0
                )
                self.service["/Alarms/LowTemperature"] = (
                    2 if tmin < TEMP_LOW_C else 0
                )
                self.service["/Alarms/HighChargeTemperature"] = (
                    2 if tmax > TEMP_HIGH_C else 0
                )
                self.service["/Alarms/LowChargeTemperature"] = (
                    2 if tmin < 0 else 0
                )

            if temps:
                self.service["/System/Temperature1"] = round(temps[0], 1)
                if len(temps) >= 2:
                    self.service["/System/MOSTemperature"] = round(temps[1], 1)
                    self.service["/System/Temperature2"] = round(temps[1], 1)

            remain = self.latest_capacity_remain
            full = self.installed_capacity
            self.service["/Capacity"] = round(remain, 1)
            self.service["/InstalledCapacity"] = round(full, 1)
            self.service["/ConsumedAmphours"] = round(
                max(0.0, full - remain), 1
            )

            self.service["/Alarms/LowSoc"] = 2 if soc < SOC_LOW_PCT else 0

            if self.latest_cycles is not None:
                self.service["/History/ChargeCycles"] = self.latest_cycles

            # problem_code bits are undocumented → any non-zero = InternalFailure
            self.service["/Alarms/InternalFailure"] = 2 if problem else 0
            self.service["/ErrorCode"] = problem

            # Victron battery /State (Lynx enum from dbus_modbustcp):
            # 0-8 = Initializing…, 9 = Running, 10 = Error,
            # 12 = Shutdown, 14 = Standby
            # Do NOT use 0/1/2 for idle/charge/discharge – 0 means "Initializing"!
            if problem or not self.charge_fet and not self.discharge_fet:
                self.service["/State"] = 10  # Error
            elif abs(current) < 0.3 and soc >= 99:
                self.service["/State"] = 14  # Standby (voll, kein Strom)
            else:
                self.service["/State"] = 9   # Running

            if current < -0.2 and remain > 0 and full > 0:
                usable = max(0.0, remain - full * 0.10)
                ttg = int(usable / abs(current) * 3600)
                self.service["/TimeToGo"] = ttg if ttg > 0 else 0
            else:
                self.service["/TimeToGo"] = None

            high_c = int(self.service["/Alarms/HighCellVoltage"] or 0)
            low_c = int(self.service["/Alarms/LowCellVoltage"] or 0)
            high_t = int(self.service["/Alarms/HighTemperature"] or 0)
            allow_c = (
                1 if self.charge_fet and not high_c and not high_t else 0
            )
            allow_d = 1 if self.discharge_fet and not low_c else 0
            self.service["/Io/AllowToCharge"] = allow_c
            self.service["/Io/AllowToDischarge"] = allow_d
            self.service["/System/NrOfModulesBlockingCharge"] = (
                0 if allow_c else 1
            )
            self.service["/System/NrOfModulesBlockingDischarge"] = (
                0 if allow_d else 1
            )

            if current > MAX_CHARGE_CURRENT * 1.05:
                self.service["/Alarms/HighChargeCurrent"] = 2
            else:
                self.service["/Alarms/HighChargeCurrent"] = 0
            if current < -MAX_DISCHARGE_CURRENT * 1.05:
                self.service["/Alarms/HighDischargeCurrent"] = 2
            else:
                self.service["/Alarms/HighDischargeCurrent"] = 0

            if voltages and (max(voltages) - min(voltages)) > 0.050:
                self.service["/Info/MaxChargeCurrent"] = max(
                    20.0, MAX_CHARGE_CURRENT * 0.5
                )
                self.service["/Info/ChargeLimitation"] = "Cell imbalance"
            else:
                self.service["/Info/MaxChargeCurrent"] = MAX_CHARGE_CURRENT
                self.service["/Info/ChargeLimitation"] = None

            if not allow_d:
                self.service["/Info/DischargeLimitation"] = "FET/Alarm"
            else:
                self.service["/Info/DischargeLimitation"] = None

        print(
            f"[D-Bus] U={total_v:.2f}V I={current:+.1f}A SoC={soc:.0f}% "
            f"T={temp:.1f}°C cells={len(voltages)} "
            f"remain={self.latest_capacity_remain:.1f}Ah "
            f"FET C={int(self.charge_fet)}/D={int(self.discharge_fet)} "
            f"prob=0x{problem:04X} 8D={int(self.got_8d)}"
        )
        return False


# ═══════════════════════ Parser ═══════════════════════════════════════
def parse_tdt_frame(data: bytes, bridge: BatteryDbusBridge) -> None:
    if not frame_ok(data):
        return
    sub = data[5]
    if sub == 0x8C:
        _parse_8c(data, bridge)
    elif sub == 0x8D:
        _parse_8d(data, bridge)
    elif sub == 0x92:
        _parse_92(data, bridge)


def _parse_8c(data: bytes, bridge: BatteryDbusBridge) -> None:
    """aiobmsble tdt_bms._async_update – 0x8C-Layout."""
    try:
        if len(data) < CELL_POS + 3:
            return
        n_cells = data[CELL_POS]
        if not (4 <= n_cells <= MAX_CELLS):
            return

        voltages: List[float] = []
        off = CELL_POS + 1
        for _ in range(n_cells):
            if off + 2 > len(data) - 3:
                break
            voltages.append(
                int.from_bytes(data[off : off + 2], "big") / 1000.0
            )
            off += 2
        if len(voltages) != n_cells:
            print(f"[Parse 8C] cells incomplete {len(voltages)}/{n_cells}")
            return

        if off >= len(data) - 3:
            return
        n_temps = data[off]
        off += 1
        temps: List[float] = []
        for _ in range(min(n_temps, 8)):
            if off + 2 > len(data) - 3:
                break
            raw = int.from_bytes(data[off : off + 2], "big")
            temps.append((raw - 2731) / 10.0)
            off += 2

        idx = n_cells + n_temps
        start = CELL_POS + idx * 2 + 2
        if start + 14 > len(data) - 3:
            print(f"[Parse 8C] frame too short (start={start})")
            return

        raw_i = int.from_bytes(data[start : start + 2], "big")
        current = (raw_i & 0x3FFF) / 10.0 * (
            -1.0 if (raw_i >> 15) else 1.0
        )
        pack_v = int.from_bytes(data[start + 2 : start + 4], "big") / 100.0
        remain = int.from_bytes(data[start + 4 : start + 6], "big") / 10.0
        cycles = int.from_bytes(data[start + 8 : start + 10], "big")
        soc = float(int.from_bytes(data[start + 12 : start + 14], "big"))

        design = CAPACITY_AH_DEFAULT
        if 5 < soc <= 100 and remain > 1:
            est = remain / (soc / 100.0)
            if 50 < est < 2000:
                design = est
        # keep configured default if estimate is implausible
        if abs(design - CAPACITY_AH_DEFAULT) > CAPACITY_AH_DEFAULT * 0.4:
            design = CAPACITY_AH_DEFAULT

        soh = 100.0
        if start + 16 <= len(data) - 3:
            maybe = int.from_bytes(data[start + 14 : start + 16], "big")
            if 1 <= maybe <= 100:
                soh = float(maybe)

        with bridge.lock:
            bridge.latest_voltages = voltages
            bridge.latest_temps = temps
            bridge.n_temps = n_temps
            bridge.cell_count = n_cells
            bridge.latest_temp = temps[0] if temps else bridge.latest_temp
            bridge.latest_current = current
            bridge.latest_pack_voltage = pack_v if pack_v > 1 else None
            if 0 <= soc <= 100:
                bridge.latest_soc = soc
            bridge.latest_soh = soh
            bridge.latest_capacity_remain = remain
            bridge.installed_capacity = design
            bridge.latest_cycles = cycles

        GLib.idle_add(bridge.commit_values)
    except Exception as e:
        print(f"[Parse 8C] {e}")


def _parse_8d(data: bytes, bridge: BatteryDbusBridge) -> None:
    """Optional – aiobmsble offsets; some devices never send 0x8D."""
    try:
        with bridge.lock:
            n_cells = bridge.cell_count
            n_temps = bridge.n_temps
        if n_cells < 4:
            return
        idx = n_cells + n_temps
        p0 = CELL_POS + idx + 6
        if p0 + 3 > len(data) - 3:
            return
        problem = int.from_bytes(data[p0 : p0 + 2], "big")
        mosfets = data[p0 + 2]
        charge_fet = bool(mosfets & 0x04)
        discharge_fet = bool(mosfets & 0x02)

        with bridge.lock:
            bridge.latest_problem_code = problem
            bridge.charge_fet = charge_fet
            bridge.discharge_fet = discharge_fet
            bridge.got_8d = True

        if problem:
            print(
                f"[8D] problem=0x{problem:04X} MOS=0x{mosfets:02X} "
                f"C={charge_fet} D={discharge_fet}"
            )
    except Exception as e:
        print(f"[Parse 8D] {e}")


def _parse_92(data: bytes, bridge: BatteryDbusBridge) -> None:
    try:

        def b2str(b: bytes) -> str:
            return "".join(
                chr(c) if 32 <= c < 127 else "" for c in b
            ).strip()

        if len(data) < 70:
            return
        sw = b2str(data[8:28])
        mfg = b2str(data[28:48])
        sn = b2str(data[48:68])
        print(f"[Device] SW={sw!r} Mfg={mfg!r} SN={sn!r}")

        if sw:
            bridge.service["/FirmwareVersion"] = sw
            bridge.service["/HardwareVersion"] = (
                sw.split("_")[0] if "_" in sw else sw
            )
        else:
            bridge.service["/HardwareVersion"] = "TDT BMS"

        bridge.service["/Manufacturer"] = mfg if mfg else "Humsienk"
        if sn:
            bridge.service["/Serial"] = sn
    except Exception as e:
        print(f"[Parse 92] {e}")


# ═══════════════════════ BLE ══════════════════════════════════════════
class FrameAssembler:
    def __init__(self):
        self.buf = bytearray()
        self.exp_len = 0

    def feed(self, data: bytes) -> Optional[bytes]:
        if (
            len(data) >= 10
            and data[0] == 0x7E
            and (not self.buf or len(self.buf) >= self.exp_len)
        ):
            self.exp_len = 10 + int.from_bytes(data[6:8], "big")
            self.buf = bytearray(data)
        else:
            self.buf.extend(data)

        if (
            len(self.buf) >= max(10, self.exp_len)
            and self.buf[-1:] == b"\x0d"
        ):
            frame = bytes(self.buf)
            self.buf.clear()
            self.exp_len = 0
            return frame
        if len(self.buf) > 512:
            self.buf.clear()
            self.exp_len = 0
        return None



def _run_cmd(cmd: list, timeout: float = 8.0) -> str:
    try:
        r = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout
        )
        out = (r.stdout or "") + (r.stderr or "")
        return out.strip()
    except Exception as e:
        return f"(cmd failed: {e})"


def ble_cleanup(mac: str) -> None:
    """Drop stale BlueZ links – common reason only a reboot seemed to help."""
    mac_u = mac.upper()
    print(f"[BLE-Cleanup] disconnect {mac_u} …")
    _run_cmd(["bluetoothctl", "disconnect", mac_u], timeout=5)
    time.sleep(0.4)
    # device kurz entfernen und neu scannen lassen (optional, harmlos wenn unbekannt)
    _run_cmd(["bluetoothctl", "remove", mac_u], timeout=5)
    time.sleep(0.3)


def ble_adapter_reset() -> None:
    """Harder recovery: cycle the HCI adapter after repeated failures."""
    print("[BLE-Cleanup] Adapter reset hci0 …")
    for c in (
        ["hciconfig", "hci0", "down"],
        ["hciconfig", "hci0", "up"],
        ["bluetoothctl", "power", "on"],
    ):
        print(f"  {' '.join(c)} -> {_run_cmd(c, timeout=6)[:80]}")
        time.sleep(0.6)
    time.sleep(1.5)


async def resolve_mac() -> str:
    if MAC_ADDRESS and len(MAC_ADDRESS) >= 12:
        print(f"[Config] Fixed MAC: {MAC_ADDRESS}")
        return MAC_ADDRESS
    while not shutdown_event.is_set():
        result = await discover_bms_mac()
        if result:
            return result[0]
        print(f"[Scan] Retry in {RESCAN_INTERVAL_S}s …")
        for _ in range(RESCAN_INTERVAL_S):
            if shutdown_event.is_set():
                break
            await asyncio.sleep(1.0)
    return ""


async def bleak_worker(bridge: BatteryDbusBridge) -> None:
    assembler = FrameAssembler()
    cmd_head = 0x7E
    fail_count = 0

    def on_notify(_sender, data: bytearray) -> None:
        frame = assembler.feed(bytes(data))
        if frame:
            parse_tdt_frame(frame, bridge)

    print("Bluetooth worker started …")
    GLib.idle_add(bridge.set_status, 0, "Scanning")
    mac = await resolve_mac()
    if not mac:
        print("[Error] No MAC – stopping worker.")
        return

    bridge.service["/Serial"] = mac.replace(":", "")
    bridge.service["/Mgmt/Connection"] = f"Bluetooth BLE {mac}"

    # Clean up once at start (prevents reboot-only recovery)
    await asyncio.to_thread(ble_cleanup, mac)

    while not shutdown_event.is_set():
        try:
            GLib.idle_add(bridge.set_status, 0, "Connecting")
            # generous timeout; BlueZ on Venus can be slow
            async with BleakClient(mac, timeout=30.0) as client:
                fail_count = 0
                print(f"BLE connected ({mac}) – auth + init …")
                GLib.idle_add(bridge.set_status, 0, "Connecting")

                await client.start_notify(UUID_NOTIFY, on_notify)
                await asyncio.sleep(0.3)

                GLib.idle_add(bridge.set_status, 0, "Authenticating")
                try:
                    await client.write_gatt_char(
                        UUID_AUTH, b"HiLink", response=True
                    )
                    await asyncio.sleep(0.15)
                    try:
                        ret = await client.read_gatt_char(UUID_AUTH)
                        if ret and ret[0] != 0x01:
                            print(f"[Auth] Unlock: 0x{ret[0]:02X}")
                        else:
                            print("[Auth] HiLink OK")
                    except Exception:
                        print("[Auth] HiLink written (no readback)")
                except Exception as e:
                    print(f"[Auth] {e} – continuing")
                await asyncio.sleep(0.4)

                GLib.idle_add(bridge.set_status, 0, "Initializing")
                # Command head 0x7E, fallback 0x1E (aiobmsble)
                for head in CMD_HEADS:
                    cmd_head = head
                    for cmd in build_init_commands(head):
                        await client.write_gatt_char(
                            UUID_WRITE, cmd, response=False
                        )
                        await asyncio.sleep(0.4)
                    break  # erster Head reicht; bei Timeout im Poll wechseln

                GLib.idle_add(bridge.set_status, 1, "Connected")
                print(
                    f"Polling active (every {POLL_INTERVAL_S}s, head=0x{cmd_head:02X}) …"
                )

                no_data_cycles = 0
                while client.is_connected and not shutdown_event.is_set():
                    for cmd in build_poll_commands(cmd_head):
                        if shutdown_event.is_set():
                            break
                        await client.write_gatt_char(
                            UUID_WRITE, cmd, response=False
                        )
                        await asyncio.sleep(0.35)

                    if (
                        bridge.last_packet_time
                        and (time.time() - bridge.last_packet_time) < 5
                    ):
                        no_data_cycles = 0
                    else:
                        no_data_cycles += 1
                        # nach 3 Zyklen ohne Daten: alternativen Head versuchen
                        if no_data_cycles == 3 and cmd_head == 0x7E:
                            cmd_head = 0x1E
                            print("[Poll] Switching command head → 0x1E")
                        elif no_data_cycles == 6 and cmd_head == 0x1E:
                            cmd_head = 0x7E
                            no_data_cycles = 0
                            print("[Poll] Switching command head → 0x7E")

                    for _ in range(POLL_INTERVAL_S):
                        if shutdown_event.is_set():
                            break
                        await asyncio.sleep(1.0)

                if client.is_connected:
                    await client.disconnect()
                print("BLE disconnected.")

        except Exception as e:
            if not shutdown_event.is_set():
                fail_count += 1
                err = str(e).strip() or type(e).__name__
                print(f"BLE error [{fail_count}]: {type(e).__name__}: {err}")
                GLib.idle_add(bridge.set_connected, 0)

                # Clear stale connection
                await asyncio.to_thread(ble_cleanup, mac)

                # Reset adapter after every 3 failures
                if fail_count >= 3 and fail_count % 3 == 0:
                    await asyncio.to_thread(ble_adapter_reset)
                    # rescan after adapter reset
                    if not MAC_ADDRESS:
                        found = await discover_bms_mac()
                        if found:
                            mac = found[0]
                            bridge.service["/Serial"] = mac.replace(":", "")
                            bridge.service["/Mgmt/Connection"] = (
                                f"Bluetooth BLE {mac}"
                            )
                            await asyncio.to_thread(ble_cleanup, mac)

                wait_s = min(15 + fail_count * 5, 60)
                print(f"Retry in {wait_s}s …")
                for _ in range(wait_s):
                    if shutdown_event.is_set():
                        break
                    await asyncio.sleep(1.0)


def stale_watchdog(bridge: BatteryDbusBridge) -> bool:
    if (
        bridge.last_packet_time
        and (time.time() - bridge.last_packet_time) > STALE_TIMEOUT_S
    ):
        if bridge.service["/Connected"] == 1:
            print("[Watchdog] Timeout – Connected=0")
            bridge.set_connected(0)
    return True


def main() -> None:
    DBusGMainLoop(set_as_default=True)
    print("Venus OS Humsienk/TDT BMS Bridge v7.4")
    bridge = BatteryDbusBridge(DEVICE_INSTANCE)

    t = threading.Thread(
        target=lambda: asyncio.run(bleak_worker(bridge)), daemon=True
    )
    t.start()
    GLib.timeout_add_seconds(5, stale_watchdog, bridge)

    loop = GLib.MainLoop()
    try:
        loop.run()
    except KeyboardInterrupt:
        print("\nShutdown …")
        shutdown_event.set()
        time.sleep(1.5)
        print("Stopped.")


if __name__ == "__main__":
    main()

# ---------------------------------------------------------------------------
# Install: see README.md  |  Boot: /data/rc.local  |  Stop: pkill -f venus_bms
# Optional fixed MAC: set MAC_ADDRESS below in the configuration section.
# ---------------------------------------------------------------------------
