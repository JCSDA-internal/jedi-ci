#!/usr/bin/env python3

import os
import sys
import subprocess
import logging
import yaml
from github import Github, GithubIntegration
from datetime import datetime
import pathlib
import pprint
from ci_action import implementation as ci_implementation
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

def list_directory(directory_path):
    """
    Log the contents of the specified directory
    """
    LOG.info(f"Files in directory: {directory_path}")
    try:
        files = os.listdir(directory_path)
        for file in sorted(files):
            LOG.info(f"  {file}")
    except Exception as e:
        LOG.info(f"Error listing files in {directory_path}: {str(e)}")

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

    if args.environment_query:
        for key, value in os.environ.items():
            LOG.info(f"{key}: {value}")
        current_dir = os.getcwd()
        LOG.info(f"Current directory: {current_dir}")
        list_directory(current_dir)
        return 0

    workspace_dir = os.environ.get('GITHUB_WORKSPACE', os.getcwd())
    target_repo_full_path = os.path.join(
        workspace_dir, os.environ['TARGET_REPO_DIR'])

    # Get the CI config from the target repository which must have
    # been cloned into the github workspace directory.
    ci_config = ci_implementation.get_ci_config(target_repo_full_path)
    LOG.info(f"ci config:")
    pprint.pprint(ci_config)

    # Get environment attributes set by GitHub.
    env_config = ci_implementation.get_environment_config()
    LOG.info(f"Environment config:")
    pprint.pprint(env_config)

    # Setup Git credentials before doing anything else
    setup_git_credentials(os.environ.get('JEDI_CI_TOKEN'))

    # Prepare and launch the CI test
    prepare_and_launch_ci_test(
        environment_config=env_config,
        ci_config=ci_config,
        bundle_repo_path=os.path.join(workspace_dir, 'bundle'),
        target_repo_path=target_repo_full_path)
   
    
    return 0

if __name__ == "__main__":
    sys.exit(main()) 