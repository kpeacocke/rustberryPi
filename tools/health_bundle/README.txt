PRIVATE RUSTBERRYPI HAILO TRAINING KIT

Extract this ZIP into your WSL Linux home directory, not /mnt/c.
Run commands from the extracted directory. Python 3 (stdlib only) and a working
Docker installation are required. Windows PowerShell and ARM hosts are not supported.
The Linux/WSL host must be x86-64 with AVX exposed to Linux. No always-on worker,
cloud service or repository checkout is required.

Check the existing environment:
  python3 build.py --check-only

Train and compile using your existing image:
  python3 build.py

If the image is absent, supply your licensed compiler wheel:
  python3 build.py --wheel /path/to/hailo_dataflow_compiler-3.34.0-py3-none-linux_x86_64.whl

The Docker image provides Ubuntu 22.04, a Python 3.10 virtual environment and
the compiler's own numpy/onnx dependencies. No host Python packages are installed.
Do not install requirements-health-model.txt over the SDK dependencies.
Image creation needs internet access, disk space and your separately obtained
licensed wheel. The wheel is deliberately NOT included or downloaded for you.
Compiler runs use CPU, no privileged mode, GPU, HAT or Docker socket mount.
Container root is needed by SDK logging; only the output directory is writable
and ownership is restored on exit. Do not interrupt ownership restoration.

Each run creates output/<UTC timestamp>/ with logs, training artifacts and
candidate.zip (HEF + matching manifest). Failed builds do not create a candidate
unless compilation and checksum verification succeeded. Never mix manifests.
Keep this ZIP, logs, source dataset and outputs private. Disk usage is not pruned.

This reproduces the existing two-input advisory regression model, not a failure
predictor. The trainer selects the latest 20,000 eligible samples and needs at
least 1,200 complete healthy samples spanning an hour. It can still select mostly
idle data: inspect input_range, sample counts, validation_rmse and
median_baseline_rmse. Build completion is NOT an accuracy certification.
Calibration spans the selected input domain, but real quantized HAT parity must
still be measured on the Pi, including boundaries and intermediate workloads.
Do not loosen the 0.05 normalized parity guard or deploy automatically.
No deployment, remote access, game restart or telemetry restart is performed.
After validation, extract candidate.zip into your private AWX/controller model
directory and use the normal Telemetry deployment with rust_telemetry_hat_model_dir
pointing at that directory. Keep server-health.hef and manifest.json together.
Follow telemetry/README.md in the repository for deployment and real inference checks.

The exporter strips identities and arbitrary fields; it includes numeric host
metrics, counts, timestamps and health flags only. Incomplete final archive rows
are skipped. SHA256.json detects accidental corruption, not malicious tampering:
use a trusted transfer channel and inspect scripts before execution.
