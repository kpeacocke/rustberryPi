"""Actual ONNX export parity; compiler calls are contract-mocked, not certified."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from telemetry.hat import INPUTS, INPUT_SCALES, SIDE, load_manifest, reference_prediction
from test_server_health import sample
from tools.train_server_health import compile_model, train, training_rows

HAS_TOOLS = importlib.util.find_spec('numpy') is not None and importlib.util.find_spec('onnx') is not None


class TrainingInputTests(unittest.TestCase):
    def test_insufficient_or_corrupt_training_data_fails_explicitly(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(ValueError):
                training_rows(folder)
            path = Path(folder) / 'features-2026-01-01.jsonl'
            path.write_text('broken\n')
            with self.assertRaisesRegex(ValueError, 'corrupt feature row'):
                training_rows(folder)


@unittest.skipUnless(HAS_TOOLS, 'Install requirements-health-model.txt for actual ONNX export tests')
class TrainingExportTests(unittest.TestCase):
    def test_training_exports_executable_onnx_and_binds_compiled_hef(self):
        import numpy as np
        from onnx.reference import ReferenceEvaluator

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            features = root / 'features'
            features.mkdir()
            path = features / 'features-2026-01-01.jsonl'
            with path.open('w') as handle:
                for index in range(1400):
                    players = index % 6
                    handle.write(json.dumps(sample(1000 + index * 5, player_count=players,
                                                   entities=1000 + index % 20 * 100,
                                                   cpu_mean=20 + players * 5, fps=60 - players * 2)) + '\n')
            output = root / 'model'
            metadata = train(features, output, 'hailo8')
            evaluator = ReferenceEvaluator(str(output / 'server-health.onnx'))
            values = [2 / INPUT_SCALES[0], 1000 / INPUT_SCALES[1]]
            tensor = np.empty((1, len(INPUTS), SIDE, SIDE), dtype=np.float32)
            tensor[:] = np.asarray(values)[None, :, None, None]
            prediction = evaluator.run(None, {'telemetry': tensor})[0].mean(axis=(0, 2, 3))
            np.testing.assert_allclose(prediction, reference_prediction(values, metadata), atol=1e-5)
            with self.assertRaises(ValueError):
                load_manifest(output / 'manifest.json')
            runner = MagicMock()
            runner.return_value.compile.return_value = b'compiled-model-contract-fixture'
            with patch.dict('sys.modules', {'hailo_sdk_client': MagicMock(ClientRunner=runner)}):
                compile_model(output)
            runner.assert_called_once_with(hw_arch='hailo8')
            self.assertEqual(load_manifest(output / 'manifest.json')['architecture'], 'hailo8')
            with self.assertRaises(ValueError):
                train(features, output, 'hailo8')
