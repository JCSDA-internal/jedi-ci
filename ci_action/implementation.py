"""Webhook implementation for Github"""

import os
import json
import base64
import logging
import yaml
import concurrent.futures
import email
import gzip
import boto3
import botocore
import botocore.session
import random
import time
import subprocess
import zipfile
import shutil

from ci_action.library import aws_client
from ci_action.library import cmake_rewrite
from ci_action.library import github_client
from ci_action.library import pr_resolve

import pprint

BUILD_ENVIRONMENTS = ['gcc', 'intel', 'gcc11']

LOG = logging.getLogger("implementation")

BUILD_CACHE_BUCKET = os.environ.get('BUILD_CACHE_BUCKET', 'jcsda-usaf-ci-build-cache')

class TimeCheckpointer:
    def __init__(self):
        self._checkpoint_time = time.time()

    def checkpoint(self):
        """Get elapsed time in seconds."""
        time_now = time.time()
        checkpoint_delta = round(time_now - self._checkpoint_time, 4)
        self._checkpoint_time = time_now
        return f'<time elapsed: {checkpoint_delta} seconds>'

def check_output(args, **kwargs):
    """
    Wrapper around subprocess.check_output that logs the command and its output.
    """
    LOG.info(f"Running command: {' '.join(args)}")
    return subprocess.check_output(args, **kwargs)


def upload_to_aws(bucket_name, s3_client, tarball_path, s3_file):
    """Upload file to S3 bucket"""
    if isinstance(json_data, str):
        json_data = json_data.encode('utf-8')
    with open(tarball_path, 'rb') as f:
        s3_client.put_object(Body=f, Bucket=bucket_name, Key=s3_file)
    s3_path = f's3://{bucket_name}/{s3_file}'
    return s3_path


def get_ci_config(target_repo_path):
    """Get the CI config from the target repository."""
    # get the CI config yaml from the target repository
    ci_config_path = os.path.join(target_repo_path, 'jedi-ci.yaml')
    if not os.path.exists(ci_config_path):
        raise EnvironmentError(f"jedi-ci.yaml not found in {target_repo_path}")
    # Open and parse the CI config yaml
    with open(ci_config_path, 'r') as f:
        ci_config = yaml.safe_load(f)

    # Validate required fields.
    required_fields = ['bundle_repository', 'bundle_branch', 'test_script', 'name', 'test_tag', 'bundle_name']
    for field in required_fields:
        if field not in ci_config:
            raise ValueError(f"Required field {field} not found in {ci_config_path}")

    # Validate optional fields.
    optional_field_defaults = {
        'unittest': [],
    }
    for field, default in optional_field_defaults.items():
        if field not in ci_config:
            ci_config[field] = default
    return ci_config


def get_environment_config():
    """Use the environment variable to get the environment config."""
    repository = os.environ.get('GITHUB_REPOSITORY')
    owner, repo_name = repository.split('/')
    github_event_path = os.environ.get('GITHUB_EVENT_PATH')
    with open(github_event_path, 'r') as f:
        event = json.load(f)

    if event.get('pull_request'):
        branch_name = event['pull_request']['head']['ref']
        pull_request_number = event['pull_request'].get('number', -1)
        pr_payload = event['pull_request']
        trigger_commit = event['pull_request'].get('head', {}).get('sha', '')
    else:
        raise ValueError(f'No pull request found in event; {event}')

    config = {
        'repository': repository,
        'owner': owner,
        'repo_name': repo_name,
        'clone_url': f'https://github.com/{repository}.git',
        'github_event_path': github_event_path,
        'branch_name': branch_name,
        'pull_request_number': pull_request_number,
        'pr_payload': pr_payload,
        'trigger_commit': trigger_commit,
    }
    return config


def prepare_and_launch_ci_test(environment_config, ci_config, bundle_repo_path, target_repo_path):
    """The main function that will be called to prepare and launch the CI test.
    
    This is similar to the process_event function, which was used by the lambda
    CI actuator but has been adapted for the Github-based Action CI.
    """
    # Use got to clone the bundle repository into the bundle_repo_path using
    # bundle_repository and bundle_branch
    if not os.path.exists(bundle_repo_path):
        LOG.info(f"Cloning bundle repository into {bundle_repo_path}")
        check_output(['git', 'clone', '--branch', ci_config['bundle_branch'], ci_config['bundle_repository'], bundle_repo_path])

    # Fetch config from the pull request data
    test_annotations = pr_resolve.read_test_annotations(
        repo_uri=ci_config['bundle_repository'],
        pr_number=environment_config['pull_request_number'],
        pr_payload=environment_config['pr_payload'],
        testmode=ci_config.get('test_mode', None) == 'SELF_TEST_JEDI_CI',
    )
    LOG.info(f'test_annotations:')
    annotations_pretty = pprint.pformat(test_annotations)
    LOG.info(annotations_pretty)

    repo_to_commit_hash = pr_resolve.gather_build_group_hashes(test_annotations.build_group_map)
    print('printing repo_to_commit_hash')
    repo_to_commit_hash_pretty = pprint.pformat(repo_to_commit_hash)
    LOG.info(repo_to_commit_hash_pretty)

    # Import the bundle file
    bundle_file = os.path.join(bundle_repo_path, 'CMakeLists.txt')
    bundle_file_unittest = bundle_file
    bundle_original = os.path.join(bundle_repo_path, 'CMakeLists.txt.original')
    bundle_integration = os.path.join(bundle_repo_path, 'CMakeLists.txt.integration')
    with open(bundle_file, 'r') as f:
        bundle = cmake_rewrite.CMakeFile(f.read())

    # Move the original bundle file to the original file.
    shutil.move(bundle_file, bundle_original)

    # Rewrite the bundle cmake file twice
    # First, rewrite the unit test bundle file with the build group commit hashes
    with open(bundle_file_unittest, 'w') as f:
        bundle.rewrite_build_group_whitelist(
            file_object=f,
            enabled_bundles=ci_config['unittest'],
            build_group_commit_map=repo_to_commit_hash,
        )

    # Create an integration test file.
    with open(bundle_integration, 'w') as f:
        bundle.rewrite_build_group_blacklist(
            file_object=f,
            disabled_bundles=set(),
            build_group_commit_map=repo_to_commit_hash,
        )

    # Add resources to the bundle by copying all files in /app/shell to jedi_ci_resources
    check_output(['mkdir', '-p', os.path.join(bundle_repo_path, 'jedi_ci_resources')])
    shutil.copytree('/app/shell', os.path.join(bundle_repo_path, 'jedi_ci_resources'))

    # Create a tarball  the new bundle (with test resources).
    LOG.info(f"Creating bundle.tar.gz from {bundle_repo_path}")
    bundle_tarball = "bundle.tar.gz"

    # Use tar to create a gzipped tarball of the bundle repository
    check_output(['tar', '-czf', bundle_tarball, '-C', os.path.dirname(bundle_repo_path), os.path.basename(bundle_repo_path)])
    LOG.info(f"Created bundle tarball at {bundle_tarball}")

    # Upload the bundle to S3.
    s3_file = f'ci_action_bundles/{environment_config["repository"]}/{environment_config["pull_request_number"]}-{environment_config["trigger_commit"]}-{ci_config["bundle_name"]}.tar.gz'
    s3_client = boto3.client('s3')
    s3_path = upload_to_aws(BUILD_CACHE_BUCKET, s3_client, bundle_tarball, s3_file)

    # Launch the test
    if test_select == 'random':
        chosen_build_environments = [random.choice(BUILD_ENVIRONMENTS)]
    elif test_select == 'all':
        chosen_build_environments = [e for e in BUILD_ENVIRONMENTS]
    else:
        chosen_build_environments = [test_select]

    # Write test lock file
    # TODO: this will not be included in first pass.

    # check the lock file and cancel old jobs for PR
    # TODO: this will not be included in first pass.

    # write the test github check runs to the PR.
     for build_environment in chosen_build_environments:
        checkrun_id_map = github_client.create_check_runs(
            build_environment,
            environment_config['repo_name'],
            environment_config['owner'],
            environment_config['trigger_commit'],
            test_annotations.next_ci_suffix)

        # Note checkrun_id_map is dict {'unit': unit_run.id, 'integration': integration_run.id}
        job = aws_client.submit_test_batch_job(
            config=batch_submit_env.get_config(build_environment + build_env_suffix),
            repo_name=repo_name,
            commit=short_commit,
            pr=pull_request_number,
            test_script=test_script,
            build_identity=f'{repo_name}-{pull_request_number}-{short_commit}-{build_environment}',
            debug_time=debug_time,
            build_info=build_info_b64,
        )
        job_arn = job['jobArn']
        LOG.info(f'Submitted Batch Job: "{job_arn}". {timer.checkpoint()}')
