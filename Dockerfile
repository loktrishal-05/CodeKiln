FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        bash \
        ca-certificates \
        docker-buildx \
        docker-cli \
        git \
        sqlite3 \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 codekiln \
    && useradd --uid 10001 --gid 10001 --create-home --shell /bin/bash codekiln \
    && mkdir -p /opt/codekiln /work \
    && chown codekiln:codekiln /work

WORKDIR /opt/codekiln
COPY pyproject.toml README.md ./
COPY src ./src
COPY scripts ./scripts

RUN chmod +x /opt/codekiln/scripts/demo_run.sh \
    && pip install --no-cache-dir "harbor==0.25.*" .

WORKDIR /work
USER codekiln

ENTRYPOINT ["codekiln"]
CMD ["--help"]

