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

# Skylab workflow paths
# TODO: these should be set in environment.sh?
# TODO: do we want them in $WORKDIR?
export JEDI_WORKFLOW="${WORKDIR}/jedi-workflow"
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
curl -sS https://bootstrap.pypa.io/get-pip.py | python3

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

echo "--- Ingest experiment YAML (dates: ${INIT_CYCLE} to ${LAST_CYCLE}) ---"
grep -E 'init_cycle|last_cycle|!INCLUDE' "${INGEST_YAML}"
echo "----------------------------------------------------------------------"

create_experiment.py --test "${INGEST_YAML}"
sleep 10

# ---------------------------------------------------------------------------
# 7. Poll experiment status
# ---------------------------------------------------------------------------

STATUS_SCRIPT="${JEDI_WORKFLOW}/skylab/.github/workflows/status.sh"

# First poll: 1 hour timeout for ingest tasks
time "${STATUS_SCRIPT}" 3600

# Second poll: 2 hour timeout for full suite completion
time "${STATUS_SCRIPT}" 7200

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
