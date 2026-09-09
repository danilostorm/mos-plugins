import importlib.util
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location("guardian", Path(__file__).resolve().parents[1] / "guardian.py")
g = importlib.util.module_from_spec(spec)
spec.loader.exec_module(g)


class Tests(unittest.TestCase):
    def sample(self, cpu=10, available=8000):
        return dict(timestamp=1000000, cpu_percent=cpu, ram_total_mib=16000, ram_available_mib=available)

    def test_guest_not_double_counted(self):
        self.assertEqual(g.cpu_counters("cpu 100 0 50 800 20 10 10 10 40 0"), (1000, 820))

    def test_mem_available(self):
        self.assertEqual(g.memory("MemTotal: 4096 kB\nMemFree: 1 kB\nMemAvailable: 2048 kB"), (4, 2))

    def test_missing_memory_fails_closed(self):
        with self.assertRaises(KeyError):
            g.memory("MemTotal: 4096 kB")

    def test_debounce_and_recovery(self):
        p = g.Policy(g.config())
        self.assertEqual(p.evaluate(self.sample(99))["state"], "warming_up")
        p.evaluate(self.sample(99))
        self.assertEqual(p.evaluate(self.sample(99))["state"], "pressure")
        for _ in range(5):
            self.assertEqual(p.evaluate(self.sample())["state"], "pressure")
        self.assertEqual(p.evaluate(self.sample())["state"], "normal")

    def test_emergency_without_cpu_sample(self):
        p = g.Policy(g.config())
        self.assertEqual(p.evaluate(self.sample(None, 500))["state"], "emergency")
        self.assertEqual(p.evaluate(self.sample(99))["state"], "emergency")

    def test_percentage_reserve(self):
        p = g.Policy(g.config())
        s = self.sample()
        s["ram_total_mib"] = 64000
        self.assertEqual(p.evaluate(s)["ram_reserve_mib"], 6400)

    def test_no_recovery_in_hysteresis_band(self):
        p = g.Policy(g.config())
        p.evaluate(self.sample(90, 500))
        for _ in range(10):
            self.assertEqual(p.evaluate(self.sample(83, 2200))["state"], "emergency")

    def test_config_rejects_unknown_and_nan(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "config.json"
            for data in ('{"unknown": 5}', '{"interval_seconds": NaN}', '{"pressure_samples": 1.5}', '{"cpu_reserve_percent": 99}', '{"interval_seconds": true}'):
                path.write_text(data)
                with self.assertRaises(ValueError):
                    g.config(path)

    def test_database_retention(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "test.db"
            h = g.History(path)
            p = g.Policy(g.config())
            h.add(p.evaluate(self.sample()), 1)
            s = self.sample()
            s["timestamp"] += 86401
            h.add(p.evaluate(s), 1)
            h.close()
            self.assertEqual(len(g.read_history(path, 100)), 1)

    def test_status_does_not_create_database(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "missing.db"
            with self.assertRaises(g.sqlite3.Error):
                g.read_history(path, 1)
            self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()
