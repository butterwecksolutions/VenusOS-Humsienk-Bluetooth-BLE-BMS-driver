# Venus OS BLE BMS Bridge – Humsienk / TDT (HiLink)
<img width="40%" alt="grafik" src="https://github.com/user-attachments/assets/2deb593c-4ea6-4469-815f-605fff1f472b" />
<img width="40%" alt="grafik" src="https://github.com/user-attachments/assets/144319b7-ecf6-4160-9030-997a71c3584a" />

Bluetooth LE bridge that exposes a **Humsienk** (or compatible **TDT / HiLink**) battery as a native Venus OS battery service:

```text
com.victronenergy.battery.bt_bms_512
```

Tested with packs that advertise names such as `HS…` and speak the TDT framed protocol (`0x7E … 0x0D`) over GATT characteristics `fff1` / `fff2` / `fffa`.

---

## Features

- Auto-discovery of the BMS MAC (name prefixes `HS`, `ECO`, `DCH`, `TDT` or manufacturer ID `54976`)
- Optional fixed `MAC_ADDRESS` if auto-scan is not desired
- HiLink unlock (`fffa`), live telemetry `0x8C`, status `0x8D` (optional), device info `0x92`
- CRC-16/Modbus frame checks
- Full cell voltages, temperatures, SoC/SoH, current, power, capacity
- DVCC-oriented paths: `/Info/MaxCharge*`, `/Io/AllowToCharge|Discharge`, alarms
- Correct Victron `/State` values (`9` = Running — **not** `0`, which means Initializing)
- Stale BlueZ cleanup + HCI reset after repeated connect failures
- Auto-install of `bleak` via pip if missing

**Not supported:** multi-pack aggregation (one bridge process = one battery).

---

## Requirements

- Venus OS (Cerbo, Venus GX, Raspberry Pi image, etc.) with Bluetooth
- Python 3 with `gi` / D-Bus (stock on Venus)
- Network once for `pip install bleak` (or install offline)

---

## Installation (reboot-safe)

`/data` survives Venus firmware updates. Prefer **`/data/rc.local`** over daemontools `/service` links.

### 1. Copy the script

```sh
cp venus_bms_bridge.py /data/etc/venus_bms_bridge.py
chmod +x /data/etc/venus_bms_bridge.py
mkdir -p /data/log
```

### 2. Enable start on boot

```sh
touch /data/rc.local
chmod +x /data/rc.local
```

Edit `/data/rc.local` so it contains at least:

```sh
#!/bin/sh
# Wait for Bluetooth and D-Bus after boot
sleep 20
/data/etc/venus_bms_bridge.py >> /data/log/venus_bms_bridge.log 2>&1 &
exit 0
```

If `rc.local` already exists, add the `sleep`/`venus_bms_bridge` lines **before** `exit 0`.

### 3. Start without rebooting

```sh
/data/etc/venus_bms_bridge.py >> /data/log/venus_bms_bridge.log 2>&1 &
```

### 4. Logs and stop

```sh
tail -F /data/log/venus_bms_bridge.log
pkill -f venus_bms_bridge.py
```

---

## Configuration

Edit the constants at the top of `venus_bms_bridge.py`:

| Variable | Default | Meaning |
|----------|---------|---------|
| `MAC_ADDRESS` | `""` | Empty = scan; or set e.g. `"C0:D6:3C:5B:A2:66"` |
| `CAPACITY_AH_DEFAULT` | `314.0` | Fallback pack capacity (Ah) |
| `MAX_CHARGE_VOLTAGE` | `58.4` | DVCC charge voltage limit (V) |
| `BATTERY_LOW_VOLTAGE` | `44.8` | Low voltage threshold (V) |
| `MAX_CHARGE_CURRENT` | `100.0` | DVCC charge current limit (A) |
| `MAX_DISCHARGE_CURRENT` | `100.0` | DVCC discharge current limit (A) |
| `DEVICE_INSTANCE` | `512` | Venus device instance |
| `CUSTOM_NAME` | `"48V314AH"` | Name shown in the GUI |
| `POLL_INTERVAL_S` | `2` | Seconds between poll cycles |

Cell count and remaining capacity are taken from the BMS when available.

---

## Verify on the GX

```sh
dbus -y com.victronenergy.battery.bt_bms_512 /Connected GetValue
dbus -y com.victronenergy.battery.bt_bms_512 /State GetValue
dbus -y com.victronenergy.battery.bt_bms_512 /Soc GetValue
dbus -y com.victronenergy.battery.bt_bms_512 /Dc/0/Current GetValue
```

Expected when online:

- `/Connected` = `1`
- `/State` = `9` (Running) or `14` (Standby)
- `/Soc`, voltage, current updating every few seconds

In Remote Console the battery should appear under device list (e.g. custom name `48V314AH`).

---

## Safety notes

- The **hardware BMS** remains the primary protection layer.
- This driver supplies monitoring and DVCC *limits* to Venus; it is not a certified Victron BMS product.
- Verify current sign/scale against a shunt or the vendor app once.
- TDT `problem_code` bit definitions are not published; any non-zero value raises `/Alarms/InternalFailure`.

---

## References

- [aiobmsble `tdt_bms.py`](https://github.com/patman15/aiobmsble)
- [dbus-serialbattery](https://github.com/mr-manuel/venus-os_dbus-serialbattery)
- [Victron dbus wiki](https://github.com/victronenergy/venus/wiki/dbus)
- Victron `dbus_modbustcp` `attributes.csv` (`/State` Lynx enum)

---

## License

Use and modify at your own risk. Not affiliated with Victron Energy or Humsienk.
