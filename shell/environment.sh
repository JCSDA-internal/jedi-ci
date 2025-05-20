#!/usr/bin/env bash

# This script is used to set environmental variables and create an environment
# used by the CI system. It should be sourced into the CI runner script or
# into the shell environment of someone looking to replicate a CI run.

export CDASH_URL="https://cdash.jcsda.org"
export SKIP_GITHUB_CHECK_RUNS='no'

# ecbuild expects this to be set; various build templates are stored here.
export jedi_cmake_ROOT=/opt/view

# Additional exports for running OpenMPI MPI jobs as root
# and with more resources than available
export OMPI_ALLOW_RUN_AS_ROOT=1
export OMPI_ALLOW_RUN_AS_ROOT_CONFIRM=1
export OMPI_MCA_rmaps_base_oversubscribe=1

# This timestamp is used to back-date source files to ensure that the later
# `make` command sees the binary cache files as "fresh" and does not rebuild
# the binaries. This procedure is necessary because tar archives preserve
# modification date, so binaries are dated to the time they were archived.
# Conversely, git does not preserve modification date, so source repositories
# are dated to the time they are cloned.
export SOURCE_BACKDATE_TIMESTAMP=$(date "+2020-%m-%dT%H:%M:%S")

# Create directory structure for tests and saving important locations. Note
# the jedi-bundle directory will be created by a later `git clone` command.
echo "Setup testing folder structure and cloning jedi-bundle."
if [ -z "$WORKDIR" ]; then
    export WORKDIR="/workdir"
fi
if [ ! -d "${WORKDIR}" ]; then
    mkdir -p "${WORKDIR}"
fi
mkdir "${WORKDIR}/test_root"
mkdir "${WORKDIR}/test_root/build"
mkdir "${WORKDIR}/test_root/build/module"
export BUILD_DIR="${WORKDIR}/test_root/build"
export TEST_ROOT="${WORKDIR}/test_root"
export JEDI_BUNDLE_DIR="${WORKDIR}/test_root/jedi-bundle"
export CI_CODE_PATH=$WORKDIR/CI
export BUILD_PARALLELISM=5


# Set compiler-specific flags and options specific to different tool chains. The
# COMPILER_FLAGS array is used in the ecbuild invocation.
export COMPILER_FLAGS=( )
if [ $JEDI_COMPILER = "intel" ]; then
    source /opt/intel/oneapi/compiler/latest/env/vars.sh
    source /opt/intel/oneapi/mpi/latest/env/vars.sh
    # Compiling with -O2 is too slow and uses too much memory
    export COMPILER_FLAGS=( -DECBUILD_C_FLAGS_RELWITHDEBINFO=-O1 -DECBUILD_CXX_FLAGS_RELWITHDEBINFO=-O1 -DECBUILD_Fortran_FLAGS_RELWITHDEBINFO=-O1 )
    export BUILD_PARALLELISM=4
fi

if [ $JEDI_COMPILER = "clang" ]; then
    export CC=/usr/bin/clang
    export CXX=/usr/bin/clang++
fi

if [ $JEDI_COMPILER = "gcc" ] || [ $JEDI_COMPILER = "gcc11" ]; then
    export BUILD_PARALLELISM=4
fi


# Add compiler flags specific to individual repositories.
if [ $TRIGGER_REPO = "ufo" ]; then
  COMPILER_FLAGS+=( -DUFO_TEST_TIER=2 )
fi


# Make sure the private key is a file.
if [ -n "$GITHUB_APP_PRIVATE_KEY" ]; then
    if [ -f $GITHUB_APP_PRIVATE_KEY ]; then
        # This is the configuration used to test this build script.
        export GITHUB_APP_PRIVATE_KEY_FILE=$GITHUB_APP_PRIVATE_KEY
    else
        echo "writing private key to a file."
        export GITHUB_APP_PRIVATE_KEY_FILE=$(mktemp -u)
        echo "$GITHUB_APP_PRIVATE_KEY" > $GITHUB_APP_PRIVATE_KEY_FILE
    fi
else
    echo "No value for \$GITHUB_APP_PRIVATE_KEY; skipping app auth config"
fi

# https://github.com/JCSDA-internal/mpas-jedi/issues/919
rm -vf `find /opt/view/bin -iname '*esmf*'`
rm -vf `find /opt/view/lib -iname '*esmf*'`
rm -vf `find /opt/view/include -iname '*esmf*'`
rm -vf `find /opt/view/cmake -iname '*esmf*'`
