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
#     $2: Test-type; must be "unit" or "integration"
#     $3: trigger commit sha hash.
util.check_run_new() {
    if [ $SKIP_GITHUB_CHECK_RUNS = 'yes' ]; then
        return 0
    fi
    repo=$1
    test_type=$2
    commit_sha=$3
    ${CI_SCRIPTS_DIR}/github_api/check_run.py new \
        --app-private-key="${GITHUB_APP_PRIVATE_KEY_FILE}" \
        --app-id="${GITHUB_APP_ID}" \
        --repo=$repo \
        --commit=$commit_sha \
        --test-type=$test_type \
        --test-platform=${JEDI_COMPILER} \
        --ecs-metadata-uri="${ECS_CONTAINER_METADATA_URI_V4}" \
        --batch-task-id="${AWS_BATCH_JOB_ID}"
}

# Update a queued check-run with a link to the runner. This is used as
# a hand-off from the lambda function which does not yet have a link to
# the runner logs. Args:
#     $1: Repository name in "owner/repo" format.
#     $2: Check Run ID: the identifier from GitHub's API.
util.check_run_runner_allocated() {
    if [ $SKIP_GITHUB_CHECK_RUNS = 'yes' ]; then
        return 0
    fi
    repo=$1
    run_id=$2
    if [ $run_id -eq 0 ]; then
        return 0
    fi
    # Note: when the test is updated to indicate a runner is allocated we do
    # not include the public log link since it is not available until after all
    # tests on a build host are complete.
    ${CI_SCRIPTS_DIR}/github_api/check_run.py update \
        --app-private-key="${GITHUB_APP_PRIVATE_KEY_FILE}" \
        --app-id="${GITHUB_APP_ID}" \
        --repo=$repo \
        --check-run-id="${run_id}" \
        --ecs-metadata-uri="${ECS_CONTAINER_METADATA_URI_V4}" \
        --batch-task-id="${AWS_BATCH_JOB_ID}" \
        --title="Test runner allocated"
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
# "skipped". This is a soft failure mode and notes a failure upstream from
# the skipped test. Repositories with skipped check runs will not permit
# code to be merged.
# Args:
#     $1: Repository name in "owner/repo" format.
#     $2: Check Run ID: the identifier from GitHub's API.
util.check_run_skip() {
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
        --conclusion="skipped" \
        --title="prior step failed"
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
