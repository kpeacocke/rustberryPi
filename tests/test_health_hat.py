"""Verify the HAT contract without pretending a mocked device is real hardware."""
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from telemetry.hat import (INPUTS, INPUT_SCALES, OUTPUTS, OUTPUT_SCALES, SIDE,
                           HailoPredictor, evaluate, inputs, load_manifest, main, reference_prediction)
from telemetry.health import hat_status
from test_server_health import sample


def manifest():
    return {'version': 1, 'architecture': 'hailo8', 'inputs': list(INPUTS),
            'input_scales': list(INPUT_SCALES), 'outputs': list(OUTPUTS),
            'output_scales': list(OUTPUT_SCALES), 'side': SIDE,
            'coefficients': [[0, 0, value / scale] for value, scale in zip(
                (60, 30, 50, 0, 60, 10, 10, 2, 10), OUTPUT_SCALES)],
            'input_range': [[0, 5], [0, 10000]], 'residual_scale': [1] * len(OUTPUTS),
            'hef_sha256': hashlib.sha256(b'model').hexdigest()}


class HatTests(unittest.TestCase):
    def test_manifest_rejects_uncompiled_and_mismatched_models(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'manifest.json'
            data = manifest()
            path.write_text(json.dumps(data))
            self.assertEqual(load_manifest(path), data)
            for field, value in [('hef_sha256', None), ('outputs', ['fps']),
                                 ('coefficients', []), ('residual_scale', [float('nan')] * len(OUTPUTS))]:
                path.write_text(json.dumps({**data, field: value}))
                with self.assertRaises(ValueError):
                    load_manifest(path)

    def test_predictor_uses_workload_counts_not_identities(self):
        self.assertEqual(inputs(sample(1000), manifest()), [2 / 64, 0.01])
        with self.assertRaises(ValueError):
            inputs(sample(1000, player_count=6), manifest())
        with self.assertRaises(ValueError):
            inputs(sample(1000, rcon_ok=False), manifest())

    def test_hardware_output_must_match_cpu_reference(self):
        data = manifest()
        prediction = reference_prediction(inputs(sample(1000), data), data)
        self.assertEqual(evaluate(sample(1000), prediction, data)['status'], 'ready')
        self.assertEqual(evaluate(sample(1000, fps=10), prediction, data)['status'], 'review')
        prediction[0] += 0.06
        with self.assertRaises(ValueError):
            evaluate(sample(1000), prediction, data)

    def test_hat_results_expire_and_disabled_is_explicit(self):
        self.assertEqual(hat_status(None, 1000)['status'], 'disabled')
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'hat.json'
            prediction = reference_prediction(inputs(sample(1000), manifest()), manifest())
            with patch('telemetry.hat.time.time', return_value=1000):
                result = evaluate(sample(1000), prediction, manifest())
            path.write_text(json.dumps(result))
            self.assertEqual(hat_status(path, 1005)['status'], 'ready')
            self.assertEqual(hat_status(path, 1021)['status'], 'unavailable')
            path.write_text('broken')
            self.assertEqual(hat_status(path, 1005)['status'], 'unavailable')

    def test_nonfinite_output_fails_closed(self):
        for prediction in ([float('nan')] * len(OUTPUTS), [0]):
            with self.assertRaises(ValueError):
                evaluate(sample(1000), prediction, manifest())

    def test_once_never_reports_cpu_fallback_as_successful_hardware(self):
        with tempfile.TemporaryDirectory() as folder:
            config = Path(folder) / 'config.json'
            output = Path(folder) / 'hat.json'
            config.write_text(json.dumps({'health_hat_result_file': str(output), 'port': 8090}))
            with patch('sys.argv', ['hat', '--config', str(config), '--once']), \
                    patch('telemetry.hat.load_manifest', return_value=manifest()), \
                    patch('telemetry.hat.HailoPredictor', side_effect=RuntimeError('device unavailable')), \
                    patch('telemetry.hat.notify'), \
                    patch('sys.stdout', new_callable=io.StringIO), self.assertLogs(level='ERROR'):
                self.assertEqual(main(), 1)
            result = json.loads(output.read_text())
            self.assertEqual((result['status'], result['backend']), ('error', 'hailo'))
            self.assertNotIn('prediction', result)

    @unittest.skipUnless(importlib.util.find_spec('numpy'), 'Optional numpy runtime contract test')
    def test_runtime_bindings_shape_float_format_timeout_and_cleanup(self):
        from unittest.mock import MagicMock
        import numpy as np

        api = MagicMock()
        device = api.VDevice.return_value.__enter__.return_value
        model = device.create_infer_model.return_value
        model.inputs = [model.input()]
        model.outputs = [model.output()]
        model.input().shape = (SIDE, SIDE, len(INPUTS))
        model.input().name = 'input'
        model.output().shape = (SIDE, SIDE, len(OUTPUTS))
        model.output().name = 'output'
        configured = model.configure.return_value.__enter__.return_value

        def infer(_bindings, timeout):
            self.assertEqual(timeout, 1000)
            buffers = configured.create_bindings.call_args.kwargs
            self.assertEqual(buffers['input_buffers']['input'].dtype, np.float32)
            np.testing.assert_allclose(buffers['input_buffers']['input'][0, 0], [0.1, 0.2])
            buffers['output_buffers']['output'][:] = 0.25

        configured.run.side_effect = infer
        with tempfile.TemporaryDirectory() as folder:
            hef = Path(folder) / 'model.hef'
            hef.write_bytes(b'model')
            with patch.dict('sys.modules', {'hailo_platform': api}):
                predictor = HailoPredictor(hef, manifest())
                self.assertEqual(predictor.predict([0.1, 0.2]), [0.25] * len(OUTPUTS))
                predictor.close()
        model.input().set_format_type.assert_called_once_with(api.FormatType.FLOAT32)
        model.configure.return_value.__exit__.assert_called_once()
        api.VDevice.return_value.__exit__.assert_called_once()
