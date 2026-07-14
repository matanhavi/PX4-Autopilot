#!/usr/bin/env python3
"""pxusb — one-command USB/IP helper for PX4 hardware debug on WSL.

Detects the PX4-relevant USB devices (FTDI/CP210x console adapters, ST-Link
JTAG/SWD probes, and PX4 board VCPs / serial bootloaders) *by identity*, then
binds + attaches them from the Windows host into WSL — so you never have to
chase reshuffling busids or hand-run `usbipd bind/attach` per device.

Why this exists (the pain it removes):
  * `usbipd list` shows devices by busid, and **busids reshuffle on every
    replug/reboot** — you must attach by VID:PID identity, not a remembered
    number.
  * Binding needs an **elevated** (admin) `usbipd bind`; this does it ONCE for
    all needed devices in a single UAC prompt instead of one prompt per device.
  * After attach you still have to map each device to its `/dev` node — this
    prints that mapping for you.

Usage (run from inside WSL; it shells out to `powershell.exe`):
  pxusb.py                 # or `status` — show recognized PX4 devices + state
  pxusb.py attach          # bind (elevated, one prompt) + attach ALL recognized
  pxusb.py attach console jtag   # only those roles
  pxusb.py detach          # detach all recognized from WSL (no admin needed)
  pxusb.py autoattach board      # run the auto-attach loop for flashing
                                 # (re-grabs the board when it re-enumerates
                                 #  into the bootloader — see SKILL.md §5)

Filters accept: a role (console|jtag|board), 'all', a VID:PID (e.g. 0403:6001),
or a busid (e.g. 3-4).

Extend RECOGNITION below for boards this doesn't classify yet.
"""

import argparse
import glob
import json
import os
import subprocess
import sys
import time

# --- device recognition -----------------------------------------------------
# Roles are deliberately coarse: console (UART bridge), jtag (SWD probe),
# board (PX4 FMU VCP or serial bootloader). Classification is by USB VID first
# (unambiguous for FTDI/CP210x/ST-Link in this context), then by known VID:PID,
# then by product-name keyword. Add entries here for new hardware.
FTDI_VIDS = {"0403"}                    # FTDI FT232/FT2232 console adapters
CP210X_VIDS = {"10c4"}                  # Silicon Labs CP210x console adapters
STLINK_VIDS = {"0483"}                  # ST-Link V2/V2.1/V3 (SWD/JTAG)

# PX4 board USB VCPs and serial/DFU bootloaders (vid:pid, lowercase).
BOARD_VIDPID = {
    "1b8c:0036": "Aerium Apex H7 (app VCP)",
    "26ac:0011": "PX4 FMU (app VCP)",
    "26ac:0032": "Pixhawk bootloader",
    "3162:0709": "PX4 serial bootloader",
    "1209:5741": "PX4 (generic)",
}
# Bootloader/DFU PIDs get a badge in the listing (they matter for flashing).
BOOTLOADER_VIDPID = {"26ac:0032", "3162:0709"}
# Fallback: product-name keywords that mark a PX4 flight controller VCP.
BOARD_KEYWORDS = ("px4", "pixhawk", "fmu", "aerium", "apex", "laminar",
                  "radian", "holybro", "cube", "auterion")

ROLE_ORDER = {"console": 0, "jtag": 1, "board": 2}


def classify(vid, pid, desc):
    """Return role str (console|jtag|board) or None if not a PX4 debug device."""
    vp = f"{vid}:{pid}"
    if vid in STLINK_VIDS:
        return "jtag"
    if vid in FTDI_VIDS or vid in CP210X_VIDS:
        return "console"
    if vp in BOARD_VIDPID:
        return "board"
    if any(k in desc.lower() for k in BOARD_KEYWORDS):
        return "board"
    return None


# --- usbipd (Windows host) plumbing -----------------------------------------
def _ps(args, **kw):
    """Run a powershell.exe command from WSL, return CompletedProcess."""
    return subprocess.run(["powershell.exe", "-NoProfile", "-Command"] + args,
                          capture_output=True, text=True, **kw)


def usbipd_state():
    """Parse `usbipd state` JSON into a list of device dicts we care about."""
    r = _ps(["usbipd state"])
    if r.returncode != 0:
        sys.exit(f"error: `usbipd state` failed — is usbipd-win installed?\n{r.stderr.strip()}")
    try:
        raw = json.loads(r.stdout)["Devices"]
    except (ValueError, KeyError) as e:
        sys.exit(f"error: could not parse usbipd JSON: {e}")

    devs = []
    for d in raw:
        if not d.get("BusId"):
            continue  # persisted-only / not currently connected -> skip
        inst = d.get("InstanceId", "")
        # InstanceId looks like:  USB\VID_0403&PID_6001\A5069RR4
        vid = pid = serial = ""
        try:
            body = inst.split("\\", 1)[1]           # VID_0403&PID_6001\A5069RR4
            vidpid, serial = body.split("\\", 1)
            vid = vidpid.split("VID_")[1].split("&")[0].lower()
            pid = vidpid.split("PID_")[1].lower()
        except (IndexError, ValueError):
            pass
        role = classify(vid, pid, d.get("Description", ""))
        if role is None:
            continue
        shared = bool(d.get("PersistedGuid"))
        attached = bool(d.get("ClientIPAddress"))
        devs.append({
            "busid": d.get("BusId", "?"),
            "vid": vid, "pid": pid, "vidpid": f"{vid}:{pid}", "serial": serial,
            "desc": d.get("Description", ""),
            "role": role,
            "state": "attached" if attached else "shared" if shared else "not-shared",
            "bootloader": f"{vid}:{pid}" in BOOTLOADER_VIDPID,
        })
    devs.sort(key=lambda x: (ROLE_ORDER.get(x["role"], 9), x["busid"]))
    return devs


def dev_node(dev):
    """Best-effort map an attached device to its /dev/serial/by-id path."""
    if dev["state"] != "attached":
        return ""
    ser = dev["serial"]
    for link in sorted(glob.glob("/dev/serial/by-id/*")):
        name = os.path.basename(link)
        if ser and len(ser) > 1 and ser in name:
            return f"{link} -> {os.path.realpath(link)}"
    # fall back to keyword match on the by-id name (boards with serial '0', etc.)
    for link in sorted(glob.glob("/dev/serial/by-id/*")):
        name = os.path.basename(link).lower()
        if any(k in name for k in BOARD_KEYWORDS + ("ftdi", "stlink", "cp210")):
            if dev["role"] == "jtag" and "stlink" in name:
                return f"{link} -> {os.path.realpath(link)}"
            if dev["role"] == "console" and ("ftdi" in name or "cp210" in name):
                return f"{link} -> {os.path.realpath(link)}"
            if dev["role"] == "board" and any(k in name for k in BOARD_KEYWORDS):
                return f"{link} -> {os.path.realpath(link)}"
    return ""


# --- filtering ---------------------------------------------------------------
def select(devs, filters):
    """Filter devices by role / 'all' / vid:pid / busid. Empty filters => all."""
    if not filters or "all" in filters:
        return devs
    out = []
    for d in devs:
        for f in filters:
            fl = f.lower()
            if fl in (d["role"], d["vidpid"], d["busid"]):
                out.append(d)
                break
    return out


# --- actions -----------------------------------------------------------------
def cmd_status(devs, filters):
    devs = select(devs, filters)
    if not devs:
        print("No recognized PX4 debug devices found on the Windows host.")
        print("(Is the cable plugged in? Extend RECOGNITION in pxusb.py for new hardware.)")
        return 0
    print(f"{'ROLE':<8} {'BUSID':<6} {'VID:PID':<11} {'STATE':<10} DEVICE")
    print("-" * 78)
    for d in devs:
        badge = "  [BOOTLOADER]" if d["bootloader"] else ""
        label = BOARD_VIDPID.get(d["vidpid"], "") or d["desc"][:30]
        print(f"{d['role']:<8} {d['busid']:<6} {d['vidpid']:<11} {d['state']:<10} {label}{badge}")
        node = dev_node(d)
        if node:
            print(f"{'':<37}{node}")
    # hint line
    need = [d for d in devs if d["state"] != "attached"]
    if need:
        print(f"\n{len(need)} device(s) not yet in WSL — run:  pxusb.py attach")
    else:
        print("\nAll recognized devices attached to WSL.")
    return 0


def elevated_bind(busids):
    """Bind all busids in ONE elevated (UAC) PowerShell. Returns True if all bound."""
    inner = "; ".join(f"usbipd bind --busid {b}" for b in busids) + "; Start-Sleep 1"
    ps = ("Start-Process powershell -Verb RunAs -Wait -ArgumentList "
          "'-NoProfile','-Command','" + inner + "'")
    print(f"Requesting admin to bind: {', '.join(busids)}  (approve the UAC prompt)...")
    _ps([ps])  # launcher exit code is unreliable; verify via re-query below
    time.sleep(1)
    now = {d["busid"]: d["state"] for d in usbipd_state()}
    return all(now.get(b, "not-shared") != "not-shared" for b in busids)


def cmd_attach(devs, filters):
    targets = select(devs, filters)
    if not targets:
        print("No matching recognized devices to attach.")
        return 1

    to_bind = [d["busid"] for d in targets if d["state"] == "not-shared"]
    if to_bind:
        if not elevated_bind(to_bind):
            print("\nCould not bind (UAC declined?). Run this ONCE in an ADMIN PowerShell:")
            for b in to_bind:
                print(f"  usbipd bind --busid {b}")
            print("Binding persists by hardware-id, so it is a one-time step.")
            return 1

    # re-query after bind, then attach everything not already attached
    devs = usbipd_state()
    targets = select(devs, filters)
    for d in targets:
        if d["state"] == "attached":
            print(f"{d['role']:<8} {d['busid']}  already attached")
            continue
        r = _ps([f"usbipd attach --wsl --busid {d['busid']}"])
        ok = r.returncode == 0
        print(f"{d['role']:<8} {d['busid']}  attach {'OK' if ok else 'FAILED: ' + r.stderr.strip()}")

    time.sleep(2)  # let udev enumerate the /dev nodes
    print()
    return cmd_status(select(usbipd_state(), filters), filters)


def cmd_detach(devs, filters):
    targets = [d for d in select(devs, filters) if d["state"] == "attached"]
    if not targets:
        print("No matching attached devices to detach.")
        return 0
    for d in targets:
        r = _ps([f"usbipd detach --busid {d['busid']}"])
        ok = r.returncode == 0
        print(f"{d['role']:<8} {d['busid']}  detach {'OK' if ok else 'FAILED: ' + r.stderr.strip()}")
    return 0


def cmd_autoattach(devs, filters):
    targets = select(devs, filters)
    if len(targets) != 1:
        print("autoattach needs exactly one target (e.g. `pxusb.py autoattach board`).")
        for d in targets:
            print(f"  matched: {d['role']} {d['busid']} {d['vidpid']}")
        return 1
    d = targets[0]
    if d["state"] == "not-shared":
        if not elevated_bind([d["busid"]]):
            print(f"Bind first (admin):  usbipd bind --busid {d['busid']}")
            return 1
    print(f"Auto-attaching {d['role']} {d['busid']} — re-grabs it across re-enumeration "
          f"(e.g. app→bootloader). Ctrl-C to stop.")
    # foreground blocking loop; the caller typically backgrounds this before flashing
    os.execvp("powershell.exe",
              ["powershell.exe", "-NoProfile", "-Command",
               f"usbipd attach --wsl --busid {d['busid']} --auto-attach"])


CMDS = {"status": cmd_status, "attach": cmd_attach,
        "detach": cmd_detach, "autoattach": cmd_autoattach}


def main():
    ap = argparse.ArgumentParser(
        description="One-command USB/IP helper for PX4 hardware debug on WSL.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="filters: role (console|jtag|board) | all | VID:PID | busid")
    ap.add_argument("command", nargs="?", default="status", choices=CMDS.keys())
    ap.add_argument("filters", nargs="*", help="roles / all / VID:PID / busid")
    args = ap.parse_args()
    devs = usbipd_state()
    sys.exit(CMDS[args.command](devs, args.filters))


if __name__ == "__main__":
    main()
