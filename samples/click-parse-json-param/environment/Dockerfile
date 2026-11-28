FROM python:3.12-slim

# git: base snapshot + grading restore; less: click's pager tests need a real pager on PATH
RUN apt-get update \
    && apt-get install -y --no-install-recommends git less ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
# Clone at the pinned commit, then re-init as ONE orphan commit so the agent cannot read
# upstream history. Editable install: tests must import the code the agent edits in /app/src.
RUN git clone --quiet https://github.com/pallets/click . \
    && git checkout --quiet cf19179e8e2fd54c601378cec9e0d21bb12b6fb1 \
    && rm -rf .git \
    && git init --quiet \
    && git add -A \
    && git -c user.name=codekiln -c user.email=codekiln@localhost commit --quiet -m "base cf19179e8e2fd54c601378cec9e0d21bb12b6fb1" \
    && pip install --no-cache-dir -e . pytest==8.4.1