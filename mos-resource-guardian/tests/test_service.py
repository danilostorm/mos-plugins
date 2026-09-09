import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from service import atomic_config, client


class ServiceIntegration(unittest.TestCase):
    def test_socket_config_history_singleton_and_shutdown(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config, database, sock = root/"config.json", root/"guardian.db", root/"api.sock"
            atomic_config(config, {"monitor": {"interval_seconds": 1}})
            command = [sys.executable, str(Path(__file__).resolve().parents[1]/"service.py"), "serve",
                       "--config-path", str(config), "--database", str(database), "--socket", str(sock)]
            process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                for _ in range(100):
                    if sock.exists():
                        break
                    if process.poll() is not None:
                        output = process.communicate()
                        if b'Operation not permitted' in output[0] and not os.environ.get('CI'):
                            self.skipTest('Runtime forbids Unix sockets; integration runs in CI')
                        self.fail(str(output))
                    time.sleep(.02)
                self.assertEqual(sock.stat().st_mode & 0o777, 0o600)
                self.assertEqual(client("config", str(sock))["mode"], "observe")
                with self.assertRaises(ValueError):
                    client("configure", str(sock), '{"mode":"invalid"}')
                self.assertEqual(client("config", str(sock))["mode"], "observe")
                client("pause", str(sock))
                self.assertEqual(json.loads(config.read_text())["mode"], "paused")
                with self.assertRaises(ValueError):
                    client("shell", str(sock))
                duplicate = subprocess.run(command, capture_output=True, timeout=5)
                self.assertNotEqual(duplicate.returncode, 0)
                for _ in range(100):
                    status = client("status", str(sock))
                    if status.get("cpu_percent") is not None:
                        break
                    time.sleep(.03)
                self.assertFalse(status["stale"])
                self.assertIn("ram_available_mib", status)
                self.assertTrue(client("history", str(sock)))
            finally:
                process.terminate()
                process.communicate(timeout=10)
            self.assertEqual(process.returncode, 0)
            self.assertFalse(sock.exists())


if __name__ == "__main__":
    unittest.main()
