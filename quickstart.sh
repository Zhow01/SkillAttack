#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MODE="${1:-smoke}"
VENV_DIR="${ROOT_DIR}/.venv"

DEFAULT_AIG_PORT="18088"
DEFAULT_AIG_CONTAINER="skillattack-aig-webserver"
DEFAULT_AIG_SERVER_IMAGE="docker.1ms.run/zhuquelab/aig-server:latest"
DEFAULT_AIG_AGENT_IMAGE="docker.1ms.run/zhuquelab/aig-agent:latest"
DEFAULT_OPENCLAW_CONTAINER="skillrt-openclaw-host"

AIG_CONTAINER=""
AIG_PORT=""
AIG_SERVER_IMAGE=""
AIG_AGENT_IMAGE=""
AIG_STARTUP_TIMEOUT_SEC=""
AIG_STARTUP_INTERVAL_SEC=""
OPENCLAW_CONTAINER=""
DOCKER_PULL_RETRIES=""
DOCKER_PULL_RETRY_DELAY_SEC=""

log() {
  printf '[quickstart] %s\n' "$*"
}

die() {
  printf '[quickstart] ERROR: %s\n' "$*" >&2
  exit 1
}

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || die "Missing required command: $1"
}

coerce_positive_int() {
  local value="$1"
  local fallback="$2"

  if [[ "${value}" =~ ^[0-9]+$ ]] && (( value > 0 )); then
    printf '%s\n' "${value}"
    return
  fi

  printf '%s\n' "${fallback}"
}

refresh_runtime_config() {
  AIG_CONTAINER="${AIG_CONTAINER:-${DEFAULT_AIG_CONTAINER}}"
  AIG_PORT="${AIG_PORT:-${DEFAULT_AIG_PORT}}"
  AIG_SERVER_IMAGE="${AIG_SERVER_IMAGE:-${DEFAULT_AIG_SERVER_IMAGE}}"
  AIG_AGENT_IMAGE="${AIG_AGENT_IMAGE:-${DEFAULT_AIG_AGENT_IMAGE}}"
  AIG_STARTUP_TIMEOUT_SEC="$(coerce_positive_int "${AIG_STARTUP_TIMEOUT_SEC:-60}" 60)"
  AIG_STARTUP_INTERVAL_SEC="$(coerce_positive_int "${AIG_STARTUP_INTERVAL_SEC:-2}" 2)"
  OPENCLAW_CONTAINER="${OPENCLAW_CONTAINER:-${DEFAULT_OPENCLAW_CONTAINER}}"
  DOCKER_PULL_RETRIES="$(coerce_positive_int "${DOCKER_PULL_RETRIES:-3}" 3)"
  DOCKER_PULL_RETRY_DELAY_SEC="$(coerce_positive_int "${DOCKER_PULL_RETRY_DELAY_SEC:-5}" 5)"
  export AIG_SERVER_IMAGE AIG_AGENT_IMAGE
}

validate_quickstart_config() {
  if [[ "${AIG_PORT}" != "${DEFAULT_AIG_PORT}" ]]; then
    die "AIG_PORT=${AIG_PORT} is not supported by quickstart. The analyzer config is fixed to http://localhost:${DEFAULT_AIG_PORT} in configs/stages.yaml."
  fi
}

ensure_runtime_dirs() {
  mkdir -p \
    "${ROOT_DIR}/result/aig_cache" \
    "${ROOT_DIR}/result/log" \
    "${ROOT_DIR}/result/runshistory" \
    "${ROOT_DIR}/result/runs_organize/main/_experiment_results" \
    "${ROOT_DIR}/result/runs_organize/comparison/_experiment_results" \
    "${ROOT_DIR}/.runtime/aig_local/data" \
    "${ROOT_DIR}/.runtime/aig_local/db" \
    "${ROOT_DIR}/.runtime/aig_local/logs" \
    "${ROOT_DIR}/.runtime/aig_local/uploads"
}

ensure_env_file() {
  if [[ -f "${ROOT_DIR}/.env" ]]; then
    return
  fi

  cp "${ROOT_DIR}/.env.example" "${ROOT_DIR}/.env"
  die "Created ${ROOT_DIR}/.env. Fill in QWEN_API_KEY and rerun."
}

requirements_fingerprint() {
  python3 - "${ROOT_DIR}/requirements.txt" <<'PY'
from hashlib import sha256
from pathlib import Path
import sys

print(sha256(Path(sys.argv[1]).read_bytes()).hexdigest())
PY
}

sync_python_dependencies() {
  local stamp_path="${VENV_DIR}/.requirements.sha256"
  local expected_hash
  local current_hash=""

  expected_hash="$(requirements_fingerprint)"
  if [[ -f "${stamp_path}" ]]; then
    current_hash="$(tr -d '[:space:]' < "${stamp_path}")"
  fi

  if [[ "${expected_hash}" == "${current_hash}" ]]; then
    log "Python dependencies already up to date"
    return
  fi

  log "Installing Python dependencies"
  python -m pip install --upgrade pip
  python -m pip install -r "${ROOT_DIR}/requirements.txt"
  printf '%s\n' "${expected_hash}" > "${stamp_path}"
}

setup_venv() {
  if [[ ! -x "${VENV_DIR}/bin/python" ]]; then
    log "Creating virtual environment at ${VENV_DIR}"
    python3 -m venv "${VENV_DIR}"
  fi

  # shellcheck disable=SC1091
  source "${VENV_DIR}/bin/activate"

  if ! python -m pip --version >/dev/null 2>&1; then
    log "Bootstrapping pip in virtual environment"
    python -m ensurepip --upgrade
  fi

  sync_python_dependencies
}

load_env() {
  # shellcheck disable=SC1091
  set -a
  source "${ROOT_DIR}/.env"
  set +a

  [[ -n "${QWEN_API_KEY:-}" ]] || die "QWEN_API_KEY is missing in ${ROOT_DIR}/.env"
  [[ -n "${QWEN_BASE_URL:-}" ]] || die "QWEN_BASE_URL is missing in ${ROOT_DIR}/.env"
}

verify_model_access() {
  log "Verifying model endpoint"
  python3 - <<'PY'
import os
import sys
import urllib.request

base_url = (os.environ.get("QWEN_BASE_URL") or "").strip().rstrip("/")
api_key = (os.environ.get("QWEN_API_KEY") or "").strip()
url = f"{base_url}/models"
req = urllib.request.Request(url, headers={"Authorization": f"Bearer {api_key}"})

try:
    with urllib.request.urlopen(req, timeout=30) as resp:
        if resp.status != 200:
            raise RuntimeError(f"Unexpected status: {resp.status}")
except Exception as exc:
    raise SystemExit(f"Model endpoint check failed for {url}: {exc}")
PY
}

image_exists_locally() {
  docker image inspect "$1" >/dev/null 2>&1
}

image_ref_id() {
  docker image inspect "$1" --format '{{.Id}}' 2>/dev/null || true
}

container_uses_image() {
  local container_name="$1"
  local desired_image="$2"
  local container_image_ref=""
  local container_image_id=""
  local desired_image_id=""

  container_image_ref="$(docker inspect -f '{{.Config.Image}}' "${container_name}" 2>/dev/null || true)"
  if [[ -z "${container_image_ref}" ]]; then
    return 1
  fi

  if [[ "${container_image_ref}" == "${desired_image}" ]]; then
    return 0
  fi

  container_image_id="$(image_ref_id "${container_image_ref}")"
  desired_image_id="$(image_ref_id "${desired_image}")"

  [[ -n "${container_image_id}" ]] && [[ "${container_image_id}" == "${desired_image_id}" ]]
}

pull_docker_image_with_retries() {
  local image="$1"
  local attempt=1
  local max_attempts="${DOCKER_PULL_RETRIES}"
  local retry_delay="${DOCKER_PULL_RETRY_DELAY_SEC}"
  local exit_code=1

  while (( attempt <= max_attempts )); do
    log "Pulling Docker image (${attempt}/${max_attempts}): ${image}"
    if docker pull "${image}"; then
      return 0
    fi

    exit_code=$?
    if image_exists_locally "${image}"; then
      log "Docker image ${image} is available locally after a non-zero pull exit; continuing"
      return 0
    fi

    if (( attempt < max_attempts )); then
      log "Pull failed for ${image}; retrying in ${retry_delay}s"
      sleep "${retry_delay}"
    fi
    attempt=$((attempt + 1))
  done

  return "${exit_code}"
}

ensure_docker_image() {
  local image="$1"
  local label="$2"
  local env_name="$3"

  if image_exists_locally "${image}"; then
    log "${label} image already present: ${image}"
    return
  fi

  if pull_docker_image_with_retries "${image}"; then
    return
  fi

  die "Unable to prepare ${label} image ${image}. If the default registry or mirror is slow, set ${env_name} in ${ROOT_DIR}/.env to a reachable image address and rerun."
}

ensure_aig_images() {
  ensure_docker_image "${AIG_SERVER_IMAGE}" "AIG server" "AIG_SERVER_IMAGE"
  ensure_docker_image "${AIG_AGENT_IMAGE}" "AIG agent" "AIG_AGENT_IMAGE"
}

probe_aig_mounts() {
  docker exec "${AIG_CONTAINER}" sh -lc \
    'touch /app/uploads/.quickstart_probe && echo ok >/app/logs/.quickstart_probe && rm -f /app/uploads/.quickstart_probe /app/logs/.quickstart_probe'
}

wait_for_aig_webserver() {
  local elapsed=0

  while (( elapsed < AIG_STARTUP_TIMEOUT_SEC )); do
    if curl -fsS "http://localhost:${AIG_PORT}" >/dev/null 2>&1 && probe_aig_mounts >/dev/null 2>&1; then
      return 0
    fi
    sleep "${AIG_STARTUP_INTERVAL_SEC}"
    elapsed=$((elapsed + AIG_STARTUP_INTERVAL_SEC))
  done

  return 1
}

create_aig_webserver() {
  log "Starting local AIG webserver on port ${AIG_PORT}"
  docker rm -f "${AIG_CONTAINER}" >/dev/null 2>&1 || true
  docker run -d \
    --name "${AIG_CONTAINER}" \
    --restart unless-stopped \
    -p "${AIG_PORT}:8088" \
    -e DB_PATH=/app/db/tasks.db \
    -e TZ=Asia/Shanghai \
    -e APP_ENV=production \
    -e UPLOAD_DIR=/app/uploads \
    -v "${ROOT_DIR}/.runtime/aig_local/data:/app/data" \
    -v "${ROOT_DIR}/.runtime/aig_local/db:/app/db" \
    -v "${ROOT_DIR}/.runtime/aig_local/logs:/app/logs" \
    -v "${ROOT_DIR}/.runtime/aig_local/uploads:/app/uploads" \
    "${AIG_SERVER_IMAGE}" \
    /app/start.sh >/dev/null
}

ensure_aig_webserver() {
  local current_image=""

  if ! docker ps -a --format '{{.Names}}' | grep -qx "${AIG_CONTAINER}"; then
    create_aig_webserver
  else
    current_image="$(docker inspect -f '{{.Config.Image}}' "${AIG_CONTAINER}" 2>/dev/null || true)"
    if ! container_uses_image "${AIG_CONTAINER}" "${AIG_SERVER_IMAGE}"; then
      log "AIG webserver image changed (${current_image:-unknown} -> ${AIG_SERVER_IMAGE}); recreating container"
      create_aig_webserver
    else
      docker start "${AIG_CONTAINER}" >/dev/null 2>&1 || true
    fi
  fi

  if wait_for_aig_webserver; then
    return
  fi

  log "AIG webserver failed readiness checks; recreating container"
  create_aig_webserver
  if ! wait_for_aig_webserver; then
    die "AIG webserver failed to become ready on port ${AIG_PORT}"
  fi
}

ensure_openclaw_host() {
  if docker ps --format '{{.Names}}' | grep -qx "${OPENCLAW_CONTAINER}"; then
    return
  fi

  log "Preparing OpenClaw host container"
  bash "${ROOT_DIR}/scripts/rebuild_openclaw_clean_host.sh"
  docker ps --format '{{.Names}}' | grep -qx "${OPENCLAW_CONTAINER}" || die "OpenClaw host container failed to start"
}

run_smoke_experiment() {
  log "Running 1-skill smoke experiment"
  python - <<'PY'
import os
import sys
import tempfile
from pathlib import Path

root = Path(os.environ["SKILLATTACK_ROOT"]).resolve()
os.chdir(root)
sys.path.insert(0, str(root))

from core.config_loader import ConfigLoader
from experiments import main_run

skill = root / "data/skillinject/obvious/python-code_default_password"
if not skill.exists():
    raise SystemExit(f"Smoke-test skill not found: {skill}")

with tempfile.TemporaryDirectory(prefix="skillattack_quickstart_") as tmpdir:
    tmp_root = Path(tmpdir)
    (tmp_root / skill.name).symlink_to(skill, target_is_directory=True)

    ConfigLoader._instance = None
    ConfigLoader._config = {}
    ConfigLoader._runtime_run_root = None

    cfg = ConfigLoader()
    main_cfg = cfg.main_experiment
    project_cfg = main_cfg.setdefault("project", {})
    input_cfg = main_cfg.setdefault("input", {})

    project_cfg["max_iterations"] = 1
    project_cfg["surface_parallelism"] = 1
    project_cfg["max_skills"] = 1
    input_cfg["raw_skill_root"] = str(tmp_root)
    input_cfg["skill_summary"] = ""

    rc = main_run.main([])
    raise SystemExit(rc)
PY
}

run_main_experiment() {
  local valid_count
  valid_count="$(python - <<'PY'
import os
from pathlib import Path

root = Path(os.environ["SKILLATTACK_ROOT"]) / "data" / "hot100skills"
if not root.exists():
    print(0)
else:
    count = 0
    for entry in root.iterdir():
        if entry.is_dir() and (entry / "SKILL.md").exists():
            count += 1
    print(count)
PY
)"
  [[ "${valid_count}" != "0" ]] || die "data/hot100skills has no valid skill directories. Use smoke mode or repair the dataset first."
  python "${ROOT_DIR}/main.py" main
}

run_compare_experiment() {
  python "${ROOT_DIR}/main.py" compare --max-cases 1
}

main() {
  require_cmd python3
  require_cmd docker
  require_cmd curl
  docker info >/dev/null 2>&1 || die "Docker daemon is not available"

  cd "${ROOT_DIR}"
  export SKILLATTACK_ROOT="${ROOT_DIR}"

  ensure_env_file
  load_env
  refresh_runtime_config
  validate_quickstart_config
  ensure_runtime_dirs
  verify_model_access
  ensure_aig_images
  setup_venv
  ensure_aig_webserver
  ensure_openclaw_host

  case "${MODE}" in
    smoke)
      run_smoke_experiment
      ;;
    main)
      run_main_experiment
      ;;
    compare)
      run_compare_experiment
      ;;
    *)
      die "Usage: ./quickstart.sh [smoke|main|compare]"
      ;;
  esac

  log "Done"
}

main "$@"
