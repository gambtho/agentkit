#!/usr/bin/env bash

set +x
set -Eeuo pipefail

log() { printf '==> %s\n' "$*" >&2; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }
require_cmd() { command -v "$1" >/dev/null 2>&1 || die "missing required command: $1"; }

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=scripts/aikit-e2e-common.sh
source "$repo_root/scripts/aikit-e2e-common.sh"
work_dir="$(mktemp -d "${RUNNER_TEMP:-${TMPDIR:-/tmp}}/agentkit-live-aikit.XXXXXX")"
run_id="${work_dir##*/}"
aikit_container_name="$run_id-aikit"
aikit_host_port="${AIKIT_HOST_PORT:-18089}"
agent_container_name="$run_id-agent"
network_name="$run_id-network"
agent_host_port="${AGENTKIT_LIVE_HOST_PORT:-18080}"
agent_auth_token="${AGENTKIT_AUTH_TOKEN:-agentkit-live-ci-token}"
tag="${TAG:-ci-live}"
platform="${PLATFORM:-}"
builder="${BUILDER:-}"
containers=()
network_created=false

default_platform() {
  local arch
  arch="$(docker info --format '{{.Architecture}}' 2>/dev/null || uname -m)"
  case "$arch" in
    aarch64|arm64) printf 'linux/arm64' ;;
    x86_64|amd64) printf 'linux/amd64' ;;
    *) die "unsupported Docker architecture: $arch" ;;
  esac
}

redact() {
  local text
  text="$(cat)"
  text="${text//${agent_auth_token}/[REDACTED]}"
  printf '%s' "$text" | sed -E 's/(Authorization: Bearer )[[:graph:]]+/\1[REDACTED]/g'
}

cleanup() {
  local name
  for name in ${containers[@]+"${containers[@]}"}; do
    docker rm -fv "$name" >/dev/null 2>&1 || true
  done
  if [[ "$network_created" == true ]]; then
    docker network rm "$network_name" >/dev/null 2>&1 || true
  fi
  rm -rf "$work_dir"
}

on_exit() {
  local status="$?"
  trap - EXIT
  if [[ "$status" -ne 0 ]]; then
    {
      echo '=== AIKit logs ==='
      docker logs "$aikit_container_name" 2>&1 || true
      echo '=== agent logs ==='
      docker logs "$agent_container_name" 2>&1 || true
    } | redact >&2
    log 'Live AIKit-backed AgentKit E2E failed'
  fi
  cleanup
  exit "$status"
}
trap on_exit EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

wait_for_http() {
  local name="$1" url="$2" deadline=$((SECONDS + $3))
  while (( SECONDS < deadline )); do
    [[ "$(docker inspect -f '{{.State.Running}}' "$name" 2>/dev/null)" == true ]] ||
      die "$name exited before readiness"
    if curl -fsS --connect-timeout 2 --max-time 5 "$url" >/dev/null 2>&1; then
      return 0
    fi
    sleep 2
  done
  die "$name did not become ready at $url"
}

main() {
  for command in curl docker go jq make; do require_cmd "$command"; done
  [[ -n "$platform" ]] || platform="$(default_platform)"
  cd "$repo_root"

  log "Creating private Docker network $network_name"
  docker network create "$network_name" >/dev/null
  network_created=true
  containers+=("$aikit_container_name")
  log "Starting AIKit ($aikit_image)"
  start_aikit "$repo_root/test/aikit-e2e/model.yaml" -d --name "$aikit_container_name" --platform "$platform" \
    --network "$network_name" --network-alias aikit \
    -p "127.0.0.1:$aikit_host_port:8080"
  wait_for_http "$aikit_container_name" "http://127.0.0.1:$aikit_host_port/readyz" 300
  curl -fsS --max-time 15 "http://127.0.0.1:$aikit_host_port/v1/models" >"$work_dir/models.json"
  jq -e --arg model "$aikit_model" '.data | any(.id == $model)' "$work_dir/models.json" >/dev/null
  log 'Warming the local model before the agent smoke'
  warm_aikit "http://127.0.0.1:$aikit_host_port" "$work_dir/warmup.json"

  log "Building AgentKit frontend and MAF adapter for $platform"
  buildx_args=()
  if [[ -n "$builder" ]]; then
    docker buildx inspect "$builder" --bootstrap
    export BUILDX_BUILDER="$builder"
    buildx_args=(--builder "$builder")
  else
    docker buildx inspect --bootstrap
  fi
  make build-agentkit TAG="$tag"
  make build-serve-maf TAG="$tag" PLATFORM="$platform"
  docker buildx build ${buildx_args[@]+"${buildx_args[@]}"} . -f test/agentkitfile-maf-live.yaml \
    --build-arg BUILDKIT_SYNTAX="agentkit:$tag" \
    --build-arg adapter="agentkit-serve-maf:$tag" \
    --platform "$platform" -t "maf-live-agent:$tag" --load --provenance=false

  log 'Starting live MAF agent'
  containers+=("$agent_container_name")
  docker run -d --name "$agent_container_name" --platform "$platform" \
    --network "$network_name" -p "127.0.0.1:$agent_host_port:8080" \
    -e AGENTKIT_BIND=0.0.0.0 -e AGENTKIT_AUTH_TOKEN="$agent_auth_token" \
    -e MODEL_API_KEY=not-needed "maf-live-agent:$tag" >/dev/null
  wait_for_http "$agent_container_name" "http://127.0.0.1:$agent_host_port/healthz" 180

  log 'Calling live MAF agent /v1/chat/completions'
  curl -fsS --connect-timeout 5 --max-time 120 \
    -H "Authorization: Bearer $agent_auth_token" -H 'Content-Type: application/json' \
    --data '{"model":"qwen-3.5-2b","stream":false,"messages":[{"role":"user","content":"Reply with exactly one short sentence that includes the sentinel token DONE42."}]}' \
    "http://127.0.0.1:$agent_host_port/v1/chat/completions" >"$work_dir/agent-response.json"
  jq '{model, content: .choices[0].message.content}' "$work_dir/agent-response.json" | redact >&2
  jq -e '.choices[0].message.content | type == "string" and contains("DONE42")' "$work_dir/agent-response.json" >/dev/null
  log 'Live AIKit-backed AgentKit E2E passed'
}

main "$@"
