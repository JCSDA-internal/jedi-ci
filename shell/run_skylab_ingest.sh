#!/usr/bin/env bash

# Skylab observation ingest experiment runner for containerized CI.
#
# This script replaces the VM-based ubuntu-ci-ingest-x86_64.yaml workflow
# with a fully containerized approach. It is designed to be invoked by
# bootstrap_test.sh (which activates spack-stack via
# /opt/spack-environment/activate.sh, sets WORKDIR, and configures git
# credentials).
#
# Execution phases:
#   1) Environment setup and variable definitions
#   2) Clone and install workflow Python packages
#   3) Initialize MySQL and load R2D2 seed data
#   4) Start R2D2 API server (gunicorn)
#   5) Build jedi-bundle (ecbuild + make)
#   6) Start ecFlow, configure and run ingest experiment
#   7) Poll experiment status
#   8) Cleanup

set -o pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ---------------------------------------------------------------------------
# 1. Environment setup
# ---------------------------------------------------------------------------

# JEDI_COMPILER is required by environment.sh for compiler-specific config.
export JEDI_COMPILER="${JEDI_COMPILER:-gcc}"
source "${SCRIPT_DIR}/environment.sh"

# GitHub App credentials for cloning private repos. These are normally
# configured by bootstrap_test.sh, but we validate and set up askPass
# here as well so the script can run standalone.
valid_environment_found="yes"

if [ -z "${GITHUB_APP_PRIVATE_KEY}" ]; then
    echo "Var GITHUB_APP_PRIVATE_KEY must be set (text of the key or a file path)"
    valid_environment_found="no"
fi
if [ -z "${GITHUB_APP_ID}" ]; then
    echo "Var GITHUB_APP_ID must be set (GitHub App ID)"
    valid_environment_found="no"
fi
if [ -z "${GITHUB_INSTALL_ID}" ]; then
    echo "Var GITHUB_INSTALL_ID must be set (GitHub App install ID)"
    valid_environment_found="no"
fi

if [ "${valid_environment_found}" == "no" ]; then
    echo "Missing required GitHub credentials. Cannot clone private repos."
    exit 1
fi

# Ensure the private key is a file (bootstrap_test.sh may have already done this).
if [ ! -f "${GITHUB_APP_PRIVATE_KEY}" ]; then
    key_file=$(mktemp -u)
    echo "${GITHUB_APP_PRIVATE_KEY}" > "${key_file}"
    export GITHUB_APP_PRIVATE_KEY="${key_file}"
fi

# Configure git askPass for GitHub App authentication.
if [ -f "${SCRIPT_DIR}/git_askPass_app_credentials.py" ]; then
    cp "${SCRIPT_DIR}/git_askPass_app_credentials.py" /bin/git_askPass_app_credentials.py
    chmod +x /bin/git_askPass_app_credentials.py
    git config --global core.askPass /bin/git_askPass_app_credentials.py
fi
git config --global --add safe.directory '*'

# Skylab workflow paths
# TODO: these should be set in environment.sh?
# TODO: do we want them in $WORKDIR?
export JEDI_WORKFLOW="${WORKDIR}/jedi-workflow"
export JEDI_BUILD="${BUILD_DIR}"
export JEDI_SRC="${JEDI_BUNDLE_DIR}"
export EWOK_WORKDIR="${WORKDIR}/workdir"
export EWOK_FLOWDIR="${WORKDIR}/ecflow"
export EWOK_STATIC_DATA="${JEDI_WORKFLOW}/static-data/static"

# ecFlow configuration
export ECF_PORT=5907
export ECF_HOST=$(hostname)

# R2D2 client configuration
export R2D2_HOST="localhost"
export R2D2_COMPILER="gnu"
export R2D2_USER="localhost"
export R2D2_API_KEY="localhost"
export R2D2_SERVER_HOST="http://localhost"
export R2D2_SERVER_PORT="8080"
export R2D2_LOG_LEVEL="INFO"
export R2D2_S3_PUBLISH_BUCKET="r2d2-experiments-localhost"

# pyioda Python bindings
PYTHON_VERSION=$(python3 -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}")')
export PYTHONPATH="${BUILD_DIR}/lib/python${PYTHON_VERSION}:${PYTHONPATH}"

mkdir -p "${JEDI_WORKFLOW}" "${EWOK_WORKDIR}" "${EWOK_FLOWDIR}"

cat <<EOF
Skylab Ingest Configuration:
  WORKDIR=${WORKDIR}
  JEDI_BUNDLE_DIR=${JEDI_BUNDLE_DIR}
  BUILD_DIR=${BUILD_DIR}
  JEDI_WORKFLOW=${JEDI_WORKFLOW}
  EWOK_WORKDIR=${EWOK_WORKDIR}
  EWOK_FLOWDIR=${EWOK_FLOWDIR}
  EWOK_STATIC_DATA=${EWOK_STATIC_DATA}
  ECF_PORT=${ECF_PORT}
  R2D2_SERVER_HOST=${R2D2_SERVER_HOST}:${R2D2_SERVER_PORT}
  JEDI_COMPILER=${JEDI_COMPILER}
  BUILD_PARALLELISM=${BUILD_PARALLELISM}
  GITHUB_APP_ID=${GITHUB_APP_ID}
  GITHUB_INSTALL_ID=${GITHUB_INSTALL_ID}
  GITHUB_APP_PRIVATE_KEY_FILE=${GITHUB_APP_PRIVATE_KEY}
EOF

set -x

# ---------------------------------------------------------------------------
# 2. Clone repos and install Python packages
# ---------------------------------------------------------------------------

python3 -m venv "${JEDI_WORKFLOW}/venv"
source "${JEDI_WORKFLOW}/venv/bin/activate"

for repo in r2d2 r2d2-client r2d2-data ewok simobs static-data skylab; do
    echo "Cloning ${repo}"
    git clone -b develop "https://github.com/jcsda-internal/${repo}" "${JEDI_WORKFLOW}/${repo}"
done

# pip may not be present inside the venv in minimal spack containers
# TODO: I dont think we need this anymore with spack activation ...
#curl -sS https://bootstrap.pypa.io/get-pip.py | python3

# Install in dependency order. The server's setup.py intentionally omits
# the r2d2 library from install_requires, so both must be installed
# explicitly and the r2d2 library must come before r2d2-client (which
# provides an identically-named module that shadows it).
cd "${JEDI_WORKFLOW}/r2d2/server" && python3 -m pip install -e .
cd "${JEDI_WORKFLOW}/r2d2"        && python3 -m pip install -e .
cd "${JEDI_WORKFLOW}/r2d2-client" && python3 -m pip install -e .
cd "${JEDI_WORKFLOW}/ewok"        && python3 -m pip install -e .
cd "${JEDI_WORKFLOW}/simobs"      && python3 -m pip install -e .
pip install gunicorn

# Symlink experiment data so R2D2 data store basedir resolves correctly.
# The SQL dump's data_store.basedir is updated to JEDI_WORKFLOW in step 3,
# so R2D2 expects files at ${JEDI_WORKFLOW}/r2d2-experiments-localhost/.
ln -sf "${JEDI_WORKFLOW}/r2d2-data/r2d2-experiments-localhost" \
       "${JEDI_WORKFLOW}/r2d2-experiments-localhost"

# ---------------------------------------------------------------------------
# 3. Initialize MySQL and load R2D2 seed data
# ---------------------------------------------------------------------------

mysqld --initialize-insecure --user=root --datadir=/var/lib/mysql 2>/dev/null || true
mysqld --user=root --datadir=/var/lib/mysql &
MYSQL_PID=$!
sleep 5

mysql -u root <<'SQL'
CREATE DATABASE IF NOT EXISTS r2d2;
CREATE USER IF NOT EXISTS 'dbuser'@'localhost' IDENTIFIED BY 'local-server-password';
CREATE USER IF NOT EXISTS 'dbuser'@'127.0.0.1' IDENTIFIED BY 'local-server-password';
GRANT ALL PRIVILEGES ON r2d2.* TO 'dbuser'@'localhost';
GRANT ALL PRIVILEGES ON r2d2.* TO 'dbuser'@'127.0.0.1';
FLUSH PRIVILEGES;
SQL

mysql -u root r2d2 < "${JEDI_WORKFLOW}/r2d2-data/r2d2-experiments-localhost.sql"

mysql -u root r2d2 <<SQL
UPDATE data_store
  SET basedir='${JEDI_WORKFLOW}'
  WHERE name='r2d2-experiments-localhost';
SQL

echo "MySQL initialized and R2D2 seed data loaded (PID: ${MYSQL_PID})"

# ---------------------------------------------------------------------------
# 4. Start R2D2 API server
# ---------------------------------------------------------------------------

export MYSQL_USER=dbuser
export MYSQL_PASSWORD=local-server-password
export MYSQL_HOST=127.0.0.1
export MYSQL_PORT=3306
export MYSQL_DATABASE=r2d2

mkdir -p /var/log/gunicorn
# The r2d2 library and r2d2-client both install a module named "r2d2".
# The client is installed last so ewok/create_experiment.py see it, but
# the server needs the library's R2D2Index. Prepend the library source
# to PYTHONPATH for the gunicorn process only.
PYTHONPATH="${JEDI_WORKFLOW}/r2d2/src:${PYTHONPATH}" \
    gunicorn -c python:app.gunicorn app:APP > /var/log/gunicorn/r2d2.log 2>&1 &
R2D2_PID=$!
sleep 3

curl -f http://localhost:8080/health || {
    echo "R2D2 API failed to start. Gunicorn log:"
    cat /var/log/gunicorn/r2d2.log
    kill $MYSQL_PID 2>/dev/null || true
    exit 1
}
echo "R2D2 API server started (PID: ${R2D2_PID})"

# ---------------------------------------------------------------------------
# 5. Build jedi-bundle
# ---------------------------------------------------------------------------

cd "${BUILD_DIR}"

ecbuild \
    -Wno-dev \
    -DBUILD_IODA_CONVERTERS=ON \
    -DBUILD_LARGE_TESTS=OFF \
    -DCMAKE_BUILD_TYPE=RelWithDebInfo \
    "${COMPILER_FLAGS[@]}" "${JEDI_BUNDLE_DIR}"

if [ $? -ne 0 ]; then
    echo "ecbuild configuration failed"
    kill $R2D2_PID $MYSQL_PID 2>/dev/null || true
    exit 1
fi

make -j ${BUILD_PARALLELISM}

if [ $? -ne 0 ]; then
    echo "Compilation failed"
    kill $R2D2_PID $MYSQL_PID 2>/dev/null || true
    exit 1
fi

# ---------------------------------------------------------------------------
# 6. Start ecFlow and run ingest experiment
# ---------------------------------------------------------------------------

ecflow_client --ping && ecflow_stop.sh -p ${ECF_PORT} || true
ecflow_start.sh -p ${ECF_PORT}

INGEST_YAML="${JEDI_WORKFLOW}/skylab/experiments/ingest-observations.yaml"

# Set ingest dates to yesterday's cycles
YESTERDAY=$(date +%Y-%m-%d -d "yesterday")
INIT_CYCLE="${YESTERDAY}T00:00:00Z"
LAST_CYCLE="${YESTERDAY}T18:00:00Z"

sed -i "s/init_cycle: .*/init_cycle: ${INIT_CYCLE}/g" "${INGEST_YAML}"
sed -i "s/last_cycle: .*/last_cycle: ${LAST_CYCLE}/g" "${INGEST_YAML}"

# Randomly select 3 active observation !INCLUDE lines to keep ingest
# runtime manageable. Comment out all active lines, then re-enable 3.
ACTIVE_LINES=$(grep -n '^- !INCLUDE' "${INGEST_YAML}" | cut -d: -f1)
ACTIVE_COUNT=$(echo "${ACTIVE_LINES}" | wc -w)

if [ "${ACTIVE_COUNT}" -gt 3 ]; then
    for line_num in ${ACTIVE_LINES}; do
        sed -i "${line_num}s/^- !INCLUDE/#- !INCLUDE/" "${INGEST_YAML}"
    done

    SELECTED=$(echo "${ACTIVE_LINES}" | tr ' ' '\n' | shuf -n 3)
    for line_num in ${SELECTED}; do
        sed -i "${line_num}s/^#- !INCLUDE/- !INCLUDE/" "${INGEST_YAML}"
    done
fi

# Remove "bias ingest:" blocks from individual obs ingest YAMLs.
# These trigger fetchObsBias tasks that require bias correction files
# not available in the localhost R2D2 data store.
for ingest_file in "${JEDI_WORKFLOW}"/skylab/obs/ingest/*_ingest.yaml; do
    if grep -q '^bias ingest:' "${ingest_file}"; then
        echo "Removing bias ingest block from $(basename ${ingest_file})"
        sed -i '/^bias ingest:/,/^[^ ]/{ /^bias ingest:/d; /^[^ ]/!d; }' "${ingest_file}"
    fi
done

echo "--- Ingest experiment YAML (dates: ${INIT_CYCLE} to ${LAST_CYCLE}) ---"
grep -E 'init_cycle|last_cycle|!INCLUDE' "${INGEST_YAML}"
echo "----------------------------------------------------------------------"

create_experiment.py --test "${INGEST_YAML}"
sleep 10

# ---------------------------------------------------------------------------
# 7. Poll experiment status
# ---------------------------------------------------------------------------

# Get the suite name registered by create_experiment.py
SUITE=$(echo $(ecflow_client --suites) | grep -o '[^ ]*$')
echo "Monitoring EWOK experiment suite: ${SUITE}"

# --- Phase 1: Wait for ingest storeObservations tasks (1 hour timeout) ---
INGEST_TIMEOUT=3600

# Suspend endCycle so individual tasks can be inspected before the suite moves on
ecflow_client --suspend=/${SUITE}/ingest/endCycle

# Build a list of storeObservations tasks from the ecFlow definition
obstypes=$(ecflow_client --get=/${SUITE}/ingest \
    | grep -i 'task storeObservations_' \
    | awk '{print $2}' \
    | sed 's/storeObservations_//g')
TASKS=()
for obstype in ${obstypes[@]}; do
    TASKS+=("/${SUITE}/ingest/obs_${obstype}/storeObservations_${obstype}")
done
echo "Watching ${#TASKS[@]} ingest tasks: ${TASKS[*]}"

while [ "${INGEST_TIMEOUT}" -gt 0 ]; do
    complete=0
    for TASK in "${TASKS[@]}"; do
        task_state=$(ecflow_client --query state "${TASK}")
        echo "${TASK} is ${task_state} : ${INGEST_TIMEOUT}s remaining"
        case "${task_state}" in
            "aborted")
                echo "Failed: ${TASK} aborted"
                exit 1
                ;;
            "complete")
                (( complete+=1 ))
                ;;
        esac
    done

    if [ "${complete}" -eq "${#TASKS[@]}" ]; then
        echo "All ${#TASKS[@]} ingest tasks complete"
        ecflow_client --resume=/${SUITE}/ingest/endCycle
        break
    fi

    (( INGEST_TIMEOUT-=30 ))
    sleep 30
done

if [ "${INGEST_TIMEOUT}" -le 0 ]; then
    echo "Timeout waiting for ingest tasks"
    exit 1
fi

# --- Phase 2: Wait for full suite completion (2 hour timeout) ---
SUITE_TIMEOUT=7200

# Allow ecFlow to process the endCycle resume before polling
sleep 10

suite_state=$(ecflow_client --query state /${SUITE})

while [ "${suite_state}" != "aborted" ] && [ "${SUITE_TIMEOUT}" -gt 0 ]; do
    echo "${SUITE} is ${suite_state} : ${SUITE_TIMEOUT}s remaining"

    ewok_status=$(ecflow_client --query variable /${SUITE}:EWOK_STATUS)
    if [ "${ewok_status}" = "Finished" ]; then
        echo "Completed: ${SUITE} EWOK_STATUS=${ewok_status}"
        break
    fi

    (( SUITE_TIMEOUT-=30 ))
    sleep 30
    suite_state=$(ecflow_client --query state /${SUITE})
done

if [ "${suite_state}" = "aborted" ]; then
    echo "Failed: ${SUITE} is ${suite_state}"
    echo "--- Aborted tasks ---"
    ecflow_client --get_state /${SUITE} | grep -i "aborted" || true
    echo "---------------------"
    exit 1
fi

if [ "${SUITE_TIMEOUT}" -le 0 ]; then
    echo "Timeout: ${SUITE} is ${suite_state}"
    exit 1
fi

ecflow_client --resume=/${SUITE}/finishExperiment

# ---------------------------------------------------------------------------
# 8. Cleanup
# ---------------------------------------------------------------------------

ecflow_stop.sh -p ${ECF_PORT} || true

echo "Stopping R2D2 API server and MySQL"
kill $R2D2_PID 2>/dev/null || true
kill $MYSQL_PID 2>/dev/null || true
wait $R2D2_PID 2>/dev/null || true
wait $MYSQL_PID 2>/dev/null || true

echo "Skylab ingest complete"
