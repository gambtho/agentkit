# syntax=docker/dockerfile:1@sha256:4edf897a3ffa55b89f906fc8cc78afdb3f1834cc9c7083565e611a8a7d5fe99e
#
# Runtime adapter used by the Go converter as the base for a built MAF agent.
# Keep Python 3.12 and a shell for Orka composition and npm/PyPI stdio MCP
# launchers. The free Chainguard Python tags track a moving Python minor.
FROM cgr.dev/chainguard/wolfi-base@sha256:1c451d46a0d5c4e9f2b38e0e8d3e299564a1aa95c21973efcc1980a9d1d2e73e AS runtime-system

# Both installation and execution use the same interpreter/library closure.
# Keep language package families stable while allowing Wolfi patch updates.
# The uv package supplies both uv and uvx.
RUN apk add --no-cache \
        python-3.12-base \
        nodejs-24 \
        npm-12 \
        uv \
        ca-certificates \
    && addgroup -g 1000 agentkit \
    && adduser -D -u 1000 -G agentkit -s /sbin/nologin agentkit \
    && mkdir -p /agent/skills /usr/local/bin \
    && chown -R 1000:1000 /agent \
    && ln -s /usr/bin/python3.12 /usr/local/bin/python \
    && ln -s /usr/bin/python3.12 /usr/local/bin/python3

FROM runtime-system AS installer
RUN apk add --no-cache py3.12-pip
WORKDIR /src
COPY runtimes/common/pyproject.toml runtimes/common/README.md ./common/
COPY runtimes/common/agentkit_serve_common ./common/agentkit_serve_common
COPY runtimes/microsoft-agent-framework/pyproject.toml runtimes/microsoft-agent-framework/README.md ./microsoft-agent-framework/
COPY runtimes/microsoft-agent-framework/agentkit_serve ./microsoft-agent-framework/agentkit_serve

# Retain MAF core/OpenAI/MCP and only the existing Foundry identity/memory and
# Azure AI Search providers; do not add unrelated framework integrations.
RUN python3.12 -m venv /opt/agentkit \
    && /opt/agentkit/bin/pip install --no-cache-dir --upgrade pip setuptools==84.0.0 \
    && /opt/agentkit/bin/pip install --no-cache-dir ./common \
    && /opt/agentkit/bin/pip install --no-cache-dir ./microsoft-agent-framework \
    && /opt/agentkit/bin/pip check \
    && /opt/agentkit/bin/agentkit-serve --help >/dev/null

FROM runtime-system AS runtime
# Copy only the venv, not /src or the installer's additional system packages.
COPY --from=installer --chown=1000:1000 /opt/agentkit /opt/agentkit

# Must match pkg/utils/const.go and pkg/agentkit/abi. The converter also sets
# its own entrypoint/environment; the passwd entry supports tool caches when
# HOME is omitted from the generated agent's environment.
ENV AGENTKIT_ROOT=/opt/agentkit \
    PATH=/opt/agentkit/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HOME=/home/agentkit \
    AGENTKIT_BIND=127.0.0.1
EXPOSE 8080
USER 1000
ENTRYPOINT ["/opt/agentkit/bin/agentkit-serve"]
CMD ["--config", "/agent/agent.yaml"]
