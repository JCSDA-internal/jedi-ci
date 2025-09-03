"""Webhook implementation for Github"""

import boto3
import concurrent.futures
import logging
import os
import random
import shutil
import subprocess
import time

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


def prepare_and_launch_ci_test(
    infra_config,
    config,
    bundle_repo_path,
    target_repo_path,
):
    """The main function that will be called to prepare and launch the CI test.

    This is similar to the process_event function, which was used by the lambda
    CI actuator but has been adapted for the Github-based Action CI.

    Args:
        infra_config: The infrastructure configuration for the CI test, pulled
                      from the cloud formation application resources.
        config: The GitHub action environment configuration including
                            PR metadata and passed config variables.
        bundle_repo_path: The path to the bundle repository.
        target_repo_path: The path to the target repository.

    Returns:
        A 2-tuple of lists of strings representing errors:
        - blocking_errors: Errors that preventing the tests from launching.
        - non_blocking_errors Any potentially recoverable errors that may
            have occurred during the test launch but did not prevent the test
            jobs from launching. These errors will be logged as non-blocking
    """
    # Some cleanup and housekeeping operations should not block the test launch
    # but should be logged as non-blocking errors so the action can fail (notifying
    # us of an issue).
    non_blocking_errors = []
    # Errors that prevent the tests from launching (e.g. misconfigured test annotations
    # or pr groups that don't exist).
    blocking_errors = []

    timer = TimeCheckpointer()  # Timer for logging.

    # Fetch config from the pull request data
    repo_uri = f'https://github.com/{config["owner"]}/{config["repo_name"]}.git'
    try:
        test_annotations = pr_resolve.read_test_annotations(
            repo_uri=repo_uri,
            pr_number=config['pull_request_number'],
            pr_payload=config['pr_payload'],
            testmode=config['self_test'],
        )
    except pr_resolve.Exception as e:
        blocking_errors.append(f"Error reading test annotations: {e}")
        return blocking_errors, non_blocking_errors

    LOG.info('test_annotations:')
    annotations_pretty = pprint.pformat(test_annotations._asdict())
    LOG.info(f'{timer.checkpoint()}\n{annotations_pretty}')

    # Check draft PR run status.
    if config.get('pr_payload', {}).get('draft') and not test_annotations.run_on_draft:
        LOG.info('\n\nTests are not launched for draft PRs by default.\n'
                 'To enable testing on draft PRs, add the following annotation to the PR:\n'
                 '```\n'
                 'run-ci-on-draft = true\n'
                 '```\n')
        return blocking_errors, non_blocking_errors

    bundle_branch = config['bundle_branch']  # This is the default branch to use for the bundle.
    if test_annotations.jedi_bundle_branch:
        bundle_branch = test_annotations.jedi_bundle_branch  # Override based on PR annotations.

    # git clone the bundle repository into `bundle_repo_path`
    if not os.path.exists(bundle_repo_path):
        LOG.info(f"Cloning \"{config['bundle_repository']}@{bundle_branch}\"")
        check_output([
            'git', 'clone', '--branch', bundle_branch,
            config['bundle_repository'], bundle_repo_path
        ])

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

    stage_1_dependencies = config['build_stages'][0]
    stage_2_dependencies = config['build_stages'][1]

    # Rewrite the bundle cmake file with the selected projects for the first build stage.
    with open(bundle_file_unittest, 'w') as f:
        bundle.rewrite_build_group_whitelist(
            file_object=f,
            enabled_bundles=stage_1_dependencies,
            build_group_commit_map=repo_to_commit_hash,
        )
        LOG.info(f'{timer.checkpoint()}\n Wrote CMakeLists file with '
                 f'bundles: {stage_1_dependencies}.')

    # Create an integration test bundle definition if necessary.
    if stage_2_dependencies == 'all':
        with open(bundle_integration, 'w') as f:
            bundle.rewrite_build_group_blacklist(
                file_object=f,
                disabled_bundles=set(),
                build_group_commit_map=repo_to_commit_hash,
            )
            LOG.info(f'{timer.checkpoint()}\n Wrote second CMakeLists file with bundles.')
    elif stage_2_dependencies:
        with open(bundle_integration, 'w') as f:
            bundle.rewrite_build_group_whitelist(
                file_object=f,
                enabled_bundles=stage_2_dependencies,
                build_group_commit_map=repo_to_commit_hash,
            )
            LOG.info(f'{timer.checkpoint()}\n Wrote second CMakeLists file '
                     f'with selected bundles: {stage_2_dependencies}.')
    else:
        LOG.info(f'{timer.checkpoint()}\n No second CMakeLists file written.')

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
        f'ci_action_bundles/{config["repository"]}/'
        f'{config["pull_request_number"]}-'
        f'{config["trigger_commit"]}-bundle.tar.gz'
    )
    s3_client = boto3.client('s3')
    configured_bundle_tarball_s3_path = upload_to_aws(
        BUILD_CACHE_BUCKET, s3_client, bundle_tarball, s3_file
    )

    # Select the build environments to test.
    test_select = test_annotations.test_select
    if test_select == 'random':
        chosen_build_environments = [random.choice(BUILD_ENVIRONMENTS)]
    elif test_select == 'all':
        chosen_build_environments = [e for e in BUILD_ENVIRONMENTS]
    else:
        chosen_build_environments = [test_select]

    # Use a thread pool to cancel prior unfinished jobs and their associated check runs.
    # This process is done in parallel to save time on slow network-bound operations.
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:

        # Submit operation: cancel prior unfinished AWS Batch jobs for the PR.
        cxl_batch_future = executor.submit(
            aws_client.cancel_prior_batch_jobs,
            job_queue=infra_config['batch_queue'],
            repo_name=config['repo_name'],
            pr=config["pull_request_number"],
        )

        # Submit operation: cancel unfinished check runs for the PR.
        cxl_checkrun_future = executor.submit(
            github_client.cancel_prior_unfinished_check_runs,
            repo=config['repo_name'],
            owner=config['owner'],
            pr_number=config["pull_request_number"],
        )

        # Wait for the cancel operations to complete.
        for future in concurrent.futures.as_completed([cxl_batch_future, cxl_checkrun_future]):
            try:
                future.result()
            except Exception as e:
                if future is cxl_batch_future:
                    non_blocking_errors.append(f"Error cancelling prior batch jobs: {e}")
                else:
                    non_blocking_errors.append(f"Error cancelling prior check runs: {e}")

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
            config['repo_name'],
            config['owner'],
            config['trigger_commit'],
            test_annotations.next_ci_suffix)
        LOG.info(f'{timer.checkpoint()}\nCreated check runs for {build_environment}.')

        # Note checkrun_id_map is dict {'unit': unit_run.id, 'integration': integration_run.id}
        debug_time = 60 * 30 if test_annotations.debug_mode else 0
        build_identity = (
            f'{config["repo_name"]}-'
            f'{config["pull_request_number"]}-'
            f'{config["trigger_commit_short"]}-{build_environment}'
        )
        repo_name_full = (
            f'{config["owner"]}/{config["repo_name"]}'
        )

        job = aws_client.submit_test_batch_job(
            config=batch_config_builder.get_config(
                build_environment + test_annotations.next_ci_suffix
            ),
            repo_name=config['repo_name'],
            repo_name_full=repo_name_full,
            commit=config['trigger_commit_short'],
            pr=config['pull_request_number'],
            configured_bundle_tarball=configured_bundle_tarball_s3_path,
            debug_time_seconds=debug_time,
            build_identity=build_identity,
            unittest_tag=config['unittest_tag'],
            trigger_sha=config['trigger_commit'],
            trigger_pr=str(config['pull_request_number']),
            integration_run_id=checkrun_id_map['integration'],
            unit_run_id=checkrun_id_map['unit'],
            unittest_dependencies=' '.join(stage_1_dependencies),
            test_script=config['test_script'],
        )
        job_arn = job['jobArn']
        LOG.info(
            f'{timer.checkpoint()}\nSubmitted Batch Job for build environment '
            f'{build_environment}: "{job_arn}".'
        )
    return blocking_errors, non_blocking_errors
