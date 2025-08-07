"""Webhook implementation for Github"""

import os
import logging
import yaml
import boto3
import random
import time
import subprocess
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
    with open(tarball_path, 'rb') as f:
        s3_client.put_object(Body=f, Bucket=bucket_name, Key=s3_file)
    s3_path = f's3://{bucket_name}/{s3_file}'
    return s3_path


def get_ci_config(target_repo_path):
    """Get the CI config from the target repository.

    The CI config is a yaml file in the target repository that contains the
    configuration for the CI test. This is used to set the bundle build
    properties, test tags, and other cmake and build properties.
    """
    # get the CI config yaml from the target repository
    ci_config_path = os.path.join(target_repo_path, 'jedi-ci.yaml')
    if not os.path.exists(ci_config_path):
        raise FileNotFoundError(f"jedi-ci.yaml not found in {target_repo_path}")
    # Open and parse the CI config yaml
    with open(ci_config_path, 'r') as f:
        ci_config = yaml.safe_load(f)

    # Validate required fields.
    required_fields = [
        'bundle_repository', 'bundle_branch', 'test_script',
        'name', 'test_tag', 'bundle_name', 'uri'
    ]
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


def prepare_and_launch_ci_test(
    infra_config,
    environment_config,
    ci_config,
    bundle_repo_path,
    target_repo_path,
):
    """The main function that will be called to prepare and launch the CI test.

    This is similar to the process_event function, which was used by the lambda
    CI actuator but has been adapted for the Github-based Action CI.

    Args:
        infra_config: The infrastructure configuration for the CI test, pulled
                      from the cloud formation application resources.
        environment_config: The GitHub action environment configuration including
                            PR metadata and passed config variables.
        ci_config: The CI configuration for the bundle configuration and cmake build.
        bundle_repo_path: The path to the bundle repository.
        target_repo_path: The path to the target repository.

    Returns:
        A list of errors that occurred during the test launch. Any potentially
        recoverable errors should be returned as a list of strings so that the
        action can fail (notifying us of an issue) even if part of the test
        launches successfully.
    """
    non_blocking_errors = []

    # Use got to clone the bundle repository into the bundle_repo_path using
    # bundle_repository and bundle_branch
    timer = TimeCheckpointer()
    if not os.path.exists(bundle_repo_path):
        LOG.info(f"Cloning bundle repository into {bundle_repo_path}")
        check_output([
            'git', 'clone', '--branch', ci_config['bundle_branch'],
            ci_config['bundle_repository'], bundle_repo_path
        ])

    # Fetch config from the pull request data
    test_annotations = pr_resolve.read_test_annotations(
        repo_uri=ci_config['uri'],
        pr_number=environment_config['pull_request_number'],
        pr_payload=environment_config['pr_payload'],
        testmode=ci_config.get('test_mode', None) == 'SELF_TEST_JEDI_CI',
    )
    LOG.info('test_annotations:')
    annotations_pretty = pprint.pformat(test_annotations)
    LOG.info(f'{timer.checkpoint()}\n{annotations_pretty}')

    repo_to_commit_hash = pr_resolve.gather_build_group_hashes(
        test_annotations.build_group_map
    )
    repo_to_commit_hash_pretty = pprint.pformat(repo_to_commit_hash)
    LOG.info(
        f'{timer.checkpoint()}\nrepo_to_commit_hash:\n{repo_to_commit_hash_pretty}'
    )

    # Import the bundle file
    bundle_file = os.path.join(bundle_repo_path, 'CMakeLists.txt')
    bundle_file_unittest = bundle_file
    bundle_original = os.path.join(
        bundle_repo_path, 'CMakeLists.txt.original'
    )
    bundle_integration = os.path.join(
        bundle_repo_path, 'CMakeLists.txt.integration'
    )
    with open(bundle_file, 'r') as f:
        bundle = cmake_rewrite.CMakeFile(f.read())

    # Move the original bundle file to the original file.
    shutil.move(bundle_file, bundle_original)

    # Rewrite the bundle cmake file twice
    # First, rewrite the unit test bundle file with the build group commit hashes
    with open(bundle_file_unittest, 'w') as f:
        bundle.rewrite_build_group_whitelist(
            file_object=f,
            enabled_bundles=set(ci_config.get('unittest', []) + [environment_config['repo_name']]),
            build_group_commit_map=repo_to_commit_hash,
        )

    # Create an integration test file.
    with open(bundle_integration, 'w') as f:
        bundle.rewrite_build_group_blacklist(
            file_object=f,
            disabled_bundles=set(),
            build_group_commit_map=repo_to_commit_hash,
        )
    LOG.info(f'{timer.checkpoint()}\n Rewrote bundle for build groups.')

    # Add resources to the bundle by copying all files in /app/shell to jedi_ci_resources
    shutil.copytree(
        '/app/shell', os.path.join(bundle_repo_path, 'jedi_ci_resources')
    )

    # Create a tarball  the new bundle (with test resources).
    LOG.info(f"Creating bundle.tar.gz from {bundle_repo_path}")
    bundle_tarball = "bundle.tar.gz"
    check_output([
        'tar', '-czf', bundle_tarball, '-C', os.path.dirname(bundle_repo_path),
        os.path.basename(bundle_repo_path)
    ])
    LOG.info(f"{timer.checkpoint()}\nCreated bundle tarball at {bundle_tarball}")

    # Upload the bundle to S3.
    s3_file = (
        f'ci_action_bundles/{environment_config["repository"]}/'
        f'{environment_config["pull_request_number"]}-'
        f'{environment_config["trigger_commit"]}-{ci_config["bundle_name"]}.tar.gz'
    )
    s3_client = boto3.client('s3')
    configured_bundle_tarball_s3_path = upload_to_aws(
        BUILD_CACHE_BUCKET, s3_client, bundle_tarball, s3_file
    )

    # Launch the test
    test_select = test_annotations.test_select
    if test_select == 'random':
        chosen_build_environments = [random.choice(BUILD_ENVIRONMENTS)]
    elif test_select == 'all':
        chosen_build_environments = [e for e in BUILD_ENVIRONMENTS]
    else:
        chosen_build_environments = [test_select]

    # Cancel prior unfinished tests jobs for the PR to save compute resources.
    #try:
    aws_client.cancel_prior_batch_jobs(
        job_queue=infra_config['batch_queue'],
        repo_name=environment_config['repo_name'],
        pr=environment_config["pull_request_number"],
    )
    #except Exception as e:
    #    non_blocking_errors.append(f"Error cancelling prior batch jobs: {e}")

    # Update GitHub check runs to reflect the new test selection.
    #try:
    github_client.cancel_prior_unfinished_check_runs(
        repo=environment_config['repo_name'],
        owner=environment_config['owner'],
        pr_number=environment_config["pull_request_number"],
    )
    #except Exception as e:
    #    non_blocking_errors.append(f"Error cancelling prior check runs: {e}")

    # This is a constructor for the configuration needed to submit AWS Batch jobs.
    # This constructor reads configuration from the environment and must be
    # configured via environmental variables set in the Lambda function. Note that
    # the timeout is set to is 4 hours since even a full cache rebuild should much
    # less time. For information on the config variables.
    batch_config_builder = aws_client.BatchSubmitConfigBuilder(
        job_name_map=infra_config['batch_job_name_map'],
        job_queue=infra_config['batch_queue'],
        timeout=60 * 240
    )

    # write the test github check runs to the PR.
    for build_environment in chosen_build_environments:
        checkrun_id_map = github_client.create_check_runs(
            build_environment,
            environment_config['repo_name'],
            environment_config['owner'],
            environment_config['trigger_commit'],
            test_annotations.next_ci_suffix)
        LOG.info(f'{timer.checkpoint()}\nCreated check runs for {build_environment}.')

        # Note checkrun_id_map is dict {'unit': unit_run.id, 'integration': integration_run.id}
        debug_time = 60 * 30 if test_annotations.debug_mode else 0
        build_identity = (
            f'{environment_config["repo_name"]}-'
            f'{environment_config["pull_request_number"]}-'
            f'{environment_config["trigger_commit_short"]}-{build_environment}'
        )
        repo_name_full = (
            f'{environment_config["owner"]}/{environment_config["repo_name"]}'
        )

        job = aws_client.submit_test_batch_job(
            config=batch_config_builder.get_config(
                build_environment + test_annotations.next_ci_suffix
            ),
            repo_name=environment_config['repo_name'],
            repo_name_full=repo_name_full,
            commit=environment_config['trigger_commit_short'],
            pr=environment_config['pull_request_number'],
            configured_bundle_tarball=configured_bundle_tarball_s3_path,
            debug_time_seconds=debug_time,
            build_identity=build_identity,
            unittest_tag=ci_config['test_tag'],
            trigger_sha=environment_config['trigger_commit'],
            trigger_pr=str(environment_config['pull_request_number']),
            integration_run_id=checkrun_id_map['integration'],
            unit_run_id=checkrun_id_map['unit'],
        )
        job_arn = job['jobArn']
        LOG.info(
            f'{timer.checkpoint()}\nSubmitted Batch Job for build environment '
            f'{build_environment}: "{job_arn}".'
        )
    return non_blocking_errors
