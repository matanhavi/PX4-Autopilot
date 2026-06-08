---
name: sitl
description: Run PX4 SITL Gazebo Classic simulator without rebuilding. Calls sitl_run.sh directly, bypassing make. Args: [model [world]] — defaults to iris / none.
argument-hint: "[model] [world]"
allowed-tools: Bash
---

# Run PX4 SITL Gazebo Classic

Launch the simulator against an already-built binary. Never invoke `make`.

## Paths (hardcoded for this repo)

```
SRC=~/src/PX4-Autopilot
BUILD=$SRC/build/px4_sitl_default
BIN=$BUILD/bin/px4
SCRIPT=$SRC/Tools/simulation/gazebo-classic/sitl_run.sh
WORKDIR=$BUILD/rootfs
```

## Argument parsing

Parse `$ARGUMENTS` (the text after `/sitl`):
- First token → `MODEL` (default: `iris`)
- Second token → `WORLD` (default: `none`)

Valid models (from sitl_targets_gazebo-classic.cmake): `iris`, `typhoon_h480`, `plane`, `standard_vtol`, `tailsitter`, `tiltrotor`, `boat`, `omnicopter`, `px4vision`, `quadtailsitter`, `believer`, `glider`, `cloudship`, `techpod`, `uuv_bluerov2_heavy`, `uuv_hippocampus`.

Valid worlds: `none`, `baylands`, `empty`, `ksql_airport`, `mcmillan_airfield`, `ramped_up_wind`, `sonoma_raceway`, `warehouse`, `windy`, `yosemite`.

## Pre-flight checks

Run these before launching — abort with a clear error if any fail:

1. Binary exists: `test -f $BUILD/bin/px4`
2. sitl_run.sh exists: `test -f $SCRIPT`
3. Gazebo plugins built: `test -d $BUILD/build_gazebo-classic`
4. DISPLAY is set (WSL): if `$DISPLAY` is empty, export `DISPLAY=:0`

If binary is missing, tell the user to run `make px4_sitl gazebo-classic` once first.

## Launch

Open a new Windows Terminal window so the simulator output is visible and detached from the Claude panel. Use `wt.exe` via PowerShell, with `tee` so output is also captured to a log for status checks:

```powershell
Start-Process wt -ArgumentList @(
  "wsl", "-d", "Ubuntu-20.04", "--", "bash", "-c",
  "cd `$HOME/src/PX4-Autopilot/build/px4_sitl_default/rootfs && DISPLAY=:0 `$HOME/src/PX4-Autopilot/Tools/simulation/gazebo-classic/sitl_run.sh `$HOME/src/PX4-Autopilot/build/px4_sitl_default/bin/px4 none $MODEL $WORLD `$HOME/src/PX4-Autopilot `$HOME/src/PX4-Autopilot/build/px4_sitl_default 2>&1 | tee /tmp/px4_sitl.log; echo '--- SITL exited ---'; read"
)
```

The trailing `read` keeps the terminal open after SITL exits so the user can inspect the output.

After launching, wait ~10 seconds then check the log:

```bash
wsl -d Ubuntu-20.04 -- bash -c "grep -E 'Simulator connected|Startup script returned|ERROR' /tmp/px4_sitl.log | grep -v 'ekf2 missing data|system power unavailable' | tail -10"
```

Report:
- Whether `Simulator connected on TCP port 4560` appeared (PX4 ↔ Gazebo link up)
- Whether `Startup script returned successfully` appeared
- Any unexpected `ERROR` or `WARN` lines
- Running PIDs from `pgrep -x gzserver` and `pgrep -x px4`

## Sending shell commands to running PX4

Use `mavlink_shell.py` to send commands to a running instance:

```bash
wsl -d Ubuntu-20.04 -- bash -c "echo 'COMMAND' | timeout 8 python3 \$HOME/src/PX4-Autopilot/Tools/mavlink_shell.py 0.0.0.0:14550 2>&1"
```

Example: `commander takeoff`, `commander land`, `commander status`.

## Stopping

If the user passes `stop` as the argument, kill the running simulator:
```bash
kill $(cat /tmp/px4_sitl.pid 2>/dev/null) 2>/dev/null && echo "Simulator stopped" || echo "No running simulator found"
pkill -x gzserver 2>/dev/null; pkill -x gzclient 2>/dev/null
```
