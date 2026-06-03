#!/usr/bin/env bash

# This file is the end-to-end test execution orchestrator used by the CI
# system. This test script builds the full jedi-bundle once, then runs unit
# tests and integration tests sequentially appending the results to a single
# GitHub check run.
#
# The script can be thought of as being split into several sections.
#
#   1) Environment validation and variable definitions. Several shell vars are
#      used or expected for this script to execute. This section validates the
#      environment has these values. It also sets several other values that are
#      reused throughout this script.
#   2) Function definitions. Any reused logic is defined as functions here.
#   3) Activate spack-stack environment. For now this assumes the use of a
#      docker image, but this could be substituted if needed.
#   4) Install any missing packages. Some applications and Python libraries are
#      missing from our standard environment. These can be set up here, although
#      the build can be sped up in the future by pre-loading these packages in
#      the test images.
#   5) Setup and run tests. Most of the actual test setup is done here, although
#      it depends on the environment and inputs prepared in steps 1-4.

#
# Environment validation and variable definitions.
#

# Load common function definitions.
source $WORKDIR/bundle/jedi_ci_resources/environment.sh
source $WORKDIR/bundle/jedi_ci_resources/util.sh

# Return code for any line of shell code containing a pipe or redirect will
# come from the inner-most executable command.
set -o pipefail

# Validate environment.
valid_environment_found="yes"

if [ -z $GITHUB_APP_PRIVATE_KEY ]; then
    echo "Var GITHUB_APP_PRIVATE_KEY must be set and must contain the text of the GitHub App private key or a file path of the key"
    valid_environment_found="no"
fi
if [ -z $GITHUB_APP_ID ]; then
    echo "Var GITHUB_APP_ID must be set and must contain the GitHub App ID"
    valid_environment_found="no"
fi
if [ -z $GITHUB_INSTALL_ID ]; then
    echo "Var GITHUB_INSTALL_ID must be set and must contain the GitHub App install ID used for API access with target repositories."
    valid_environment_found="no"
fi
if [ -z "${JEDI_COMPILER}" ]; then
    echo "Var JEDI_COMPILER must be set. This variable must be the name of the build environment toolchain."
    valid_environment_found="no"
fi
if [ -z $AWS_BATCH_JOB_ID ]; then
    # This variable is set by AWS Batch.
    echo "Var AWS_BATCH_JOB_ID must be set.."
    valid_environment_found="no"
fi
if [ -z "${ECS_CONTAINER_METADATA_URI_V4}" ]; then
    # This variable is set by AWS Batch.
    echo "Var ECS_CONTAINER_METADATA_URI_V4 must be set."
    valid_environment_found="no"
fi
if [ -z "${TRIGGER_REPO_FULL}" ]; then
    # This variable is set by AWS Batch.
    echo "Var TRIGGER_REPO_FULL must be set."
    valid_environment_found="no"
fi
if [ -z "${CACHE_BUCKET}" ]; then
    # This variable is set by AWS Batch.
    echo "Var CACHE_BUCKET must be set."
    valid_environment_found="no"
fi


if [ $valid_environment_found == "no" ]; then
    util.evaluate_debug_timer_then_cleanup
    exit 1
fi

cat << EOF
Configuration:
JEDI_COMPILER=${JEDI_COMPILER}
GITHUB_APP_PRIVATE_KEY_FILE=${GITHUB_APP_PRIVATE_KEY_FILE}
GITHUB_APP_ID=${GITHUB_APP_ID}
GITHUB_INSTALL_ID=${GITHUB_INSTALL_ID}
BUILD_PARALLELISM=${BUILD_PARALLELISM}
WORKDIR=${WORKDIR}
CDASH_URL=${CDASH_URL}
jedi_cmake_ROOT=${jedi_cmake_ROOT}
OMPI_ALLOW_RUN_AS_ROOT=${OMPI_ALLOW_RUN_AS_ROOT}
OMPI_ALLOW_RUN_AS_ROOT_CONFIRM=${OMPI_ALLOW_RUN_AS_ROOT_CONFIRM}
OMPI_MCA_rmaps_base_oversubscribe=${OMPI_MCA_rmaps_base_oversubscribe}
CI_SCRIPTS_DIR=${CI_SCRIPTS_DIR}
CC="${CC}"
CXX="${CXX}"
FC="${FC}"
CHECK_RUN_ID=${CHECK_RUN_ID}
EOF

echo "Fortran compiler version"
$FC -v

# For local testing, check runs are created by this script.
if [ "${CREATE_CHECK_RUNS}" == "yes" ]; then
    export CHECK_RUN_ID=$(util.check_run_new $TRIGGER_REPO_FULL $TRIGGER_SHA)
fi

echo "--------------------------------------------------------------"
echo "Platform debug info"
echo "--------------------------------------------------------------"
echo "aws sts get-caller-identity"
aws sts get-caller-identity
echo "df -h"
df -h
echo "lscpu"
lscpu
echo "ulimit -a"
ulimit -a

# From this point forward we are executing the test and sending debug to stderr.
set -x

#
# Setup sccache for caching. This will be handled by the image in the future but temporarily do setup here.
#
if [[ ! $(which sccache) ]]; then
    wget --no-verbose https://github.com/mozilla/sccache/releases/download/v0.14.0/sccache-v0.14.0-x86_64-unknown-linux-musl.tar.gz
    tar -xvf sccache-v0.14.0-x86_64-unknown-linux-musl.tar.gz
    mv ./sccache-v0.14.0-x86_64-unknown-linux-musl/sccache /usr/local/bin/sccache
    rm -rf sccache-v0.14.0-x86_64-unknown-linux-musl*
fi

# Export sccache AWS bucket config.
export SCCACHE_BUCKET="${CACHE_BUCKET}"
export SCCACHE_REGION="us-east-2"
export SCCACHE_S3_KEY_PREFIX="sccache-$JEDI_COMPILER-$($CC -dumpversion | tr -d '.')"
sccache --start-server


#
# Setup and run tests.
#


# Temporary bugfix; update awscrt because the spack-provded version is too old.
# BUG: https://github.com/JCSDA-internal/jedi-ci/issues/50
pip install --upgrade awscrt

# Extract just the repo name from the full repository path
TRIGGER_REPO=$(echo "$TRIGGER_REPO_FULL" | cut -d'/' -f2)

# Initialize the structured status log with run metadata. From here on we
# append lifecycle events (configure, build, test, upload) so overall test
# system status can be monitored independently of CDash and GitHub check runs.
util.status_log_init

# Mark the single check run as building and attach the batch job URL.
util.check_run_start_build $TRIGGER_REPO_FULL $CHECK_RUN_ID

# Get all GitLFS repositories from s3.
pushd ${JEDI_BUNDLE_DIR}
echo "showing git config"
git config --global credential.helper 'cache --timeout=590'
git config --list
echo "Fetching GitLFS repositories via tarball."
git config --global --add safe.directory '*'
for repo in ioda-data ufo-data fv3-jedi-data mpas-jedi-data jedi-model-data ; do
    echo "repo == ${repo}"
    aws s3 cp "s3://jcsda-public-rpays/JCSDA-internal/${repo}.tar.gz" "${repo}.tar.gz" --no-progress
    tar -xf "${repo}.tar.gz"
    cd ${repo}
    # Update refs
    git fetch --all
    cd ..
    rm "${repo}.tar.gz"
done
popd


# Configure cdash integration.
mkdir "${JEDI_BUNDLE_DIR}/cmake"
cp "${SCRIPT_DIR}/ctest_assets/CTestConfig.cmake"       "${JEDI_BUNDLE_DIR}/"
cp "${SCRIPT_DIR}/ctest_assets/CTestCustom.ctest.in"    "${JEDI_BUNDLE_DIR}/cmake/"
cp "${SCRIPT_DIR}/ctest_assets/cdash-integration.cmake" "${JEDI_BUNDLE_DIR}/cmake/"
sed -i "s#CDASH_URL#${CDASH_URL}#g"           "${JEDI_BUNDLE_DIR}/CTestConfig.cmake"
sed -i "s#CDASH_URL#${CDASH_URL}#g"           "${JEDI_BUNDLE_DIR}/CTestConfig.cmake"
sed -i "s#TEST_TARGET_NAME#${TRIGGER_REPO}#g" "${JEDI_BUNDLE_DIR}/CTestConfig.cmake"

# Update the CMakeLists.txt file to include cdash integration.
echo "include(cmake/cdash-integration.cmake)" >> "${JEDI_BUNDLE_DIR}/CMakeLists.txt"
echo ""                                       >> "${JEDI_BUNDLE_DIR}/CMakeLists.txt"
echo "include(CTest)"                         >> "${JEDI_BUNDLE_DIR}/CMakeLists.txt"
echo ""                                       >> "${JEDI_BUNDLE_DIR}/CMakeLists.txt"

echo "---- JEDI Bundle CMakeLists.txt -----"
cat $JEDI_BUNDLE_DIR/CMakeLists.txt
echo "-------------------------------------------------------"

#
# Build the full bundle.
#
cd "${BUILD_DIR}"

ecbuild \
      -Wno-dev \
      -DCMAKE_C_COMPILER_LAUNCHER=sccache \
      -DCMAKE_CXX_COMPILER_LAUNCHER=sccache \
      -DBUILD_GSIBEC=ON \
      -DCMAKE_BUILD_TYPE=RelWithDebInfo \
      -DCDASH_OVERRIDE_SYSTEM_NAME="${JEDI_COMPILER}-Container" \
      -DCDASH_OVERRIDE_SITE=AWSBatch \
      -DCDASH_OVERRIDE_GIT_BRANCH=${TRIGGER_PR} \
      -DCTEST_UPDATE_VERSION_ONLY=FALSE \
      -DBUILD_IODA_CONVERTERS=ON \
      -DBUILD_PYIRI=ON \
      "${COMPILER_FLAGS[@]}" "${JEDI_BUNDLE_DIR}"

if [ $? -ne 0 ]; then
    util.status_log_event configure failure "Bundle configuration failed"
    util.check_run_fail $TRIGGER_REPO_FULL $CHECK_RUN_ID "Bundle configuration failed"
    util.evaluate_debug_timer_then_cleanup
    exit 0
fi
util.status_log_event configure success

# Back-date source files (search "back-date" in this file for an explanation).
find $JEDI_BUNDLE_DIR -type f -exec touch -d "$SOURCE_BACKDATE_TIMESTAMP" {} \;

make -j $BUILD_PARALLELISM
if [ $? -ne 0 ]; then
    util.status_log_event build failure "compilation failed"
    util.check_run_fail $TRIGGER_REPO_FULL $CHECK_RUN_ID "compilation failed"
    util.evaluate_debug_timer_then_cleanup
    exit 0
fi
util.status_log_event build success

# Show sccache debug output.
sccache --show-stats

#
# Run unit tests (when a UNITTEST_TAG label is configured).
#

util.check_run_start_test $TRIGGER_REPO_FULL $CHECK_RUN_ID

if [ -n "${UNITTEST_TAG}" ]; then
    ctest $(util.ctest_LE_flag "${ENV_CTEST_EXCLUDES}") -L "${UNITTEST_TAG}" --timeout 500 -C RelWithDebInfo -M Experimental -T Test

    # Record unit test pass/fail counts to the status log.
    util.status_log_test_result unit_test "$(util.find_test_xml)"

    # Upload unit test results.
    if ctest -C RelWithDebInfo -T Submit --track Continuous --group Continuous; then
        util.status_log_event cdash_upload success "unit"
    else
        util.status_log_event cdash_upload failure "unit"
    fi

    # Debug info for cdash test tags. Do not remove until https://github.com/JCSDA-internal/jedi-ci/issues/70 is resolved.
    find ${BUILD_DIR}/Testing -type f
    find "${BUILD_DIR}/Testing" -type f -name "Done.xml" -exec head -n5 {} \;
    CDASH_TEST_TAG=$(head -1 "${BUILD_DIR}/Testing/TAG")
    ls -al "${BUILD_DIR}/Testing/${CDASH_TEST_TAG}/"
    # End of debug info.

    echo "CDash URL: $(util.create_cdash_url "${BUILD_DIR}/Testing")"

    # Evaluate the unit tests for failure. If they failed, update the check run
    # with failure and the test dashboard link. Otherwise continue to integration tests.
    ALLOWED_UNIT_FAIL_RATE=0
    if ! util.check_run_eval_test_xml $ALLOWED_UNIT_FAIL_RATE ; then
        util.check_run_end $TRIGGER_REPO_FULL $CHECK_RUN_ID $ALLOWED_UNIT_FAIL_RATE
        util.evaluate_debug_timer_then_cleanup
        exit 0
    fi
fi


#
# Run integration tests.
#

# If no unittest tag is configured, add a junk regex to avoid breaking the label regex.
if [ -z "${UNITTEST_TAG}" ]; then
    UNITTEST_TAG="NoTestsShouldMatchThisLabel"
fi

# Run integration tests.
ctest $(util.ctest_LE_flag "${ENV_CTEST_EXCLUDES}|${UNITTEST_TAG}|tier2|gsibec|rttov|oasim|ropp-ufo") --timeout 180 -C RelWithDebInfo -T Test

# Record integration test pass/fail counts to the status log.
util.status_log_test_result integration_test "$(util.find_test_xml)"

# Upload ctests.
if ctest -C RelWithDebInfo -T Submit --track Continuous --group Continuous; then
    util.status_log_event cdash_upload success "integration"
else
    util.status_log_event cdash_upload failure "integration"
fi

# Debug info for cdash test tags. Do not remove until https://github.com/JCSDA-internal/jedi-ci/issues/70 is resolved.
find ${BUILD_DIR}/Testing -type f
find "${BUILD_DIR}/Testing" -type f -name "Done.xml" -exec head -n5 {} \;
CDASH_TEST_TAG=$(head -1 "${BUILD_DIR}/Testing/TAG")
ls -al "${BUILD_DIR}/Testing/${CDASH_TEST_TAG}/"
# End of debug info.

echo "CDash URL: $(util.create_cdash_url "${BUILD_DIR}/Testing")"

ALLOWED_INTEGRATION_FAIL_RATE=0
util.check_run_end $TRIGGER_REPO_FULL $CHECK_RUN_ID $ALLOWED_INTEGRATION_FAIL_RATE

# Upload codecov data if gcc compiler is used.
if [ "$JEDI_COMPILER" = "gcc" ] && [ -f "${JEDI_BUNDLE_DIR}/${TRIGGER_REPO}/.codecov.yml" ]; then
    bash <(curl -s https://codecov.io/bash) -t 53f87271-b490-453c-b891-afd39cb658af -R "${JEDI_BUNDLE_DIR}/${TRIGGER_REPO}"
fi

util.status_log_event run complete
util.evaluate_debug_timer_then_cleanup
echo "test complete"
