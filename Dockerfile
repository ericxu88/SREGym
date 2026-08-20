# SREGym in a container: the full harness (run / sweep / verify) plus the verifiers
# taskset, isolated from the host. Episodes run as an unprivileged user; worlds live
# in the container's ephemeral filesystem unless you mount a volume.
#
#   docker build -t sregym .
#   docker run --rm sregym run --seed 42 --agent scripted --quiet          # offline demo
#   docker run --rm -e ANTHROPIC_API_KEY sregym run --seed 7 --agent anthropic --model claude-sonnet-5
#   docker run --rm -e ANTHROPIC_API_KEY -v "$PWD/sweeps:/work/sweeps" sregym \
#       sweep --seeds 1-20 --out sweeps/run1 --model claude-sonnet-5
FROM python:3.12-slim

RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        git sqlite3 curl procps \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --uid 10001 oncall
WORKDIR /opt/sregym

COPY pyproject.toml README.md ./
COPY sregym ./sregym
COPY environments ./environments
RUN pip install --no-cache-dir ".[verifiers]" \
    && pip install --no-cache-dir --no-deps ./environments/sregym_env

# episodes write worlds/runs under the workdir; keep it owned by the runtime user
USER oncall
WORKDIR /work
ENV HOME=/home/oncall

ENTRYPOINT ["sregym"]
CMD ["--help"]
