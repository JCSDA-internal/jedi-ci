#!/bin/bash
source /opt/spack-environment/activate.sh

_RANDOM="$(head /dev/urandom | base32 | head -c 8)"
_REGION="$(aws s3api get-bucket-location --bucket $PUBLIC_LOGS_BUCKET | jq -r .LocationConstraint)"
export PUBLIC_LOG_S3="s3://${PUBLIC_LOGS_BUCKET}/${BUILD_IDENTITY}-${_RANDOM}.html"
export PUBLIC_LOG_URL="https://${PUBLIC_LOGS_BUCKET}.s3.${_REGION}.amazonaws.com/${BUILD_IDENTITY}-${_RANDOM}.html"


# Make sure the GitHub app key is a file.
if [ ! -f "${GITHUB_APP_PRIVATE_KEY}" ]; then
    key_file=$(mktemp -u)
    echo "$GITHUB_APP_PRIVATE_KEY" > $key_file
    export GITHUB_APP_PRIVATE_KEY=$key_file
fi

export WORKDIR=/workdir
mkdir -p $WORKDIR
cd $WORKDIR

# Download and export the CRTM binary files tarball. This is done before the
# first use of git credentials to prevent the download from allowing a
# credential timeout during the ecbuild step.
export CRTM_BINARY_FILES_TARBALL="${WORKDIR}/crtmm_coeffs.tgz"
wget --no-verbose -O get_crtm_tarball.sh https://raw.githubusercontent.com/JCSDA/CRTMv3/refs/heads/develop/Get_CRTM_Binary_Files.sh
chmod +x get_crtm_tarball.sh
./get_crtm_tarball.sh -d "${CRTM_BINARY_FILES_TARBALL}"

# Configure git credentials
git config --global core.askPass /bin/git_askPass_app_credentials.py

if [ -n "${CI_REPOSITORY_BRANCH}" ]; then
    git clone -b $CI_REPOSITORY_BRANCH https://github.com/JCSDA-internal/CI.git
else
    git clone https://github.com/JCSDA-internal/CI.git
fi


echo "Starting tests."
./CI/$TEST_SCRIPT 2>&1 | tee >(python -m ansi2html -l > /tmp/build_logs.html)

set -x
aws s3 cp /tmp/build_logs.html $PUBLIC_LOG_S3 --content-type "text/html"
df -h
