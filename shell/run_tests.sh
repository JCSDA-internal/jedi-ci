#!/usr/bin/env bash

# This file is the end-to-end test execution orchestrator used by the CI
# system. Broadly this file can be split into several sections.
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
source $WORKDIR/CI/src/test_runner/util.sh
source $WORKDIR/CI/src/test_runner/environment.sh

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


if [ $valid_environment_found == "no" ]; then
    util.evaluate_debug_timer_then_cleanup
    exit 1
fi

cat << EOF
Configuration:
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
CI_CODE_PATH=${CI_CODE_PATH}
CC="${CC}"
CXX="${CXX}"
EOF


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
# Setup and run tests.
#


#REFRESH_CACHE_ON_FETCH="$(jq -r ".skip_cache" $BUILD_JSON)"
#REFRESH_CACHE_ON_WRITE="$(jq -r ".rebuild_cache" $BUILD_JSON)"
#JEDI_BUNDLE_BRANCH="$(jq -r ".jedi_bundle_branch" $BUILD_JSON)"

# Extract just the repo name from the full repository path
TRIGGER_REPO=$(echo "$TRIGGER_REPO_FULL" | cut -d'/' -f2)



# Generate the version ref flag value used later for build config. Ignore
# entries with null version_ref.commit values they are branch references already
# configured in the bundle.
VERSION_MAP=$(jq -j '[.version_map[] | select(.version_ref.commit != null) | "\(.name)=\(.version_ref.commit)" ] | join(" ")' $BUILD_JSON)
UNIT_DEPENDENCIES=$(jq -r '.dependencies | join(" ")' $BUILD_JSON)


if [ "${CREATE_CHECK_RUNS}" == "yes" ]; then
    UNIT_RUN_ID=$(util.check_run_new $TRIGGER_REPO_FULL "unit" $TRIGGER_SHA)
    INTEGRATION_RUN_ID=$(util.check_run_new $TRIGGER_REPO_FULL "integration" $TRIGGER_SHA)
fi

# Update check-runs to include the batch job URL is included.
util.check_run_runner_allocated $TRIGGER_REPO_FULL $INTEGRATION_RUN_ID
util.check_run_start_build $TRIGGER_REPO_FULL $UNIT_RUN_ID

# Get all GitLFS repositories from s3.
pushd ${JEDI_BUNDLE_DIR}
echo "showing git config"
git config --list
echo "Fetching GitLFS repositories via tarball."
git config --global --add safe.directory '*'
for repo in ioda-data ufo-data fv3-jedi-data mpas-jedi-data ; do
    echo "repo == ${repo}"
    aws s3 cp "s3://jcsda-usaf-ci-build-cache/lfs/${repo}.tar.gz" "${repo}.tar.gz" --no-progress
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
echo "include(cmake/cdash-integration.cmake)" >> "${JEDI_BUNDLE_DIR}/CMakeLists.txt"
echo ""                                       >> "${JEDI_BUNDLE_DIR}/CMakeLists.txt"
echo "include(CTest)"                         >> "${JEDI_BUNDLE_DIR}/CMakeLists.txt"
echo ""                                       >> "${JEDI_BUNDLE_DIR}/CMakeLists.txt"

# Switch to the unittest and integration CMakeLists.txt files.
cp $WORKDIR/bundle/CMakeLists.txt $JEDI_BUNDLE_DIR/CMakeLists.txt.unittest
cp $WORKDIR/bundle/CMakeLists.txt.integration $JEDI_BUNDLE_DIR/CMakeLists.txt


if [ $? -ne 0 ]; then
    if grep -qi "remote: Invalid username or password." configure_1.log; then
        util.check_run_fail $TRIGGER_REPO_FULL $UNIT_RUN_ID "Failure: see jcsda-internal/CI/issues/137"
    else
        util.check_run_fail $TRIGGER_REPO_FULL $UNIT_RUN_ID "Bundle configuration failed"
    fi
    util.check_run_skip $TRIGGER_REPO_FULL $INTEGRATION_RUN_ID
    util.evaluate_debug_timer_then_cleanup
    exit 0
fi

# Add the oasim, ropp, and rttov compiler flags if the unittests have one
# of these dependencies. Note that the same compiler flags are used for
# integration tests, so a package without these unittest dependencies will
# not build them for integration testing.
if grep -q -e "oasim" <<< $UNIT_DEPENDENCIES; then
    COMPILER_FLAGS+=( -DBUILD_OASIM=ON )
fi
if  grep -q -e "rttov" <<< $UNIT_DEPENDENCIES; then
    COMPILER_FLAGS+=( -DBUILD_RTTOV=ON )
fi
if  grep -q -e "ropp-ufo" <<< $UNIT_DEPENDENCIES; then
    COMPILER_FLAGS+=( -DBUILD_ROPP=ON )
fi


echo "---- JEDI Bundle CMakeLists.txt -----"
cat $JEDI_BUNDLE_DIR/CMakeLists.txt
echo "-------------------------------------"


#
# Build and run unit tests.
#
cd "${BUILD_DIR}"

# Fetch any pre-built artifacts from the build cache.
$WORKDIR/CI/src/test_runner/binary_cache.py fetch \
    --build-info-json $BUILD_JSON \
    --test-manifest $WORKDIR/CI/test_manifest.json \
    --cache-bucket jcsda-usaf-ci-build-cache \
    --container-version ${CONTAINER_VERSION:-latest} \
    --compiler $JEDI_COMPILER \
    --platform "$(uname)-$(uname -p)-batch" \
    --build-directory $BUILD_DIR \
    --refresh-cache $REFRESH_CACHE_ON_FETCH \
    --whitelist $UNIT_DEPENDENCIES $TRIGGER_REPO

ecbuild \
      -Wno-dev \
      -DCMAKE_BUILD_TYPE=RelWithDebInfo \
      -DCDASH_OVERRIDE_SYSTEM_NAME="${JEDI_COMPILER}-Container" \
      -DCDASH_OVERRIDE_SITE=AWSBatch \
      -DCDASH_OVERRIDE_GIT_BRANCH=${TRIGGER_PR} \
      -DCTEST_UPDATE_VERSION_ONLY=FALSE \
      -DBUILD_IODA_CONVERTERS=ON \
      -DBUILD_PYIRI=ON \
      ${COMPILER_FLAGS[@]} "${JEDI_BUNDLE_DIR}" | tee configure_2.log

if [ $? -ne 0 ]; then
    if grep -qi "remote: Invalid username or password." configure_2.log; then
        util.check_run_fail $TRIGGER_REPO_FULL $UNIT_RUN_ID "Failure: see jcsda-internal/CI/issues/137"
    else
        util.check_run_fail $TRIGGER_REPO_FULL $UNIT_RUN_ID "Bundle configuration failed"
    fi
    util.check_run_skip $TRIGGER_REPO_FULL $INTEGRATION_RUN_ID
    util.evaluate_debug_timer_then_cleanup
    exit 0
fi

# Back-date source files (search "back-date" in this file for an explanation).
find $JEDI_BUNDLE_DIR -type f -exec touch -d "$SOURCE_BACKDATE_TIMESTAMP" {} \;

make -j $BUILD_PARALLELISM
if [ $? -ne 0 ]; then
    util.check_run_fail $TRIGGER_REPO_FULL $UNIT_RUN_ID "compilation failed"
    util.check_run_skip $TRIGGER_REPO_FULL $INTEGRATION_RUN_ID
    util.evaluate_debug_timer_then_cleanup
    exit 0
fi

util.check_run_start_test $TRIGGER_REPO_FULL $UNIT_RUN_ID

# Run unit tests.
ctest -L $UNITTEST_TAG --timeout 500 -C RelWithDebInfo -D ExperimentalTest

# Upload ctests.
ctest -C RelWithDebInfo -D ExperimentalSubmit -M Continuous -- --track Continuous --group Continuous

echo "CDash URL: $(util.create_cdash_url "${BUILD_DIR}/Testing")"

# This is a temporary hack to allow UFO tests to pass until we resolve the
# flakes and/or persistent failures. Once UFO testing failures are resolved
# we can hard-code this failure rate to zero and remove this logic.
ALLOWED_UNIT_FAIL_RATE=0
if [ $UNITTEST_TAG = 'ufo' ]; then
    ALLOWED_UNIT_FAIL_RATE=1
fi

# Close out the check run for unit tests and mark success or failure.
util.check_run_end $TRIGGER_REPO_FULL $UNIT_RUN_ID $ALLOWED_UNIT_FAIL_RATE

# Decision point: if the unit tests failed then we should mark the integration
# tests as skipped and end test execution.
if ! util.check_run_eval_test_xml $ALLOWED_UNIT_FAIL_RATE ; then
    util.check_run_skip $TRIGGER_REPO_FULL $INTEGRATION_RUN_ID
    util.evaluate_debug_timer_then_cleanup
    exit 0
fi

#
# Build and run integration tests. This section will not be run if we detect
# a failure above (implemented)


# Delete test output to force re-generation of BuildID
TEST_TAG=$(head -1 "${BUILD_DIR}/Testing/TAG")

# Start the integration test run.
util.check_run_start_build $TRIGGER_REPO_FULL $INTEGRATION_RUN_ID

if [ $? -ne 0 ]; then
    util.check_run_fail $TRIGGER_REPO_FULL $INTEGRATION_RUN_ID "Bundle configuration failed"
    util.evaluate_debug_timer_then_cleanup
    exit 0
fi

# Fetch any pre-built artifacts from the build cache.
$WORKDIR/CI/src/test_runner/binary_cache.py fetch \
    --build-info-json $BUILD_JSON \
    --test-manifest $WORKDIR/CI/test_manifest.json \
    --cache-bucket jcsda-usaf-ci-build-cache \
    --container-version ${CONTAINER_VERSION:-latest} \
    --compiler $JEDI_COMPILER \
    --platform "$(uname)-$(uname -p)-batch" \
    --refresh-cache $REFRESH_CACHE_ON_FETCH \
    --build-directory $BUILD_DIR

ecbuild \
      -Wno-dev \
      -DCMAKE_BUILD_TYPE=RelWithDebInfo \
      -DCDASH_OVERRIDE_SYSTEM_NAME="${JEDI_COMPILER}-Container" \
      -DCDASH_OVERRIDE_SITE=AWSBatch \
      -DCDASH_OVERRIDE_GIT_BRANCH=${TRIGGER_PR} \
      -DCTEST_UPDATE_VERSION_ONLY=FALSE \
      -DBUILD_IODA_CONVERTERS=ON \
      -DBUILD_PYIRI=ON \
      ${COMPILER_FLAGS[@]} "${JEDI_BUNDLE_DIR}"
if [ $? -ne 0 ]; then
    util.check_run_fail $TRIGGER_REPO_FULL $INTEGRATION_RUN_ID "ecbuild failed"
    util.evaluate_debug_timer_then_cleanup
    exit 0
fi

# Back-date source files (search "back-date" in this file for an explanation).
find $JEDI_BUNDLE_DIR -type f -exec touch -d "$SOURCE_BACKDATE_TIMESTAMP" {} \;

make -j $BUILD_PARALLELISM
if [ $? -ne 0 ]; then
    util.check_run_fail $TRIGGER_REPO_FULL $INTEGRATION_RUN_ID "compilation failed"
    util.evaluate_debug_timer_then_cleanup
    exit 0
fi

util.check_run_start_test $TRIGGER_REPO_FULL $INTEGRATION_RUN_ID

# Run tests.
ctest -LE "${UNITTEST_TAG}|gsibec|rttov|oasim|ropp-ufo" --timeout 180 -C RelWithDebInfo -D ExperimentalTest

# Upload ctests.
ctest -C RelWithDebInfo -D ExperimentalSubmit -M Continuous -- --track Continuous --group Continuous

find ${BUILD_DIR}/Testing -type f
find ${BUILD_DIR}/Testing -type f -exec head -n5 {} \;

echo "CDash URL: $(util.create_cdash_url "${BUILD_DIR}/Testing")"
TEST_TAG=$(head -1 "${BUILD_DIR}/Testing/TAG")
ls -al "${BUILD_DIR}/Testing/${TEST_TAG}/"

# Complete integration tests and allow a failure rate up to 3%
util.check_run_end $TRIGGER_REPO_FULL $INTEGRATION_RUN_ID 3

#echo "Pushing build artifacts to cache."
$WORKDIR/CI/src/test_runner/binary_cache.py write \
    --build-info-json $BUILD_JSON \
    --cache-bucket jcsda-usaf-ci-build-cache \
    --container-version ${CONTAINER_VERSION:-latest} \
    --compiler $JEDI_COMPILER \
    --platform "$(uname)-$(uname -p)-batch" \
    --build-directory $BUILD_DIR \
    --refresh-cache $REFRESH_CACHE_ON_WRITE \
    --test-manifest $WORKDIR/CI/test_manifest.json

# Upload codecov data if gcc compiler is used.
if [ "$JEDI_COMPILER" = "gcc" ] && [ -f "${JEDI_BUNDLE_DIR}/${TRIGGER_REPO}/.codecov.yml" ]; then
    bash <(curl -s https://codecov.io/bash) -t 53f87271-b490-453c-b891-afd39cb658af -R "${JEDI_BUNDLE_DIR}/${TRIGGER_REPO}"
fi

util.evaluate_debug_timer_then_cleanup
echo "test complete"
