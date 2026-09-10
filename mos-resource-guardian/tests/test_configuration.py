import base64
import json
from pathlib import Path
import sys
import tempfile
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from service import Application, atomic_config, decode_config
from autovm import AutoVM


class ConfigurationTests(unittest.TestCase):
    def test_transport_persistence_and_restart(self):
        data = {'auto_vms': True, 'data_directory': '/mnt/disco-ação/guardian'}
        payload = 'b64:' + base64.b64encode(json.dumps(data, ensure_ascii=False).encode()).decode()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'settings.json'
            atomic_config(path, {})
            app = Application(path, str(Path(directory)/'db'))
            app.request(dict(op='configure', config=decode_config(payload)))
            self.assertTrue(app.request(dict(op='config'))['auto_vms'])
            reloaded = Application(path, str(Path(directory)/'db'))
            self.assertTrue(reloaded.cfg['auto_vms'])
            self.assertEqual(reloaded.cfg['data_directory'], data['data_directory'])

    def test_readonly_discovery_error_is_visible(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'settings.json'
            atomic_config(path, {})
            app = Application(path, str(Path(directory)/'db'))
            def fail(args):
                raise RuntimeError('libvirt unavailable')
            app.discovery = AutoVM(fail)
            result = app.request(dict(op='discover'))
            self.assertIn('libvirt unavailable', result['vms'][0]['error'])
            self.assertFalse(app.cfg['auto_vms'])

    def test_invalid_encoded_data_rejected(self):
        for payload in ['b64:!!!', 'b64:' + base64.b64encode(b'not json').decode()]:
            with self.assertRaises(ValueError):
                decode_config(payload)
