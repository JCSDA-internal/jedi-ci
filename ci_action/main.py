#!/usr/bin/env python3

import sys
import subprocess
import logging
import pathlib
import argparse

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
LOG = logging.getLogger("entrypoint")

def check_output(args, **kwargs):
    """
    Wrapper around subprocess.check_output that logs the command and its output.
    """
    LOG.info(f"Running command: {' '.join(args)}")
    return subprocess.check_output(args, **kwargs)

def setup_git_credentials(github_token):
    """
    Setup Git credentials using the JEDI_CI_TOKEN environment variable.
    Configures git credential store and writes credentials to file.
    """
    # No exception handling here, workflow must fail if this step fails.
    if github_token:
        LOG.info("JEDI_CI_TOKEN is set. Setting up Git credentials.")

        # Configure git to use the credential store
        result = check_output(
            ["git", "config", "--global", "credential.helper", "store"],
        )
        # Write the ~/.git-credentials file
        credentials_file = pathlib.Path.home() / ".git-credentials"
        with open(credentials_file, 'w') as f:
            f.write(f"https://x-access-token:{github_token}@github.com")
        credentials_file.chmod(0o600)
        LOG.info(f"Git credentials written to {credentials_file}")

    else:
        LOG.info("GITHUB_TOKEN is not set. Git operations may require authentication.")


def main():
    """This function is the entrypoint for the CI action, it gets all configuration
    information and then calls the prepare_and_launch_ci_test function.
    """
    # Parse command line arguments
    parser = argparse.ArgumentParser(description='JEDI CI Action')
    parser.add_argument('--noop', action='store_true', default=False,
                       help='No-op mode - exit immediately if set')
    # DO NOT SUBMIT: this must default to False before submission.
    parser.add_argument('--environment_query', action='store_true', default=False,
                       help='Similar to --noop, but will show the environment config')
    args = parser.parse_args()

    if args.noop:
        LOG.info("No-op flag set, exiting successfully")
        return 0

    return 0

if __name__ == "__main__":
    sys.exit(main()) 