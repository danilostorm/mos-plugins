#!/usr/bin/env python3
"""MOS lifecycle and private Unix-socket API. No listening TCP port."""
import argparse
import base64
import copy
import fcntl
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import signal
import socket
import socketserver
import sqlite3
import tempfile
import threading
import time
from guardian import Monitor, Policy, History
from control import validate, DEFAULT
from engine import Engine
from autovm import AutoVM
from affinity import Affinity

CONFIG = "/boot/optional/plugins/plugins/settings.json"
SOCKET = "/run/mos-resource-guardian/api.sock"
DATABASE = "/var/lib/mos-resource-guardian/guardian.db"


def atomic_config(path, data):
    cfg = validate(data)
    parent = Path(path).parent
    parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=parent, prefix=".guardian-")
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(cfg, stream, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
        directory = os.open(parent, os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return cfg


class Application:
    def __init__(self, config_path, database):
        self.path, self.database = config_path, database
        self.cfg = validate(json.loads(Path(config_path).read_text()))
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.restore = False
        self.cancel_restore = False
        self.status = dict(state="starting", mode=self.cfg["mode"], timestamp=0)
        self.discovery = AutoVM()

    def request(self, data):
        op = data.get("op")
        if op == "status":
            result = copy.deepcopy(self.status)
            result["stale"] = time.time()-result["timestamp"] > max(30, self.cfg["monitor"]["interval_seconds"]*3)
            return result
        if op == "config":
            return copy.deepcopy(self.cfg)
        if op == "discover":
            with self.lock:
                self.discovery.last = -float('inf')
                self.discovery.resolve(self.cfg)
                return dict(vms=self.discovery.catalog)
        if op == "history":
            db = sqlite3.connect(Path(self.database).resolve().as_uri()+"?mode=ro", uri=True)
            try:
                return [json.loads(r[0]) for r in db.execute("SELECT payload FROM samples ORDER BY ts DESC LIMIT 120")]
            finally:
                db.close()
        if op in ("configure", "pause", "automatic", "observe", "restore"):
            with self.lock:
                cfg = data.get("config") if op == "configure" else copy.deepcopy(self.cfg)
                if op in ("pause", "restore"):
                    cfg["mode"] = "paused"
                elif op in ("automatic", "observe"):
                    cfg["mode"] = op
                self.cfg = atomic_config(self.path, cfg)
                if op == "restore":
                    self.restore = True
                else:
                    self.restore = False
                    self.cancel_restore = True
                return dict(ok=True, mode=self.cfg["mode"], restart_required=str(Path(self.cfg["data_directory"])/"guardian.db") != self.database)
        raise ValueError("Unknown operation")

    def loop(self):
        history = History(self.database)
        engine = Engine(history.db)
        affinity = Affinity(history.db)
        automatic_vms = self.discovery
        monitor, policy = Monitor(), Policy(self.cfg["monitor"])
        rows = [json.loads(r[0]) for r in history.db.execute("SELECT payload FROM samples ORDER BY ts DESC LIMIT 120")][::-1]
        try:
            while not self.stop.is_set():
                started = time.monotonic()
                try:
                    # Serialize configuration changes with actuator operations.
                    with self.lock:
                        cfg = copy.deepcopy(self.cfg)
                        cfg = automatic_vms.resolve(cfg)
                        automatic_vms.enable_stats(cfg)
                        if policy.c != cfg["monitor"]:
                            policy = Policy(cfg["monitor"])
                        record = policy.evaluate(monitor.sample())
                        rows = (rows+[record])[-120:]
                        if self.cancel_restore:
                            engine.restore_requested = False
                            self.cancel_restore = False
                        engine.restore_requested = engine.restore_requested or self.restore
                        affinity.tick(cfg, restore=engine.restore_requested)
                        self.restore = False
                        prediction = engine.tick(cfg, record, rows)
                        record.update(mode=cfg["mode"], forecast=prediction,
                                      auto_vms=automatic_vms.catalog,
                                      affinity=affinity.status,
                                      database=self.database,
                                      restart_required=str(Path(cfg["data_directory"])/"guardian.db") != self.database,
                                      profile=prediction["profile"] if cfg["profile"] == "automatic" else cfg["profile"],
                                      targets=engine.inventory, blocked=engine.unresolved(),
                                      restoring=engine.restore_requested)
                        history.add(record, cfg["monitor"]["retention_days"])
                        self.status = dict(record, actions=engine.audit(30))
                        with history.db:
                            # Preserve live rollback lineage; bound resolved audit history.
                            history.db.execute("DELETE FROM actions WHERE ts < ? AND status IN ('restored','expired','superseded')", (time.time()-cfg["monitor"]["retention_days"]*86400,))
                except Exception as exc:
                    logging.exception("Cycle failed; no further writes in this cycle")
                    self.status = dict(state="error", error=str(exc)[:300], timestamp=time.time(), mode=self.cfg["mode"])
                self.stop.wait(max(.1, self.cfg["monitor"]["interval_seconds"]-(time.monotonic()-started)))
        finally:
            history.close()


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        self.connection.settimeout(60)
        try:
            line = self.rfile.readline(65537)
            if len(line) > 65536:
                raise ValueError("Request too large")
            data = json.loads(line)
            if not isinstance(data, dict):
                raise ValueError("Expected object")
            response = dict(ok=True, result=self.server.app.request(data))
        except Exception as exc:
            response = dict(ok=False, error=str(exc)[:300])
        self.wfile.write((json.dumps(response, allow_nan=False)+"\n").encode())


def serve(config_path, database, sockpath):
    os.umask(0o077)
    Path(sockpath).parent.mkdir(parents=True, exist_ok=True)
    Path(database).parent.mkdir(parents=True, exist_ok=True)
    log = RotatingFileHandler(str(Path(sockpath).parent / "events.log"), maxBytes=1048576, backupCount=2)
    logging.getLogger().addHandler(log)
    logging.getLogger().setLevel(logging.INFO)
    with open(str(sockpath)+".lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        app = Application(config_path, database)
        if Path(sockpath).exists():
            Path(sockpath).unlink()
        with socketserver.ThreadingUnixStreamServer(sockpath, Handler) as server:
            server.daemon_threads = True
            server.app = app
            os.chmod(sockpath, 0o600)
            server.timeout = .5
            for sig in (signal.SIGINT, signal.SIGTERM):
                signal.signal(sig, lambda *_: app.stop.set())
            worker = threading.Thread(target=app.loop)
            worker.start()
            try:
                while not app.stop.is_set():
                    server.handle_request()
            finally:
                app.stop.set()
                worker.join()
                Path(sockpath).unlink(missing_ok=True)


def client(op, sockpath=SOCKET, payload=None):
    request = dict(op=op)
    if payload is not None:
        request["config"] = decode_config(payload)
    with socket.socket(socket.AF_UNIX) as conn:
        conn.settimeout(60)
        conn.connect(sockpath)
        conn.sendall((json.dumps(request)+"\n").encode())
        stream = conn.makefile("rb")
        result = json.loads(stream.readline(4194304))
        if not result["ok"]:
            raise ValueError(result["error"])
        return result["result"]


def decode_config(payload):
    # A data-only transport for MOS query arguments; decoded JSON still passes validate().
    if payload.startswith('b64:'):
        if len(payload) > 80000:
            raise ValueError('Configuration too large')
        payload = base64.b64decode(payload[4:], validate=True).decode('utf-8')
    return json.loads(payload)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["serve", "status", "history", "config", "configure", "discover", "pause", "automatic", "observe", "restore", "validate"])
    parser.add_argument("payload", nargs="?")
    parser.add_argument("--config-path", default=CONFIG)
    parser.add_argument("--database", default=DATABASE)
    parser.add_argument("--socket", default=SOCKET)
    args = parser.parse_args()
    try:
        if args.command == "serve":
            serve(args.config_path, args.database, args.socket)
        elif args.command == "validate":
            print(json.dumps(validate(json.loads(Path(args.config_path).read_text()))))
        else:
            print(json.dumps(client(args.command, args.socket, args.payload), allow_nan=False))
    except Exception as exc:
        print(json.dumps(dict(error=str(exc)[:300])))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
