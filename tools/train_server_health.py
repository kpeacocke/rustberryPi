#!/usr/bin/env python3
"""Train a workload-conditioned predictor, then compile it with Hailo's SDK."""
import argparse
from collections import deque
import hashlib
import json
from pathlib import Path
import sys
import time

# Allow running the operator tool directly from any directory.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from telemetry.hat import INPUTS, INPUT_SCALES, OUTPUTS, OUTPUT_SCALES, SIDE
from telemetry.health import METRICS, eligible, finite


def training_rows(directory):
    rows = deque(maxlen=20000)
    last_at = 0
    for path in sorted(Path(directory).glob('features-*.jsonl')):
        with path.open(encoding='utf-8') as handle:
            for index, line in enumerate(iter(lambda: handle.readline(65537), ''), 1):
                if len(line) > 65536:
                    raise ValueError(f'{path.name}:{index}: oversized feature row')
                try:
                    row = json.loads(line)
                except ValueError as error:
                    raise ValueError(f'{path.name}:{index}: corrupt feature row') from error
                if not isinstance(row, dict):
                    raise ValueError(f'{path.name}:{index}: feature row must be an object')
                if not eligible(row):
                    continue
                if (row['at'] <= last_at
                        or not all(finite(row.get(key)) and row[key] >= 0 for key in OUTPUTS)
                        or any(row.get(flag) is True for flag in
                               ('throttled_now', 'throttle_cpu_now', 'watchdog_blocked'))):
                    continue
                last_at = row['at']
                rows.append(row)
    if len(rows) < 1200 or rows[-1]['at'] - rows[0]['at'] < 3600:
        raise ValueError('Need at least 1200 complete healthy samples spanning at least one hour')
    return list(rows)


def train(directory, output, architecture):
    import numpy as np
    import onnx
    from onnx import TensorProto, helper, numpy_helper

    rows = training_rows(directory)
    if output.exists() and any(output.iterdir()):
        raise ValueError('Output directory must be empty; preserve previous model versions')
    output.mkdir(parents=True, exist_ok=True)
    x = np.asarray([[row[key] / scale for key, scale in zip(INPUTS, INPUT_SCALES)]
                    for row in rows], dtype=np.float32)
    y = np.asarray([[row[key] / scale for key, scale in zip(OUTPUTS, OUTPUT_SCALES)]
                    for row in rows], dtype=np.float32)
    split = int(len(rows) * 0.7)
    design = np.column_stack((x, np.ones(len(x), dtype=np.float32)))
    regularizer = np.diag([0.001, 0.001, 0.0])
    coefficients = np.linalg.solve(design[:split].T @ design[:split] + regularizer,
                                   design[:split].T @ y[:split]).T.astype(np.float32)
    predicted = np.maximum(0, design[split:] @ coefficients.T)
    rmse = np.sqrt(np.mean((predicted - y[split:]) ** 2, axis=0)) * OUTPUT_SCALES
    median_rmse = np.sqrt(np.mean((np.median(y[:split], axis=0) - y[split:]) ** 2, axis=0)) * OUTPUT_SCALES
    weights = coefficients[:, :2].reshape(len(OUTPUTS), len(INPUTS), 1, 1)
    graph = helper.make_graph(
        [helper.make_node('Conv', ['telemetry', 'weights', 'bias'], ['linear'], kernel_shape=[1, 1]),
         helper.make_node('Relu', ['linear'], ['prediction'])],
        'server_health',
        [helper.make_tensor_value_info('telemetry', TensorProto.FLOAT, [1, len(INPUTS), SIDE, SIDE])],
        [helper.make_tensor_value_info('prediction', TensorProto.FLOAT, [1, len(OUTPUTS), SIDE, SIDE])],
        [numpy_helper.from_array(weights.copy(), 'weights'),
         numpy_helper.from_array(coefficients[:, 2].copy(), 'bias')])
    model = helper.make_model(graph, opset_imports=[helper.make_opsetid('', 13)])
    model.ir_version = 8
    onnx.checker.check_model(model)
    onnx.save(model, output / 'server-health.onnx')
    calibration_indices = np.linspace(0, len(x) - 1, 64, dtype=int)
    # Include channel extrema within the SDK's 64-sample calibration budget.
    for channel in range(len(INPUTS)):
        calibration_indices[2 * channel] = np.argmin(x[:, channel])
        calibration_indices[2 * channel + 1] = np.argmax(x[:, channel])
    calibration = np.broadcast_to(x[calibration_indices, None, None, :],
                                 (64, SIDE, SIDE, len(INPUTS))).copy()
    np.save(output / 'calibration.npy', calibration, allow_pickle=False)
    manifest = {
        'version': 1, 'architecture': architecture, 'side': SIDE,
        'inputs': list(INPUTS), 'input_scales': list(INPUT_SCALES),
        'outputs': list(OUTPUTS), 'output_scales': list(OUTPUT_SCALES),
        'coefficients': coefficients.tolist(),
        'input_range': [[min(row[key] for row in rows), max(row[key] for row in rows)] for key in INPUTS],
        'residual_scale': [max(METRICS[key][2], float(error)) for key, error in zip(OUTPUTS, rmse)],
        'validation_rmse': dict(zip(OUTPUTS, map(float, rmse))),
        'median_baseline_rmse': dict(zip(OUTPUTS, map(float, median_rmse))),
        'training_samples': split, 'validation_samples': len(rows) - split,
        'first_sample_at': rows[0]['at'], 'last_sample_at': rows[-1]['at'],
        'trained_at': time.time(), 'hef_sha256': None,
        'onnx_sha256': hashlib.sha256((output / 'server-health.onnx').read_bytes()).hexdigest(),
    }
    (output / 'manifest.json').write_text(json.dumps(manifest, indent=2, allow_nan=False) + '\n')
    return manifest


def compile_model(directory):
    import numpy as np
    from hailo_sdk_client import ClientRunner

    manifest_path = directory / 'manifest.json'
    manifest = json.loads(manifest_path.read_text())
    if manifest.get('architecture') not in ('hailo8', 'hailo8l'):
        raise ValueError('Choose hailo8 or hailo8l at training time')
    model = directory / 'server-health.onnx'
    if hashlib.sha256(model.read_bytes()).hexdigest() != manifest['onnx_sha256']:
        raise ValueError('ONNX checksum mismatch; retrain rather than mixing model versions')
    calibration = np.load(directory / 'calibration.npy', allow_pickle=False)
    if (calibration.shape != (64, SIDE, SIDE, len(INPUTS))
            or not np.isfinite(calibration).all()):
        raise ValueError('Expected 64 finite NHWC calibration samples; regenerate the model bundle')
    bounds = np.asarray(manifest['input_range']) / np.asarray(INPUT_SCALES)[:, None]
    if (not np.allclose(calibration.min(axis=(0, 1, 2)), bounds[:, 0], rtol=1e-6, atol=1e-8)
            or not np.allclose(calibration.max(axis=(0, 1, 2)), bounds[:, 1], rtol=1e-6, atol=1e-8)):
        raise ValueError('Calibration does not cover the model input range; regenerate the model bundle')
    runner = ClientRunner(hw_arch=manifest['architecture'])
    runner.translate_onnx_model(str(model), 'server_health',
                                net_input_shapes={'telemetry': [1, len(INPUTS), SIDE, SIDE]})
    runner.optimize(calibration)
    hef = runner.compile()
    (directory / 'server-health.hef').write_bytes(hef)
    manifest['hef_sha256'] = hashlib.sha256(hef).hexdigest()
    manifest['compiled_at'] = time.time()
    manifest_path.write_text(json.dumps(manifest, indent=2, allow_nan=False) + '\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    training = commands.add_parser('train')
    training.add_argument('--features', type=Path, required=True)
    training.add_argument('--output', type=Path, required=True)
    training.add_argument('--architecture', choices=('hailo8', 'hailo8l'), required=True)
    compilation = commands.add_parser('compile')
    compilation.add_argument('--model-dir', type=Path, required=True)
    args = parser.parse_args()
    if args.command == 'train':
        manifest = train(args.features, args.output, args.architecture)
        print(json.dumps({'training_samples': manifest['training_samples'],
                          'validation_rmse': manifest['validation_rmse'],
                          'median_baseline_rmse': manifest['median_baseline_rmse']}, indent=2))
    else:
        compile_model(args.model_dir)
        print('Compiled HEF and checksum-bound manifest; hardware validation still required.')


if __name__ == '__main__':
    main()
