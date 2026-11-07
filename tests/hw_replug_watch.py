"""Wait for the QUADERNO to be unplugged and plugged back in, record how it
enumerates, then run the CLI so it has to do the USB mode switch itself."""
import glob, os, subprocess, sys, time
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
from quaderno import usb as Q

t0 = time.time()
def log(*a):
    print(f"[{time.time() - t0:6.1f}s]", *a, flush=True)

deadline = t0 + 600
log("waiting for unplug")
while Q.usb_devices():
    if time.time() > deadline: log("timeout waiting for unplug"); sys.exit(2)
    time.sleep(0.5)
log("unplugged; waiting for plug-in")
while not Q.usb_devices():
    if time.time() > deadline: log("timeout waiting for plug-in"); sys.exit(2)
    time.sleep(0.3)
for _ in range(20):  # give the tty node a moment to appear
    devs = Q.usb_devices()
    if any(tty for _, _, tty in devs) or any(pid == Q.PID_ETHERNET for pid, _, _ in devs):
        break
    time.sleep(0.25)
log("enumerated as", [(hex(pid), ser, tty) for pid, ser, tty in devs])
log("/dev/cu.*:", glob.glob("/dev/cu.*"))
log("running: quaderno -v ls")
r = subprocess.run([sys.executable, "-m", "quaderno", "-v", "ls"], cwd=REPO,
                   capture_output=True, text=True, timeout=120)
log("exit", r.returncode)
print(r.stderr + r.stdout, flush=True)
log("after:", [(hex(pid), ser, tty) for pid, ser, tty in Q.usb_devices()])
