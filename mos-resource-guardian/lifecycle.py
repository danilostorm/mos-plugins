"""SysV/MOS launcher: PID identity validation and bounded log rotation."""
import fcntl
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from service import CONFIG, SOCKET, client, atomic_config
from control import DEFAULT, validate

RUN = Path("/run/mos-resource-guardian")
PID = RUN / "process.json"
SCRIPT = "/usr/lib/mos-resource-guardian/service.py"


def identity(pid):
    return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]


def alive():
    try:
        item = json.loads(PID.read_text())
        pid = item["pid"]
        args = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
        if identity(pid) == item["start"] and SCRIPT.encode() in args:
            return pid
    except (OSError, ValueError, KeyError):
        pass
    return None


def main(op):
    os.umask(0o077)
    RUN.mkdir(parents=True, exist_ok=True)
    with (RUN / "lifecycle.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        pid = alive()
        if op == "status":
            print(json.dumps(dict(running=bool(pid), pid=pid)))
            return 0 if pid else 3
        if op == "stop":
            if pid:
                # pidfd pins process identity even if it exits and PID is reused.
                fd = os.pidfd_open(pid)
                try:
                    if alive() == pid:
                        signal.pidfd_send_signal(fd, signal.SIGTERM)
                        for _ in range(600):
                            if alive() != pid:
                                break
                            time.sleep(.1)
                        else:
                            raise RuntimeError("Daemon did not stop; no force kill issued")
                finally:
                    os.close(fd)
            return 0
        if op != "start":
            raise ValueError("Invalid lifecycle operation")
        if pid:
            return 0
        if not Path(CONFIG).exists():
            atomic_config(CONFIG, DEFAULT)
        cfg = validate(json.loads(Path(CONFIG).read_text()))
        directory = Path(cfg["data_directory"])
        if str(directory).startswith("/mnt/"):
            ancestor = directory
            while not ancestor.exists():
                ancestor = ancestor.parent
            if not any(os.path.ismount(p) for p in (ancestor, *ancestor.parents) if str(p).startswith("/mnt/")):
                raise RuntimeError("Configured data disk is not mounted; refusing RAM/boot fallback")
        # /var/lib can be redirected to a persistent MOS data disk with a bind mount.
        log = RUN / "daemon.log"
        if log.exists() and log.stat().st_size > 1048576:
            log.replace(RUN / "daemon.log.1")
        with log.open("ab") as stream:
            process = subprocess.Popen(["/usr/bin/python3", SCRIPT, "serve", "--database", str(directory/"guardian.db")], stdin=subprocess.DEVNULL,
                                       stdout=stream, stderr=stream, start_new_session=True)
        PID.write_text(json.dumps(dict(pid=process.pid, start=identity(process.pid))))
        for _ in range(50):
            if process.poll() is not None:
                raise RuntimeError("Daemon failed; inspect /run/mos-resource-guardian/daemon.log")
            try:
                client("status")
                return 0
            except (OSError, ValueError):
                time.sleep(.1)
        raise RuntimeError("Daemon startup timeout")


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1]))
