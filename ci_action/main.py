#!/usr/bin/env python3
"""
This is the entrypoint for the JEDI CI action, it is responsible for
verifying and fetching the environment and for invoking the main
implementation of the CI action.
"""

import argparse
import datetime
import json
import logging
import os
import pathlib
import pprint
import subprocess
import sys

from ci_action import implementation as ci_implementation


# This configuration is used to store references to AWS resources
# specific to our cloud formation stack.
JEDI_CI_INFRA_CONFIG = {
    # ARN of the AWS Batch job queue.
    'batch_queue': 'arn:aws:batch:us-east-2:747101682576:job-queue/JobQueue-wnYIFmaQpfwKNZuw',
    # Map of build environment name to job definition name.
    'batch_job_name_map': {
        'gcc11': 'jedi-ci-action-gcc11',
        'gcc11-next': 'jedi-ci-action-gcc11',
        'gcc': 'jedi-ci-action-gcc',
        'gcc-next': 'jedi-ci-action-gcc-next',
        'intel': 'jedi-ci-action-intel',
        'intel-next': 'jedi-ci-action-intel-next',
    }
}

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
        check_output(
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


def get_environment_config():
    """Pull config data from GitHub action environment.

    The github action environment is set by the GitHub action runner
    and includes pull request and push event data in a json file
    that is read here. The action also contains environment variables
    set by the runner configuration yaml.
    """
    repository = os.environ.get('GITHUB_REPOSITORY')
    if not repository:
        raise ValueError("GITHUB_REPOSITORY environment variable is required")
    owner, repo_name = repository.split('/')
    github_event_path = os.environ.get('GITHUB_EVENT_PATH')
    with open(github_event_path, 'r') as f:
        event = json.load(f)

    event_name = os.environ.get('GITHUB_EVENT_NAME', '')
    if event.get('pull_request'):
        branch_name = event['pull_request']['head']['ref']
        pull_request_number = event['pull_request'].get('number', -1)
        pr_payload = event['pull_request']
        trigger_commit = event['pull_request'].get('head', {}).get('sha', '')
        build_id_name = f'{pull_request_number}-{trigger_commit[:7]}'
        is_scheduled = False
    elif event_name in ['schedule', 'workflow_dispatch']:
        branch_name = os.environ.get('GITHUB_REF_NAME', 'develop')
        pull_request_number = 0
        pr_payload = None
        trigger_commit = os.environ.get('GITHUB_SHA', '')
        is_scheduled = True
        if not trigger_commit:
            raise ValueError(
                'GITHUB_SHA is required for scheduled runs but was not set')
        timestamp = datetime.datetime.now(datetime.timezone.utc).strftime('%H%M')
        build_id_name = f'{branch_name}-{timestamp}'
    else:
        raise ValueError(
            f'Unsupported event: expected pull_request or schedule, '
            f'got "{event_name}"; payload keys: {list(event.keys())}')

    default_bundle_branch = os.environ.get('BUNDLE_BRANCH', 'develop')
    bundle_repository = os.environ.get(
        'BUNDLE_REPOSITORY', 'https://github.com/JCSDA-internal/jedi-bundle.git')
    test_tag = os.environ.get('UNITTEST_TAG', '')
    self_test = os.environ.get('CI_SELF_TEST', 'false').lower() == 'true'
    test_script = os.environ.get('TEST_SCRIPT', 'run_tests.sh')

    # Get the target project name. If not passed explicitly, use the repo name.
    target_project_name = os.environ.get('TARGET_PROJECT_NAME', '')
    if not target_project_name.strip():
        target_project_name = repo_name

    # Get the build cache bucket.
    build_cache_bucket = os.environ.get('BUILD_CACHE_BUCKET', 'jcsda-usaf-ci-build-cache')

    if repository == 'JCSDA-internal/jedi-bundle':
        LOG.info(f'Overriding default branch "{default_bundle_branch}" with "{branch_name}"')
        default_bundle_branch = branch_name

    config = {
        'is_scheduled': is_scheduled,
        'repository': repository,
        'owner': owner,
        'repo_name': repo_name,
        'clone_url': f'https://github.com/{repository}.git',
        'github_event_path': github_event_path,
        'branch_name': branch_name,
        'pull_request_number': pull_request_number,
        'build_id_name': build_id_name,
        'pr_payload': pr_payload,
        'trigger_commit': trigger_commit,
        'trigger_commit_short': trigger_commit[:7],
        'bundle_branch': default_bundle_branch,
        'bundle_repository': bundle_repository,
        'self_test': self_test,
        'unittest_tag': test_tag,
        'test_script': test_script,
        'target_project_name': target_project_name,
        'build_cache_bucket': build_cache_bucket
    }
    LOG.info(f'config\n:{pprint.pformat(config)}')
    return config


def main():
    """This function is the entrypoint for the CI action, it gets all configuration
    information and then calls the prepare_and_launch_ci_test function.
    """
    # Parse command line arguments
    parser = argparse.ArgumentParser(description='JEDI CI Action')
    parser.add_argument('--noop', action='store_true', default=False,
                        help='No-op mode - exit immediately if set')
    args = parser.parse_args()

    if args.noop:
        LOG.info("No-op flag set, exiting successfully")
        return 0

    workspace_dir = os.environ.get('GITHUB_WORKSPACE', os.getcwd())

    # Get environment attributes set by GitHub.
    env_config = get_environment_config()

    # Setup Git credentials before doing anything else
    setup_git_credentials(os.environ.get('JEDI_CI_TOKEN'))

    # Prepare and launch the CI test
    errors, non_blocking_errors = ci_implementation.prepare_and_launch_ci_test(
        infra_config=JEDI_CI_INFRA_CONFIG,
        config=env_config,
        bundle_repo_path=os.path.join(workspace_dir, 'bundle'))

    # The following block is used to format a list of errors that will be logged to the GitHub
    # action log. The output should look something like this.
    # ...
    # Tests could not launch due to these errors:
    #  1. error message number one
    #  2. error message number two
    if errors:
        # Enumerate and indent each error message.
        enumerated_errors = [f' {i+1}. ' + str(e) for i, e in enumerate(errors)]
        error_list = '\n'.join(enumerated_errors)
        LOG.error(f"Tests could not launch due to these errors:\n{error_list}")
        return 1

    # Non-blocking errors note internal failures that must be addressed but that did not
    # prevent the tests from launching. We are still returning a failure code to the GitHub
    # action so that internal errors are surfaced and addressed. These errors are formatted
    # like the blocking errors but with a different message noting the succesful test launch.
    if non_blocking_errors:
        # Enumerate and indent each error message.
        enumerated_errors = [f' {i+1}. ' + str(e) for i, e in enumerate(non_blocking_errors)]
        error_list = '\n'.join(enumerated_errors)
        LOG.error(f"Tests launched successfully but experienced errors:\n{error_list}")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
