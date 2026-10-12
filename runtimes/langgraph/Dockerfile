# syntax=docker/dockerfile:1@sha256:4edf897a3ffa55b89f906fc8cc78afdb3f1834cc9c7083565e611a8a7d5fe99e
#
# Runtime adapter used by the Go converter as the base for a built LangGraph agent.
# Keep Python 3.12 and a shell for Orka composition and npm/PyPI stdio MCP
# launchers. The free Chainguard Python tags track a moving Python minor.
FROM cgr.dev/chainguard/wolfi-base@sha256:1c451d46a0d5c4e9f2b38e0e8d3e299564a1aa95c21973efcc1980a9d1d2e73e AS runtime-system

# Both installation and execution use the same interpreter/library closure.
# Wolfi's versioned uv package supplies uv and uvx without a moving image tag.
RUN apk add --no-cache \
        python-3.12-base=3.12.15-r3 \
        nodejs-24=24.21.0-r4 \
        npm-12=12.2.0-r2 \
        uv=0.13.0-r0 \
        ca-certificates=20260909-r2 \
    && addgroup -g 1000 agentkit \
    && adduser -D -u 1000 -G agentkit -s /sbin/nologin agentkit \
    && mkdir -p /agent /usr/local/bin \
    && chown -R 1000:1000 /agent \
    && ln -s /usr/bin/python3.12 /usr/local/bin/python \
    && ln -s /usr/bin/python3.12 /usr/local/bin/python3

FROM runtime-system AS installer
RUN apk add --no-cache py3.12-pip=26.2.1-r4
WORKDIR /src
COPY runtimes/common/pyproject.toml runtimes/common/README.md ./common/
COPY runtimes/common/agentkit_serve_common ./common/agentkit_serve_common
COPY runtimes/langgraph/pyproject.toml runtimes/langgraph/README.md ./langgraph/
COPY runtimes/langgraph/agentkit_serve ./langgraph/agentkit_serve

# Retain the LangChain/LangGraph/OpenAI/MCP dependency boundary; never install
# Azure/Foundry packages or another agent framework in this generic runtime.
RUN python3.12 -m venv /opt/agentkit \
    && /opt/agentkit/bin/pip install --no-cache-dir --upgrade pip setuptools==84.0.0 \
    && /opt/agentkit/bin/pip install --no-cache-dir ./common \
    && /opt/agentkit/bin/pip install --no-cache-dir ./langgraph \
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
