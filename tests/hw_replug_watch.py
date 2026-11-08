"""Wait for the QUADERNO to be unplugged and plugged back in, record how it
enumerates, then run the CLI so it has to do the USB mode switch itself.
With --tab, that first command is a Tab completion (`quaderno __complete Bo`),
to check that the first Tab after plugging in works."""
import glob
import os
import subprocess
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
from quaderno import usb as Q  # noqa: E402

t0 = time.time()


def log(*a: object) -> None:
    print(f"[{time.time() - t0:6.1f}s]", *a, flush=True)


deadline = t0 + 600
log("waiting for unplug")
while Q.usb_devices():
    if time.time() > deadline: log("timeout waiting for unplug"); sys.exit(2)
    time.sleep(0.5)
log("unplugged; running `quaderno ls` while unplugged (expect exit 3, no QUADERNO on USB)")
r = subprocess.run([sys.executable, "-m", "quaderno", "ls"], cwd=REPO, capture_output=True, text=True, timeout=60)
log("exit", r.returncode, "|", r.stderr.strip())
log("waiting for plug-in")
while not Q.usb_devices():
    if time.time() > deadline: log("timeout waiting for plug-in"); sys.exit(2)
    time.sleep(0.3)
for _ in range(20):  # give the tty node a moment to appear
    devs = Q.usb_devices()
    if any(d.tty or d.pid == Q.PID_ETHERNET for d in devs):
        break
    time.sleep(0.25)
log("enumerated as", [(hex(d.pid), d.serial, d.tty or d.iface) for d in devs])
log("/dev/cu.*:", glob.glob("/dev/cu.*"))
cmd = ["__complete", "Bo"] if "--tab" in sys.argv else ["-v", "ls"]
log("running: quaderno", *cmd)
t1 = time.time()
r = subprocess.run([sys.executable, "-m", "quaderno", *cmd], cwd=REPO,
                   capture_output=True, text=True, timeout=120)
log("exit", r.returncode, f"after {time.time() - t1:.1f} s")
print(r.stderr + r.stdout, flush=True)
log("after:", [(hex(d.pid), d.serial, d.iface, d.link) for d in Q.usb_devices()])
