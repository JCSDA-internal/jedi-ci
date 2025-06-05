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
export CRTM_BINARY_FILES_TARBALL="${WORKDIR}/crtmm_coeffs.tgz"
wget --no-verbose -O get_crtm_tarball.sh https://raw.githubusercontent.com/JCSDA/CRTMv3/refs/heads/develop/Get_CRTM_Binary_Files.sh
chmod +x get_crtm_tarball.sh
./get_crtm_tarball.sh -d "${CRTM_BINARY_FILES_TARBALL}"

echo "Starting tests."
$WORKDIR/bundle/jedi_ci_resources/run_tests_integration.sh 2>&1 | tee >(python -m ansi2html -l > /tmp/build_logs.html)

set -x
aws s3 cp /tmp/build_logs.html $PUBLIC_LOG_S3 --content-type "text/html"
df -h
