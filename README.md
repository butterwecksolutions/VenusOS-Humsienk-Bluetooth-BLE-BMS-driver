# Venus OS BLE BMS Bridge – Humsienk / TDT (HiLink)

Python driver that connects a **Humsienk** (or compatible **TDT / HiLink**) LiFePO4 battery over **Bluetooth LE** and publishes a full `com.victronenergy.battery.*` service on **Venus OS** (Cerbo GX, Raspberry Pi, etc.).

The battery appears in the Remote Console like a native Victron battery monitor, including cell voltages, DVCC limits, alarms, SoC, and Time-to-Go.

---

## Features

| Feature | Details |
|--------|---------|
| **Auto discovery** | Finds the pack by BLE name (`HS*`, `ECO*`, `DCH*`, `TDT*`) or manufacturer ID `54976` – no MAC required |
| **HiLink auth** | Writes `HiLink` to the auth characteristic (same as the vendor app) |
| **TDT protocol** | Frame format `7E … CRC 0D`, commands `0x8C` / `0x8D` / `0x92` (aligned with [aiobmsble](https://github.com/patman15/aiobmsble) `tdt_bms.py`) |
| **Venus integration** | Voltage, current, power, SoC, SoH, capacity, all cell voltages, temps, TimeToGo, SystemSwitch |
| **DVCC** | `/Info/MaxChargeVoltage`, `/Info/MaxChargeCurrent`, `/Info/MaxDischargeCurrent`, `/Io/AllowToCharge`, `/Io/AllowToDischarge` |
| **Alarms** | Cell high/low, pack high/low, imbalance, SoC, temperature, over-current, BMS cable, internal failure |
| **Resilience** | Stale BlueZ cleanup, HCI adapter reset after repeated failures, command-head fallback `0x7E` / `0x1E` |
| **Dependencies** | Installs `bleak` via `pip` automatically if missing |

**Not supported (by design):** multiple packs on one process instance.

---

## Requirements

- Venus OS (official image or Large) with Bluetooth
- Python 3 with GLib / D-Bus (`vedbus` from Venus)
- A Humsienk / TDT HiLink BLE BMS (service UUID `fff0`)

Default pack profile in the script (adjust constants as needed):

- 16 cells, about 48 V, 314 Ah
- Charge limit 58.4 V / 100 A, discharge 100 A, low voltage 44.8 V

---

## Quick install

```bash
cp venus_bms_bridge.py /data/etc/venus_bms_bridge.py
chmod +x /data/etc/venus_bms_bridge.py
```

Run once in the foreground to verify:

```bash
/data/etc/venus_bms_bridge.py
```

You should see scan, connect, auth, polling, and the battery in the device list (e.g. `48V314AH`).

### Optional: fixed MAC

If auto-discovery is unreliable, set at the top of the script:

```python
MAC_ADDRESS = "C0:D6:3C:5B:A2:66"
```

---

## Run as a service (daemontools)

Survives reboots. Files under `/data` survive Venus firmware updates.

```bash
mkdir -p /data/etc/venus_bms_bridge/service/log

cat > /data/etc/venus_bms_bridge/service/run << 'EOF'
#!/bin/sh
exec 2>&1
exec /data/etc/venus_bms_bridge.py
EOF

cat > /data/etc/venus_bms_bridge/service/log/run << 'EOF'
#!/bin/sh
exec 2>&1
exec multilog t s25000 n4 /var/log/venus_bms_bridge
EOF

chmod +x /data/etc/venus_bms_bridge.py \
         /data/etc/venus_bms_bridge/service/run \
         /data/etc/venus_bms_bridge/service/log/run

ln -sf /data/etc/venus_bms_bridge/service /service/venus_bms_bridge
```

| Action | Command |
|--------|---------|
| Logs | `tail -F /var/log/venus_bms_bridge/current` |
| Stop | `svc -d /service/venus_bms_bridge` |
| Start | `svc -u /service/venus_bms_bridge` |
| Restart | `svc -t /service/venus_bms_bridge` |

---

## Configuration

Near the top of `venus_bms_bridge.py`:

| Constant | Default | Meaning |
|----------|---------|---------|
| `MAC_ADDRESS` | `""` | Empty = auto-scan |
| `NAME_PREFIXES` | `HS`, `ECO`, `DCH`, `TDT` | BLE name prefixes |
| `CAPACITY_AH_DEFAULT` | `314.0` | Nominal capacity |
| `MAX_CHARGE_VOLTAGE` | `58.4` | DVCC charge voltage limit |
| `BATTERY_LOW_VOLTAGE` | `44.8` | Low voltage / DVCC floor |
| `MAX_CHARGE_CURRENT` | `100.0` | DVCC charge current limit |
| `MAX_DISCHARGE_CURRENT` | `100.0` | DVCC discharge current limit |
| `DEVICE_INSTANCE` | `512` | D-Bus instance (`bt_bms_512`) |
| `POLL_INTERVAL_S` | `2` | Seconds between poll cycles |
| `CELL_HIGH_V` / `CELL_LOW_V` | `3.65` / `2.80` | Per-cell alarm thresholds |

---

## D-Bus paths (selection)

| Path | Meaning |
|------|---------|
| `/Soc`, `/Soh` | State of charge / health |
| `/Dc/0/Voltage`, `/Current`, `/Power`, `/Temperature` | Pack DC values |
| `/Voltages/Cell1` … `/CellN`, `/Sum`, `/Diff` | Cell data |
| `/Capacity`, `/InstalledCapacity`, `/ConsumedAmphours` | Ah |
| `/TimeToGo` | Seconds remaining while discharging |
| `/State` | Lynx enum: **9 = Running**, 10 = Error, 14 = Standby (0–8 = Initializing) |
| `/SystemSwitch` | 1 = On |
| `/Info/MaxChargeVoltage`, `/MaxChargeCurrent`, `/MaxDischargeCurrent` | DVCC |
| `/Io/AllowToCharge`, `/AllowToDischarge` | Policy for the GX |
| `/Alarms/*` | 0 = OK, 2 = Alarm |

Check from SSH:

```bash
dbus -y com.victronenergy.battery.bt_bms_512 /Connected GetValue
dbus -y com.victronenergy.battery.bt_bms_512 /State GetValue
dbus -y com.victronenergy.battery.bt_bms_512 /Soc GetValue
dbus -y com.victronenergy.battery.bt_bms_512 /Dc/0/Current GetValue
```

---

## Protocol notes

- **GATT:** notify `fff1`, write `fff2`, auth `fffa`
- **Auth:** write ASCII `HiLink`
- **0x8C:** live data (cells, temps, current, pack V, remain Ah, cycles, SoC)
- **0x8D:** optional status (MOSFET bits, `problem_code`); some firmwares never answer
- **0x92:** software / manufacturer / serial strings
- Current formula follows aiobmsble: `(raw & 0x3FFF) / 10`, sign from bit 15. **Verify once** against a shunt; invert in `_parse_8c` if needed.

Safety-critical protection (cell UVP/OVP, over-current, FET cut-off) lives **inside the BMS hardware**. This driver is a monitoring and DVCC limit source, not a replacement for the BMS.

---

## Troubleshooting

| Symptom | What to try |
|---------|-------------|
| `State` stuck on **Initializing** | `/State` must be **9** (Running), not 0 – fixed in v7.3+ |
| Connect fails until reboot | v7.4 runs `bluetoothctl disconnect/remove` and periodic HCI reset |
| No units (V/A) in GUI | Use latest script with `gettextcallback` on numeric paths |
| Wrong current sign | Flip sign in `_parse_8c` after comparing to a shunt |
| `bleak` missing after OS update | Script reinstalls on start, or run `pip3 install bleak` |

---

## Credits and references

- [patman15/aiobmsble](https://github.com/patman15/aiobmsble) – TDT BLE implementation
- [mr-manuel/venus-os_dbus-serialbattery](https://github.com/mr-manuel/venus-os_dbus-serialbattery) – Venus battery path conventions
- [Victron dbus wiki](https://github.com/victronenergy/venus/wiki/dbus) / [dbus_modbustcp](https://github.com/victronenergy/dbus_modbustcp) – `/State` Lynx enum

---

## License

Use and adapt freely for personal and community Venus OS projects.

**No warranty.** Battery systems can cause fire or equipment damage. Use at your own risk.
