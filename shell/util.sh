#!/usr/bin/env bash
#
# Function definitions.
#

# Used by any exit clauses of the script, this function will first check if
# a debug sleep is enabled (allowing users time to log-in and inspect tests
# environments), then the work directory is cleared allowing the script to
# exit without consuming additional storage.
util.evaluate_debug_timer_then_cleanup() {
    # Sleep at the end of execution if debug mode is enabled.
    if [ -n "${DEBUG_TIME_SECONDS}" ]; then
        echo "Debug mode is enabled, sleeping for ${DEBUG_TIME_SECONDS} seconds"
        sleep $DEBUG_TIME_SECONDS
    fi
    # Clear workdir
    rm -rf ${WORKDIR}/*
}


# Find the Test.xml file. Under some circumstances the Test.xml file may
# be in an unexpected location, this function attempts to find the file at
# the expected location, then expands the search if it is not found
# See this bug for more information on why this function is needed:
#  - https://github.com/JCSDA-internal/CI/issues/56
util.find_test_xml() {
    test_tag=$(head -1 "${BUILD_DIR}/Testing/TAG")
    expected_file="${BUILD_DIR}/Testing/${test_tag}/Test.xml"
    if [ -f "${expected_file}" ]; then
        echo "${expected_file}"
        return 0
    fi
    # Infer here that the file was not found at the expected tag location,
    # use find to look for the file.
    found_file="$(find ${BUILD_DIR}/Testing -type f -name "Test.xml" | head -n1)"
    if [ -f "${found_file}" ]; then
        echo "${found_file}"
    else
        # This case is very rare and is associated with build errors that are
        # detected earlier. If this happens, we just update the string to make
        # later errors more obvious.
        echo "no-file-found-for-Test.xml"
    fi
}

# Generate a cdash url using the upload output xml.
util.create_cdash_url() {
    test_dir=$1
    tag=$(head -1 "${test_dir}/TAG")
    Done=$(cat "${test_dir}/${tag}/Done.xml")
    buildID=$(echo $Done | grep -o -P '(?<=buildId>).*(?=</build)')
    echo "${CDASH_URL}/viewTest.php?buildid=$buildID"
}

# Create a new check run in the queued state.
# Takes three arguments:
#     $1: Repository name in "owner/repo" format.
#     $2: trigger commit sha hash.
util.check_run_new() {
    if [ $SKIP_GITHUB_CHECK_RUNS = 'yes' ]; then
        return 0
    fi
    repo=$1
    commit_sha=$2
    ${CI_SCRIPTS_DIR}/github_api/check_run.py new \
        --app-private-key="${GITHUB_APP_PRIVATE_KEY_FILE}" \
        --app-id="${GITHUB_APP_ID}" \
        --repo=$repo \
        --commit=$commit_sha \
        --test-platform=${JEDI_COMPILER} \
        --ecs-metadata-uri="${ECS_CONTAINER_METADATA_URI_V4}" \
        --batch-task-id="${AWS_BATCH_JOB_ID}"
}

# Update a check run setting the status to failure and giving a simple
# reason for the failure like "compile failed" or similar. Args:
#     $1: Repository name in "owner/repo" format.
#     $2: Check Run ID: the identifier from GitHub's API.
#     $3: Failure reason string.
util.check_run_fail() {
    if [ $SKIP_GITHUB_CHECK_RUNS = 'yes' ]; then
        return 0
    fi
    repo=$1
    run_id=$2
    fail_reason=$3
    if [ $run_id -eq 0 ]; then
        return 0
    fi
    ${CI_SCRIPTS_DIR}/github_api/check_run.py update \
        --app-private-key="${GITHUB_APP_PRIVATE_KEY_FILE}" \
        --app-id="${GITHUB_APP_ID}" \
        --repo=$repo \
        --check-run-id="${run_id}" \
        --ecs-metadata-uri="${ECS_CONTAINER_METADATA_URI_V4}" \
        --batch-task-id="${AWS_BATCH_JOB_ID}" \
        --public-log-link="${PUBLIC_LOG_URL}" \
        --status="completed" \
        --conclusion="failure" \
        --title="${fail_reason}"
}

# Update a check run to have a status of "complete" and a conclusion of
# "success". This type of update is used to skip a test that is not required
# but would otherwise cause the repository to block merging.
# Args:
#     $1: Repository name in "owner/repo" format.
#     $2: Check Run ID: the identifier from GitHub's API.
util.check_run_successful_skip() {
    if [ $SKIP_GITHUB_CHECK_RUNS = 'yes' ]; then
        return 0
    fi
    repo=$1
    run_id=$2
    if [ $run_id -eq 0 ]; then
        return 0
    fi
    ${CI_SCRIPTS_DIR}/github_api/check_run.py update \
        --app-private-key="${GITHUB_APP_PRIVATE_KEY_FILE}" \
        --app-id="${GITHUB_APP_ID}" \
        --repo=$repo \
        --check-run-id="${run_id}" \
        --ecs-metadata-uri="${ECS_CONTAINER_METADATA_URI_V4}" \
        --batch-task-id="${AWS_BATCH_JOB_ID}" \
        --public-log-link="${PUBLIC_LOG_URL}" \
        --status="completed" \
        --conclusion="success" \
        --title="no required tests"
}

# Update a queued check-run setting its status to 'in_progress' and
# details title to "building". Also attach a link to the build logs.
# Args:
#     $1: Repository name in "owner/repo" format.
#     $2: Check Run ID: the identifier from GitHub's API.
util.check_run_start_build() {
    if [ $SKIP_GITHUB_CHECK_RUNS = 'yes' ]; then
        return 0
    fi
    repo=$1
    run_id=$2
    if [ $run_id -eq 0 ]; then
        return 0
    fi
    # As with runner_allocated we don't include the public log link.
    ${CI_SCRIPTS_DIR}/github_api/check_run.py update \
        --app-private-key="${GITHUB_APP_PRIVATE_KEY_FILE}" \
        --app-id="${GITHUB_APP_ID}" \
        --repo=$repo \
        --check-run-id="${run_id}" \
        --status="in_progress" \
        --ecs-metadata-uri="${ECS_CONTAINER_METADATA_URI_V4}" \
        --batch-task-id="${AWS_BATCH_JOB_ID}" \
        --title='building'
}

# Update a queued check-run setting its status to 'in_progress' and
# details title to "testing". Also attach a link to the build logs.
# Args:
#     $1: Repository name in "owner/repo" format.
#     $2: Check Run ID: the identifier from GitHub's API.
util.check_run_start_test() {
    if [ $SKIP_GITHUB_CHECK_RUNS = 'yes' ]; then
        return 0
    fi
    repo=$1
    run_id=$2
    if [ $run_id -eq 0 ]; then
        return 0
    fi
    # As with runner_allocated we don't include the public log link.
    ${CI_SCRIPTS_DIR}/github_api/check_run.py update \
        --app-private-key="${GITHUB_APP_PRIVATE_KEY_FILE}" \
        --app-id="${GITHUB_APP_ID}" \
        --repo="${repo}" \
        --check-run-id="${run_id}" \
        --status="in_progress" \
        --ecs-metadata-uri="${ECS_CONTAINER_METADATA_URI_V4}" \
        --batch-task-id="${AWS_BATCH_JOB_ID}" \
        --title='testing'
}

# Update a queued check-run setting its status to 'complete' and set
# the conclusion based on the contents of the Test.xml file and a maximum
# failure rate. Additionally this will author a summary markdown
# document that will be rendered in the GitHub UI.
# Args:
#     $1: Repository name in "owner/repo" format.
#     $2: Check Run ID: the identifier from GitHub's API.
#     $3: (integer) The max allowed failure percentage.
util.check_run_end() {
    if [ $SKIP_GITHUB_CHECK_RUNS = 'yes' ]; then
        return 0
    fi
    repo=$1
    run_id=$2
    if [ $run_id -eq 0 ]; then
        return 0
    fi
    max_fail_ppc=$3
    cdash_url=$(util.create_cdash_url "${BUILD_DIR}/Testing")
    test_xml=$(util.find_test_xml)
    ${CI_SCRIPTS_DIR}/github_api/check_run.py end \
        --app-private-key="${GITHUB_APP_PRIVATE_KEY_FILE}" \
        --app-id="${GITHUB_APP_ID}" \
        --repo="${repo}" \
        --check-run-id="${run_id}" \
        --test-xml="${test_xml}" \
        --max-failure-percentage $max_fail_ppc \
        --cdash-url="${cdash_url}" \
        --public-log-link="${PUBLIC_LOG_URL}" \
        --ecs-metadata-uri="${ECS_CONTAINER_METADATA_URI_V4}" \
        --batch-task-id="${AWS_BATCH_JOB_ID}"
}


# This function checks the Test.xml file to determine if we should mark the test
# as passed or failed. This is used for bash-logic, not for cdash or check-run
# outputs. Takes no arguments. This only runs after the unit tests.
# Args:
#     $1: (integer) The max allowed failure percentage.
#
# Returns:
#     return code 0 if the test is successful, 1 if the test is a failure.
util.check_run_eval_test_xml() {
    max_fail_ppc=$1
    if [ $SKIP_GITHUB_CHECK_RUNS = 'yes' ]; then
        return 0
    fi
    test_xml=$(util.find_test_xml)
    if ${CI_SCRIPTS_DIR}/github_api/check_run.py eval_test_xml \
            --test-xml="${test_xml}" \
            --max-failure-percentage $max_fail_ppc
    then
        return 0
    fi
    return 1
}

# This function generates a ctest label exclude flag from a given
# regex input if and only if the input is not empty. Otherwise it
# Returns the empty string preventing ctest from receiving an empty flag.
# This function is tolerant of extraneous pipes created by appending
# nullable regex components with pipes.
# Args:
#     $1: Regex of test labels to exclude, or empty string to exclude no labels.
util.ctest_LE_flag() {
    # Accept input, if it includes leading or trailing pipe, remove it.
    exclude_regex="$(echo "${1}" | sed 's/|$//' | sed 's/^|//')"

    # If input regex is zero, we return no flag.
    if [ -z "${exclude_regex}" ]; then
        echo ""
        return 0
    fi
    echo "-LE ${exclude_regex}"
    return 0
}

#
# Structured status logging.
#
# Append-only record of the test lifecycle, written as a YAML document: a
# metadata header followed by an `events:` list, one event per line. Uploaded
# to the public bucket for aggregation into a global status dashboard. Unlike
# CDash, this captures failures before or during configure/build.
#
# Example output:
#
#   repo: JCSDA/ufo
#   commit: a1b2c3d
#   pr: "42"
#   compiler: gcc11
#   batch_job_id: abc-123
#   build_identity: ufo-gcc11
#   public_log_url: https://.../ufo-gcc11-xxxx.html
#   start_time: 2026-06-03T12:00:00Z
#   events:
#     - {time: 2026-06-03T12:01:00Z, event: configure, status: success}
#     - {time: 2026-06-03T12:45:00Z, event: unit_test, status: failure, passed: 10, failed: 2}

# Status log path. Normally exported by bootstrap_test.sh so the uploader
# shares the same path.
export STATUS_LOG="${STATUS_LOG:-/tmp/status.log}"

# Initialize the status log document. Call once at the start of the test run.
util.status_log_init() {
    local ts
    ts="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    cat > "${STATUS_LOG}" <<EOF
repo: ${TRIGGER_REPO_FULL}
commit: ${TRIGGER_SHA}
pr: "${TRIGGER_PR}"
compiler: ${JEDI_COMPILER}
scheduled: "${IS_SCHEDULED:-no}"
batch_job_id: ${AWS_BATCH_JOB_ID}
build_identity: ${BUILD_IDENTITY}
public_log_url: ${PUBLIC_LOG_URL}
start_time: ${ts}
events:
EOF
}

# Append a single lifecycle event to the status log.
# Args:
#     $1: event name (e.g. "configure", "build", "unit_test", "cdash_upload").
#     $2: status (e.g. "start", "success", "failure", "skipped").
#     $3: (optional) free-form human-readable detail string, defaults to "".
util.status_log_event() {
    local event="$1"
    local status="$2"
    local detail="${3:-}"
    local ts
    ts="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    printf "  - " >> "${STATUS_LOG}"
    jq -cn --arg time "${ts}" --arg event "${event}" --arg status "${status}" --arg detail "${detail}" \
        '{time: $time, event: $event, status: $status, detail: $detail}' >> "${STATUS_LOG}"
}

# Append a test-result event, with status and test counts parsed from a ctest
# Test.xml by check_run.py.
# Args:
#     $1: event name ("unit_test" or "integration_test").
#     $2: path to the ctest Test.xml file.
util.status_log_test_result() {
    local event="$1"
    local test_xml="$2"
    local ts
    ts="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

    # Parse stats; a missing/unparseable Test.xml yields a "no_result" status.
    local stat_json
    stat_json="$(${CI_SCRIPTS_DIR}/github_api/check_run.py stat_test_xml --test-xml="${test_xml}")" || stat_json='{"status": "no_result"}'

    # Merge the base event object with the parsed stats. Compact (-c) keeps it
    # on one line; JSON flow mappings are valid YAML.
    printf "  - " >> "${STATUS_LOG}"
    jq -cn --arg time "${ts}" --arg event "${event}" --argjson stat "${stat_json}" \
        '{time: $time, event: $event} + $stat'  >> "${STATUS_LOG}"
}
