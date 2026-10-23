FROM ubuntu:22.04
ENV DEBIAN_FRONTEND=noninteractive
ENV PATH="/opt/hailo-venv/bin:${PATH}"
RUN apt-get update && apt-get install -y --no-install-recommends python3.10 python3.10-venv python3.10-dev build-essential graphviz libgraphviz-dev python3-tk libgl1 libglib2.0-0 libgomp1 ca-certificates lsb-release bsdextrautils && rm -rf /var/lib/apt/lists/*
RUN python3.10 -m venv /opt/hailo-venv
COPY compiler.whl /tmp/compiler.whl
RUN mv /tmp/compiler.whl /tmp/hailo_dataflow_compiler-3.34.0-py3-none-linux_x86_64.whl && python -m pip install /tmp/hailo_dataflow_compiler-3.34.0-py3-none-linux_x86_64.whl && python -m pip check
WORKDIR /work
