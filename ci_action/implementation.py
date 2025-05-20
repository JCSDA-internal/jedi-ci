"""Webhook implementation for Github"""

import os
import json
import base64
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
from ci_action.library import pr_resolve
from ci_action.library import github_client


BUILD_ENVIRONMENTS = ['gcc', 'intel', 'gcc11']


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


def upload_to_aws(s3_client, json_data, s3_file):
    """Upload file to S3 bucket"""
    if isinstance(json_data, str):
        json_data = json_data.encode('utf-8')
    bucket = os.environ['BUCKET_NAME']
    s3_client.put_object(Body=json_data, Bucket=bucket, Key=s3_file)
    s3_path = f's3://{bucket}/{s3_file}'
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
    
    This is similart to the process_event function, which was used by the lambda
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
    )
    pr_group_map = pr_resolve.get_build_group_pr_map(test_annotations['build_group'])

    # Import the bundle file
    bundle_file = os.path.join(bundle_repo_path, 'CMakeLists.txt')
    bundle_original = os.path.join(bundle_repo_path, 'CMakeLists.txt.original')
    bundle_integration = os.path.join(bundle_repo_path, 'CMakeLists.txt.integration')
    with open(bundle_file, 'r') as f:
        bundle = cmake_rewrite.CMakeFile(f.read())

    # Move the original bundle file to the original file.
    shutil.move(bundle_file, bundle_original)

    # Rewrite the bundle cmake file twice

    # Zip the new bundle.

    # Upload the bundle to S3.

    # Launch the test

    # Write test lock file

    # check the lock file and cancel old jobs for PR
    # TODO: this will not be included in first pass.

    # write the test github check runs to the PR.

    # close as success.





def process_event(event_config, ci_config):
    """Create file for S3 bucket"""
    timer = TimeCheckpointer()
    trigger_repo = event_config['repository']
    trigger_repo_uri = event_config['clone_url']
    owner = event_config['owner']
    repo_name = event_config['repo_name']
    pull_request_number = event_config['pull_request_number']
    trigger_commit = event_config['trigger_commit']

    # Set up clients.
    # This is a constructor for the configuration needed to submit AWS Batch jobs.
    batch_submit_env = aws_client.BatchSubmitConfigFromEnv(timeout=60*240)
    # S3 client is currently not used.
    #s3_client = boto3.client('s3')

    # vars that must be pulled from the event.
    branch_name = ''
    pull_request_number = -1
    pr_payload = None


    if pull_request_number <= 0 or not trigger_commit:
        print(f'Found no associated PR or trigger commit')
        print(f'{event_config}')
        raise ValueError('bad config, check logs.')

    build_info_payload, run_tests, test_select, build_env_suffix = pr_resolve.get_prs(
        trigger_repo=repo_name,
        trigger_uri=trigger_repo_uri,
        trigger_pr_id=pull_request_number,
        trigger_commit=trigger_commit,
        pr_payload=pr_payload)
    if not run_tests:
        print(f'Skipping tests for "{repo_name}#{pull_request_number}"')
        return
    test_script = build_info_payload['test_script']
    short_commit = trigger_commit[0:7]
    debug_time = build_info_payload['debug_time']
    print(f'Got PR payload. {timer.checkpoint()}')

    # Map of futures to environment names. Note that future objects are hashable
    # even before they return, so they can be used as dict keys.
    check_run_future_to_env = {}

    # Create the check "unit" and "integration" payloads using a thread pool;
    # The GitHub API is a bit slow and we can take advantage of this by creating
    # all the check runs in a thread pool allowing each thread to wait on the
    # requests independently. Each future returns a dictionary with the Check
    # Run ID mapped to the run type, ex {'unit': -id-, 'integration': -id'}.
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:

        if test_select == 'random':
            chosen_build_environments = [random.choice(BUILD_ENVIRONMENTS)]
        elif test_select == 'all':
            chosen_build_environments = [e for e in BUILD_ENVIRONMENTS]
        else:
            chosen_build_environments = [test_select]

        # Submit check-run create requests via the thread pool (see above).
        for build_environment in chosen_build_environments:
            check_run_future = executor.submit(
                github_client.create_check_runs,
                build_environment,
                repo_name,
                owner,
                trigger_commit,
                build_env_suffix)
            check_run_future_to_env[check_run_future] = build_environment

        # Watch the thread pool futures and create the build jobs when done.
        for future in concurrent.futures.as_completed(check_run_future_to_env):
            build_environment = check_run_future_to_env[future]
            try:
                check_run_id_map = future.result()
            except Exception as exc:
                print(f'Failed to create check runs for {build_environment}'
                      f'due to exception: {exc}')
                continue

            # Attach check-run IDs to the PR payload allowing the test runner to
            # update each check run with a link to the job logs.
            build_info_payload['check_runs'] = check_run_id_map
            print(f'Created check runs. {timer.checkpoint()}')

            # The build info message is converted to json, compressed, then
            # encoded as base64 so that it can be passed to the build container.
            # This compression is necessary because there is a limit of 8192
            # characters in the 'overrides' message used by the underlying
            # ECS run task API.
            # https://docs.aws.amazon.com/AmazonECS/latest/APIReference/API_RunTask.html
            build_info_zipped = gzip.compress(json.dumps(build_info_payload).encode('utf8'))
            build_info_b64 = base64.b64encode(build_info_zipped).decode()

            # Create the AWS Batch job that will execute both the unit and the
            # integration test. The batch job executes a job definition stored
            # in AWS (see cfn/batch-backend.yaml for our job definitions). The
            # job takes the build info json file as an argument and this file
            # configures the build.
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
            print(f'Submitted Batch Job: "{job_arn}". {timer.checkpoint()}')


def get_content_type(headers):
    """Helper function to parse content-type from the header"""
    raw_content_type = headers.get('content-type')

    if raw_content_type is None:
        return None
    # This is Python's recommended std. lib. method for decoding content type.
    msg = email.message.EmailMessage()
    msg['content-type'] = raw_content_type
    return msg.get_content_type()


def print_error(message, headers):
    """Helper function to print errors"""
    print(f'ERROR: {message}\nHeaders: {str(headers)}')


if __name__ == '__main__':
    if os.environ.get('LAMBDA_TEST_GET_PRS'):
        print(pr_resolve.get_prs('oops', 2179))
    elif os.environ.get('TEST_GITHUB_EVENT_JSON'):
        event_json_file = os.environ.get('TEST_GITHUB_EVENT_JSON')
        with open(event_json_file, 'r') as f:
            raw_struct = f.read()
        event = {
            'isBase64Encoded': False,
            'body': raw_struct,
            'headers': {
                'x-github-event': 'pull_request',
                'content-type': 'application/json',
            },
        }
        lambda_handler(event, None)
