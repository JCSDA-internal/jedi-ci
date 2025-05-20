"""Webhook implementation for Github"""

import os
import json
import urllib.parse
import base64
import concurrent.futures
import hmac
import hashlib
import email
import gzip
import boto3
import botocore
import botocore.session
import random
import time
import traceback
import tempfile
import uuid
import zipfile

from library import aws_client
from library import pr_resolve
from library import github_client

client = botocore.session.get_session().create_client('secretsmanager')
s3 = boto3.client('s3')

# This is a constructor for the configuration needed to submit AWS Batch jobs.
# This constructor reads configuration from the environment and must be
# configured via environmental variables set in the Lambda function. Note that
# the timeout is set to is 4 hours since even a full cache rebuild should much
# less time. For information on the config variables.
BATCH_SUBMIT_ENV = aws_client.BatchSubmitConfigFromEnv(timeout=60*240)

github_webhook_secret_arn = os.environ.get('GITHUB_WEBHOOK_SECRET_ARN')

ACTIONABLE_ACTIONS = ['pull_request:opened', 'pull_request:synchronize']
IGNORE_ACTIONS = ['check_run:completed', 'check_run:created']

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


def lambda_handler(event, _context):
    """Webhook function"""
    headers = event.get('headers')

    # Input validation; this lambda function handles a GitHub webhook event
    # and expects a standard event json payload.
    try:
        json_payload = get_json_payload(event=event)
    except ValueError as err:
        traceback.print_exception(type(err), value=err, tb=err.__traceback__)
        print_error(f'400 Bad Request - {err}', headers)
        return {'statusCode': 400, 'body': str(err)}
    except Exception as err:  # Unexpected Error
        traceback.print_exception(type(err), value=err, tb=err.__traceback__)
        print_error('500 Internal Server Error\n' +
                    f'Unexpected error: {err}, {type(err)}', headers)
        return {'statusCode': 500, 'body': 'Internal Server Error'}
    # Validate webhook signature.
    if not contains_valid_signature(event=event):
        traceback.print_exception(type(err), value=err, tb=err.__traceback__)
        print_error('401 Unauthorized - Invalid Signature', headers)
        return {'statusCode': 401, 'body': 'Invalid Signature'}

    # Process event.
    detail_type = headers.get('x-github-event', 'github-webhook-lambda')
    try:
        process_event(json_payload, detail_type)
        return {'statusCode': 202, 'body': 'Webhook processed'}
    except Exception as err:  # Unexpected Error
        traceback.print_exception(type(err), value=err, tb=err.__traceback__)
        print_error('500 Internal Server Error\n' +
                    f'Unexpected error: {err}, {type(err)}', headers)
        return {'statusCode': 500, 'body': 'Internal Server Error'}


def contains_valid_signature(event):
    """Check for the payload signature
       Github documention: https://docs.github.com/en/developers/webhooks-and-events/webhooks/securing-your-webhooks#validating-payloads-from-github
    """
    secret = aws_client.get_secret_string(github_webhook_secret_arn)
    payload_bytes = get_payload_bytes(
        raw_payload=event['body'], is_base64_encoded=event['isBase64Encoded'])
    computed_signature = compute_signature(
        payload_bytes=payload_bytes, secret=secret)

    return hmac.compare_digest(event['headers'].get('x-hub-signature-256', ''), computed_signature)


def get_payload_bytes(raw_payload, is_base64_encoded):
    """Get payload bytes to feed hash function"""
    if is_base64_encoded:
        return base64.b64decode(raw_payload)
    else:
        return raw_payload.encode()


def compute_signature(payload_bytes, secret):
    """Compute HMAC-SHA256"""
    m = hmac.new(key=secret.encode(), msg=payload_bytes,
                 digestmod=hashlib.sha256)
    return 'sha256=' + m.hexdigest()


def get_json_payload(event):
    """Get JSON string from payload"""
    content_type = get_content_type(event.get('headers', {}))
    if not (content_type == 'application/json' or
            content_type == 'application/x-www-form-urlencoded'):
        raise ValueError(f'Unsupported content-type: {content_type}')

    raw_payload = event.get('body')
    if raw_payload is None:
        raise ValueError('Missing event body')
    payload = raw_payload
    if event['isBase64Encoded']:
        payload = base64.b64decode(raw_payload).decode('utf-8')

    if content_type == 'application/x-www-form-urlencoded':
        parsed_qs = urllib.parse.parse_qs(payload)
        if 'payload' not in parsed_qs or len(parsed_qs['payload']) != 1:
            raise ValueError('Invalid urlencoded payload')
        payload = parsed_qs['payload'][0]

    try:
       payload = json.loads(payload)
    except ValueError as err:
        raise ValueError('Invalid JSON payload') from err

    return payload


def upload_to_aws(json_data, s3_file):
    """Upload file to S3 bucket"""
    if isinstance(json_data, str):
        json_data = json_data.encode('utf-8')
    bucket = os.environ['BUCKET_NAME']
    s3.put_object(Body=json_data, Bucket=bucket, Key=s3_file)
    s3_path = f's3://{bucket}/{s3_file}'
    return s3_path


def process_event(payload, detail_type):
    """Create file for S3 bucket"""
    timer = TimeCheckpointer()
    trigger_repo = payload['repository']['full_name']
    trigger_repo_uri = payload['repository']['clone_url']
    owner, repo_name = trigger_repo.split('/')
    action = payload.get('action', '')
    detail_action = f'{detail_type}:{action}'
    print(f'Event: {detail_type}. Action: {action}. Repo: {trigger_repo}')

    # vars that must be pulled from the event.
    branch_name = ''
    pull_request_number = -1
    pr_payload = None

    if detail_action in ACTIONABLE_ACTIONS and detail_type == 'pull_request':
        branch_name = payload['pull_request']['head']['ref']
        pull_request_number = payload['pull_request']['number']
        pr_payload = payload['pull_request']
        trigger_commit = payload['pull_request']['head']['sha']
    elif detail_action in IGNORE_ACTIONS:  # Known events with no action.
        print(f'Ignoring {trigger_repo}: event "{detail_type}:{action}"')
        return
    else:  # These are unknown events.
        print(f'No processor for event "{detail_type}" with action "{action}"')
        print(f'{payload}')
        return

    if pull_request_number <= 0:
        print(f'{detail_type} event on {branch_name} found no associated PR')
        print(f'{payload}')
        return

    print(f'Evaluated Git event. {timer.checkpoint()}')
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
                config=BATCH_SUBMIT_ENV.get_config(build_environment + build_env_suffix),
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
