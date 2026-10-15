"""Optional isolated Hailo predictor; never opens RCON or controls the game."""
import argparse
from contextlib import ExitStack
import hashlib
import json
import logging
import os
from pathlib import Path
import socket
import time
import urllib.request

from telemetry.health import METRICS, eligible, finite, read_document

INPUTS = ('player_count', 'entities')
INPUT_SCALES = (64.0, 100000.0)
OUTPUTS = tuple(METRICS)
OUTPUT_SCALES = (100.0, 100.0, 100.0, 1024.0, 100.0, 1024.0, 1024.0, 100.0, 1000.0)
SIDE = 32


def load_manifest(path):
    manifest = read_document(path)
    if (manifest.get('version') != 1 or manifest.get('architecture') not in ('hailo8', 'hailo8l')
            or manifest.get('inputs') != list(INPUTS) or manifest.get('outputs') != list(OUTPUTS)
            or manifest.get('input_scales') != list(INPUT_SCALES)
            or manifest.get('output_scales') != list(OUTPUT_SCALES)
            or manifest.get('side') != SIDE):
        raise ValueError('Unsupported HAT model contract')
    for name, count, width in (('coefficients', len(OUTPUTS), 3), ('input_range', 2, 2)):
        rows = manifest.get(name)
        if (not isinstance(rows, list) or len(rows) != count
                or any(not isinstance(row, list) or len(row) != width
                       or not all(finite(value) for value in row) for row in rows)):
            raise ValueError('Invalid model coefficients or input domain')
    scales = manifest.get('residual_scale')
    if (not isinstance(scales, list) or len(scales) != len(OUTPUTS)
            or not all(finite(value) and value > 0 for value in scales)):
        raise ValueError('Invalid model residual scales')
    for low, high in manifest['input_range']:
        if low < 0 or high < low:
            raise ValueError('Invalid model input domain')
    digest = manifest.get('hef_sha256')
    if not isinstance(digest, str) or len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest):
        raise ValueError('Model must be bound to a compiled HEF checksum')
    return manifest


def inputs(row, manifest):
    if not eligible(row):
        raise ValueError('Fresh, healthy game telemetry required')
    values = []
    for key, scale, (low, high) in zip(INPUTS, INPUT_SCALES, manifest['input_range']):
        value = row[key]
        if not low <= value <= high:
            raise ValueError('Workload outside training domain')
        values.append(value / scale)
    return values


def reference_prediction(values, manifest):
    return [max(0.0, coefficients[0] * values[0] + coefficients[1] * values[1] + coefficients[2])
            for coefficients in manifest['coefficients']]


def evaluate(row, prediction, manifest):
    if len(prediction) != len(OUTPUTS) or not all(finite(value) for value in prediction):
        raise ValueError('Invalid HAT output')
    expected = reference_prediction(inputs(row, manifest), manifest)
    if any(abs(value - reference) > 0.05 for value, reference in zip(prediction, expected)):
        raise ValueError('HAT output differs from CPU reference by more than 0.05 normalized units')
    observed = {}
    findings = []
    for key, output, scale, residual in zip(OUTPUTS, prediction, OUTPUT_SCALES, manifest['residual_scale']):
        predicted = max(0.0, output) * scale
        observed[key] = round(predicted, 3)
        if not finite(row.get(key)):
            continue
        label, direction, floor = METRICS[key]
        delta = (row[key] - predicted) * (-1 if direction == 'lower' else 1)
        score = delta / max(floor, residual)
        if score >= 3:
            findings.append({'metric': key, 'title': label + ' outside HAT prediction',
                             'severity': 'review', 'observed': round(row[key], 2),
                             'baseline': round(predicted, 2), 'score': round(score, 2),
                             'detail': f"Observed {row[key]:.1f}; model expected {predicted:.1f}."
                                       ' Advisory deviation, not a diagnosed cause.'})
    return {'version': 1, 'at': time.time(), 'source_at': row['at'], 'backend': 'hailo',
            'status': 'review' if findings else 'ready', 'advisory_only': True,
            'architecture': manifest['architecture'], 'prediction': observed,
            'evidence': findings, 'reference_max_error': max(
                abs(value - reference) for value, reference in zip(prediction, expected))}


class HailoPredictor:
    def __init__(self, hef_path, manifest):
        import numpy as np
        from hailo_platform import FormatType, HailoSchedulingAlgorithm, VDevice

        self.np = np
        self.stack = ExitStack()
        try:
            if hashlib.sha256(Path(hef_path).read_bytes()).hexdigest() != manifest['hef_sha256']:
                raise ValueError('HEF checksum mismatch')
            params = VDevice.create_params()
            params.scheduling_algorithm = HailoSchedulingAlgorithm.ROUND_ROBIN
            self.device = self.stack.enter_context(VDevice(params))
            self.model = self.device.create_infer_model(str(hef_path))
            self.model.set_batch_size(1)
            if len(self.model.inputs) != 1 or len(self.model.outputs) != 1:
                raise ValueError('Expected one HAT input and output')
            if (tuple(self.model.input().shape) != (SIDE, SIDE, len(INPUTS))
                    or tuple(self.model.output().shape) != (SIDE, SIDE, len(OUTPUTS))):
                raise ValueError('Unexpected HEF tensor shapes')
            self.model.input().set_format_type(FormatType.FLOAT32)
            self.model.output().set_format_type(FormatType.FLOAT32)
            self.configured = self.stack.enter_context(self.model.configure())
        except Exception:
            self.stack.close()
            raise

    def predict(self, values):
        source = self.np.empty((SIDE, SIDE, len(INPUTS)), dtype=self.np.float32)
        source[:] = values
        output = self.np.empty((SIDE, SIDE, len(OUTPUTS)), dtype=self.np.float32)
        bindings = self.configured.create_bindings(
            input_buffers={self.model.input().name: source},
            output_buffers={self.model.output().name: output})
        self.configured.run([bindings], timeout=1000)
        if not self.np.isfinite(output).all():
            raise ValueError('Nonfinite HAT output')
        return [float(value) for value in output.mean(axis=(0, 1))]

    def close(self):
        self.stack.close()


def notify(message):
    address = os.environ.get('NOTIFY_SOCKET')
    if address:
        if address.startswith('@'):
            address = '\0' + address[1:]
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.connect(address)
            sock.sendall(message.encode())


def write_result(path, result):
    temporary = path.with_suffix('.tmp')
    with temporary.open('w', encoding='utf-8') as handle:
        temporary.chmod(0o600)
        json.dump(result, handle, allow_nan=False)
    temporary.replace(path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', default='/etc/rustberrypi/telemetry.json')
    parser.add_argument('--model-dir', default='/opt/rustberrypi/models')
    parser.add_argument('--once', action='store_true', help='Run real inference once; nonzero unless ready/review')
    args = parser.parse_args()
    config = read_document(args.config)
    if not config.get('health_hat_result_file'):
        parser.error('HAT analysis is disabled in telemetry configuration')
    output = Path(config['health_hat_result_file'])
    predictor = None
    previous_error = None
    last_source = 0
    notify('READY=1')
    try:
        while True:
            try:
                if predictor is None:
                    manifest = load_manifest(Path(args.model_dir) / 'manifest.json')
                    predictor = HailoPredictor(Path(args.model_dir) / 'server-health.hef', manifest)
                with urllib.request.urlopen(
                        f"http://127.0.0.1:{int(config['port'])}/api/health-sample", timeout=3) as response:
                    content = response.read(1024 * 1024 + 1)
                if len(content) > 1024 * 1024:
                    raise ValueError('Oversized telemetry response')
                row = json.loads(content)
                if not isinstance(row, dict):
                    raise ValueError('Invalid health sample document')
                if (not finite(row.get('at')) or not 0 <= time.time() - row['at'] <= 20
                        or row['at'] <= last_source
                        or Path('/var/lib/rustberrypi-maintenance').exists()):
                    raise ValueError('Telemetry stale, duplicate or in maintenance')
                values = inputs(row, manifest)
                started = time.monotonic()
                prediction = predictor.predict(values)
                inference_ms = (time.monotonic() - started) * 1000
                result = evaluate(row, prediction, manifest)
                result['inference_ms'] = round(inference_ms, 2)
                last_source = row['at']
                previous_error = None
            except Exception as error:
                # HailoRT raises vendor-specific exceptions. This process never substitutes CPU output.
                category = type(error).__name__
                if category != previous_error:
                    logging.exception('HAT inference unavailable')
                previous_error = category
                result = {'version': 1, 'at': time.time(), 'backend': 'hailo',
                          'status': 'error', 'error': category, 'advisory_only': True}
                if predictor is not None:
                    predictor.close()
                    predictor = None
            write_result(output, result)
            notify('WATCHDOG=1')
            if args.once:
                print(json.dumps(result, allow_nan=False))
                return 0 if result['status'] in ('ready', 'review') else 1
            time.sleep(5 if previous_error is None else 10)
    finally:
        if predictor is not None:
            predictor.close()


if __name__ == '__main__':
    raise SystemExit(main())
