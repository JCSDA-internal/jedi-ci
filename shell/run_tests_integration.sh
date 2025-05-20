#!/usr/bin/env bash

# This script runs integration tests for the JEDI CI system. Unlike the other
# test runner script in this project, this will automatically mark unit tests
# as passed and will directly run integration tests. It will still evaluate
# the build information and assemble a specific build group based on the
# change that started this test.

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
if [ -z $BUILD_INFO_B64 ]; then
    echo "Var BUILD_INFO_B64 must be set; it should be a base64 encoded gzipped json string with build configuration and dependency versions."
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


if [ $valid_environment_found == "no" ]; then
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


# From this point forward we are executing the test and sending debug to stderr.
set -x


#
# Setup and run tests.
#

# Starting here we need to clone several things from GitHub, altough all
# git operations should take far less than the 10 minute life of this token.



# The lambda function has generated a json job config and passed it to this test
# script as "BUILD_INFO_B64" after zipping and base64 encoding it. This step
# decodes the job config json, and extracts several important config values.
BUILD_JSON=$(mktemp)
echo $BUILD_INFO_B64 | base64 --decode | gunzip > $BUILD_JSON
TRIGGER_REPO=$(jq -r '.trigger_repo' $BUILD_JSON)
TRIGGER_MANIFEST_NAME=$(jq -r '.manifest_name' $BUILD_JSON)
UNITTEST_TAG=$(jq -r '.test_tag' $BUILD_JSON)
TRIGGER_SHA=$(jq -r '.trigger_commit_sha' $BUILD_JSON)
TRIGGER_PR=$(jq -r '.trigger_pr_number' $BUILD_JSON)
TRIGGER_REPO_FULL="JCSDA-internal/${TRIGGER_REPO}"
INTEGRATION_RUN_ID="$(jq -r ".check_runs.integration" $BUILD_JSON)"
UNIT_RUN_ID="$(jq -r ".check_runs.unit" $BUILD_JSON)"
REFRESH_CACHE_ON_FETCH="$(jq -r ".skip_cache" $BUILD_JSON)"
REFRESH_CACHE_ON_WRITE="$(jq -r ".rebuild_cache" $BUILD_JSON)"
JEDI_BUNDLE_BRANCH="$(jq -r ".jedi_bundle_branch" $BUILD_JSON)"


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
util.check_run_successful_skip $TRIGGER_REPO_FULL $UNIT_RUN_ID
util.check_run_start_build $TRIGGER_REPO_FULL $INTEGRATION_RUN_ID

if [ -n "${JEDI_BUNDLE_BRANCH}" ]; then
    git clone https://github.com/JCSDA-internal/jedi-bundle.git -b "${JEDI_BUNDLE_BRANCH}" "${JEDI_BUNDLE_DIR}"
else
    git clone https://github.com/JCSDA-internal/jedi-bundle.git "${JEDI_BUNDLE_DIR}"
fi

# Configure cdash integration.
mkdir "${JEDI_BUNDLE_DIR}/cmake"
cp "${WORKDIR}/CI/src/configure_bundle/ctest_assets/CTestConfig.cmake"       "${JEDI_BUNDLE_DIR}/"
cp "${WORKDIR}/CI/src/configure_bundle/ctest_assets/CTestCustom.ctest.in"    "${JEDI_BUNDLE_DIR}/cmake/"
cp "${WORKDIR}/CI/src/configure_bundle/ctest_assets/cdash-integration.cmake" "${JEDI_BUNDLE_DIR}/cmake/"
sed -i "s#CDASH_URL#${CDASH_URL}#g"           "${JEDI_BUNDLE_DIR}/CTestConfig.cmake"
sed -i "s#TEST_TARGET_NAME#${TRIGGER_REPO}#g" "${JEDI_BUNDLE_DIR}/CTestConfig.cmake"
echo "include(cmake/cdash-integration.cmake)" >> "${JEDI_BUNDLE_DIR}/CMakeLists.txt"
echo ""                                       >> "${JEDI_BUNDLE_DIR}/CMakeLists.txt"
echo "include(CTest)"                         >> "${JEDI_BUNDLE_DIR}/CMakeLists.txt"
echo ""                                       >> "${JEDI_BUNDLE_DIR}/CMakeLists.txt"


$WORKDIR/CI/src/configure_bundle/configure_bundle.py \
  --integration-test \
  --test-target $TRIGGER_MANIFEST_NAME \
  --dependency-version $VERSION_MAP \
  --bundle-root="${JEDI_BUNDLE_DIR}"

echo "---- JEDI Bundle CMakeLists.txt -----"
cat $JEDI_BUNDLE_DIR/CMakeLists.txt

if [ $? -ne 0 ]; then
    util.check_run_fail $TRIGGER_REPO_FULL $INTEGRATION_RUN_ID "Bundle configuration failed"
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
    exit 0
fi

# Back-date source files (search "back-date" in this file for an explanation).
find $JEDI_BUNDLE_DIR -type f -exec touch -d "$SOURCE_BACKDATE_TIMESTAMP" {} \;

make -j $BUILD_PARALLELISM
if [ $? -ne 0 ]; then
    util.check_run_fail $TRIGGER_REPO_FULL $INTEGRATION_RUN_ID "compilation failed"
    exit 0
fi

util.check_run_start_test $TRIGGER_REPO_FULL $INTEGRATION_RUN_ID

# Run tests.
ctest -LE "gsibec|rttov|oasim|ropp-ufo" --timeout 180 -C RelWithDebInfo -D ExperimentalTest

# Upload ctests.
ctest -C RelWithDebInfo -D ExperimentalSubmit -M Continuous -- --track Continuous --group Continuous

find ${BUILD_DIR}/Testing -type f
find ${BUILD_DIR}/Testing -type f -exec head -n5 {} \;

echo "CDash URL: $(util.create_cdash_url "${BUILD_DIR}/Testing")"
TEST_TAG=$(head -1 "${BUILD_DIR}/Testing/TAG")
ls -al "${BUILD_DIR}/Testing/${TEST_TAG}/"

# Complete unit tests with max failure percentage of 3
util.check_run_end $TRIGGER_REPO_FULL $INTEGRATION_RUN_ID 3

# Sleep at the end of execution if debug mode is enabled.
if [ -n "${DEBUG_TIME_SECONDS}" ]; then
    sleep $DEBUG_TIME_SECONDS
fi
