#!/bin/bash
source /opt/spack-environment/activate.sh

# Directory of this script.
export SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Setup public log stream.
_RANDOM="$(head /dev/urandom | base32 | head -c 8)"
_REGION="$(aws s3api get-bucket-location --bucket $PUBLIC_LOGS_BUCKET | jq -r .LocationConstraint)"
export PUBLIC_LOG_S3="s3://${PUBLIC_LOGS_BUCKET}/${BUILD_IDENTITY}-${_RANDOM}.html"
export PUBLIC_LOG_URL="https://${PUBLIC_LOGS_BUCKET}.s3.${_REGION}.amazonaws.com/${BUILD_IDENTITY}-${_RANDOM}.html"

# Setup GitHub app credentials.
cp $SCRIPT_DIR/git_askPass_app_credentials.py /bin/git_askPass_app_credentials.py
chmod +x /bin/git_askPass_app_credentials.py
git config --global core.askPass /bin/git_askPass_app_credentials.py

# Make sure the GitHub app key is a file.
if [ ! -f "${GITHUB_APP_PRIVATE_KEY}" ]; then
    key_file=$(mktemp -u)
    echo "$GITHUB_APP_PRIVATE_KEY" > $key_file
    export GITHUB_APP_PRIVATE_KEY=$key_file
fi

# Workdir should already exist but create it if it doesn't.
export WORKDIR=/workdir
mkdir -p $WORKDIR
cd $WORKDIR

export JEDI_BUNDLE_DIR="${WORKDIR}/bundle"

# Download and export the CRTM binary files tarball. This is done before the
# first use of git credentials to prevent the download from allowing a
# credential timeout during the ecbuild step.
export CRTM_BINARY_FILES_TARBALL="${WORKDIR}/crtm_coeffs.tgz"
wget --no-verbose -O get_crtm_tarball.sh https://raw.githubusercontent.com/JCSDA/CRTMv3/refs/heads/develop/Get_CRTM_Binary_Files.sh
chmod +x get_crtm_tarball.sh
./get_crtm_tarball.sh -d "${CRTM_BINARY_FILES_TARBALL}"

echo "Starting tests."
$WORKDIR/bundle/jedi_ci_resources/${TEST_SCRIPT} 2>&1 | tee /tmp/build_logs.txt &
TEST_PID=$!

# Wait for 30 seconds to make sure the tests are running.
sleep 30

# Background process to convert logs every 2 minutes for the first
# 10 minutes then every 5 minutes thereafter.
MONITOR_UPLOADS=0
SLEEP_TIME=120  # 2 minutes
while kill -0 $TEST_PID 2>/dev/null; do
    cat /tmp/build_logs.txt | python -m ansi2html -l > /tmp/build_logs.html
    aws s3 cp /tmp/build_logs.html $PUBLIC_LOG_S3 --content-type "text/html"
    sleep $SLEEP_TIME
    MONITOR_UPLOADS=$((MONITOR_UPLOADS + 1))
    if [[ $MONITOR_UPLOADS == 5 ]]; then
        SLEEP_TIME=300  # 5 minutes
    fi
done &
MONITOR_PID=$!

wait $TEST_PID
TEST_EXIT_CODE=$?
echo "Test completed with return code: $TEST_EXIT_CODE"

# Kill the monitoring process and do final conversion
kill $MONITOR_PID 2>/dev/null || true
sleep 1

set -x
cat /tmp/build_logs.txt | python -m ansi2html -l > /tmp/build_logs.html
aws s3 cp /tmp/build_logs.html $PUBLIC_LOG_S3 --content-type "text/html"
df -h
