"""Wrappers for AWS functions where direct API access isn't desired.

"""

import boto3
import functools
import logging
import re

LOG = logging.getLogger("aws_client")


@functools.lru_cache(maxsize=1)
def get_batch_client():
    """Lazily initialize and cache the GitHub client manager from environment."""
    return boto3.session.Session().client(service_name='batch')


class BatchSubmitConfig(object):
    """A batch job config used to submit an AWS batch job."""

    def __init__(self, job_definition, job_queue, timeout, build_environment):
        self.job_definition = job_definition
        self.job_queue = job_queue
        self.timeout = timeout
        self.build_environment = build_environment


class BatchSubmitConfigBuilder(object):
    """Collect batch job config values from the environment.

    This class is configured using the following environment variables. If any
    are unset, the class cannot be instantiated.
    """

    def __init__(self, job_name_map, job_queue, timeout):
        """Init from environment."""

        self._job_def_map = {
            'gcc11': self.get_latest_job_arn(job_name_map, 'gcc11'),
            'gcc': self.get_latest_job_arn(job_name_map, 'gcc'),
            'intel': self.get_latest_job_arn(job_name_map, 'intel'),
            'gcc11-next': self.get_latest_job_arn(job_name_map, 'gcc11-next'),
            'gcc-next': self.get_latest_job_arn(job_name_map, 'gcc-next'),
            'intel-next': self.get_latest_job_arn(job_name_map, 'intel-next')
        }
        self._job_queue = job_queue
        self._timeout = timeout
        # Validate job definition ARNs.
        for job_name, job_arn in self._job_def_map.items():
            if not job_arn.startswith('arn:aws:batch'):
                raise EnvironmentError(
                    f'Variable BATCH_JOB_DEFINITION_* for "{job_name}"'
                    f'is not a AWS Batch service arn. Found "{job_arn}"')
        if not self._job_queue.startswith('arn:aws:batch'):
            raise EnvironmentError(
                f'BATCH_JOB_QUEUE "{self._job_queue}" is not an AWS Batch '
                'service arn. Value must start with "arn:aws:batch"')

    def get_latest_job_arn(self, job_name_map, job_environment):
        """Get the job arn for a given environment."""
        client = get_batch_client()
        batch_job_name = job_name_map[job_environment]
        response = client.describe_job_definitions(
            jobDefinitionName=batch_job_name,
            status='ACTIVE',
        )
        # Get the most recent active job.
        job_definitions = sorted(response['jobDefinitions'],
                                 key=lambda x: x['revision'], reverse=True)
        return job_definitions[0]['jobDefinitionArn']

    def get_config(self, build_environment):
        """Get a BatchSubmitConfig for a named environment."""
        if build_environment not in self._job_def_map:
            raise ValueError(f'no job definition for "{build_environment}"; '
                             f'in job definition map: {self._job_def_map}')
        return BatchSubmitConfig(
            job_definition=self._job_def_map[build_environment],
            job_queue=self._job_queue,
            timeout=self._timeout,
            build_environment=build_environment)


def cancel_prior_batch_jobs(job_queue: str, repo_name: str, pr: int):
    """List currently running jedi-ci jobs and cancel them based on commit/build environment logic.

    If the commit does not match the current commit cancel the job.

    Args:
        repo_name: Repository name
        pr: Pull request number
        current_commit: Current commit hash to compare against
    """
    client = get_batch_client()
    jobs_to_cancel = []

    # compile the regex using the repo_name and PR number as filtering values
    # with capture groups for the commit and build environment
    regex = re.compile(f'jedi-ci-{repo_name}-{pr}' + r'-(\w+)-(\w+)')

    LOG.info(f'Using regex: {regex.pattern}')

    pending_jobs_statuses = ['SUBMITTED', 'PENDING', 'RUNNABLE', 'STARTING', 'RUNNING']

    # Use list_jobs with a filter to find jobs from our current repo and pull request.
    response = client.list_jobs(
            jobQueue=job_queue,
            filters=[{'name': 'JOB_NAME', 'values': [f'jedi-ci-{repo_name}-{pr}-*']}],
            maxResults=20,
    )

    for job_summary in response['jobSummaryList']:
        job_status = job_summary['status']
        job_name = job_summary['jobName']
        job_id = job_summary['jobId']

        LOG.info(f'{job_name} -> status "{job_status}"')

        if job_status not in pending_jobs_statuses:
            LOG.info(f'{job_name} not pending, skipping')
            continue

        # Regex to extract commit, and build environment
        match = regex.search(job_name)
        if not match:
            LOG.info(f'{job_name} not matching regex, skipping')
            continue

        # Cancel any running or pending jobs for the pull request.
        jobs_to_cancel.append({
            'jobId': job_id,
            'jobName': job_name,
            'jobStatus': job_status,
            'reason': "Preempted by new test run"
        })

    # Cancel the identified jobs. A failed cancellation will be caught to ensure that
    # the new job is allowed to run (status changes may cause jobs to be uncancelable).
    cancelled_jobs = []
    for job_info in jobs_to_cancel:
        if job_info['jobStatus'] in ['STARTING', 'RUNNING']:
            LOG.info(f"Terminating job {job_info['jobName']} (ID: {job_info['jobId']})")
            # Use terminate_job for running jobs
            client.terminate_job(
                jobId=job_info['jobId'],
                reason=job_info['reason']
            )
            print(f"Terminated job {job_info['jobName']} (ID: {job_info['jobId']})")
        else:
            # Use cancel_job for pending jobs
            LOG.info(f"Cancelling job {job_info['jobName']} (ID: {job_info['jobId']})")
            client.cancel_job(
                jobId=job_info['jobId'],
                reason=job_info['reason']
            )
            print(f"Cancelled job {job_info['jobName']} (ID: {job_info['jobId']})")

        cancelled_jobs.append(job_info)

    return cancelled_jobs


def submit_test_batch_job(
        config: BatchSubmitConfig,
        repo_name: str,
        repo_name_full: str,
        commit: str,
        pr: int,
        configured_bundle_tarball: str,
        debug_time_seconds: int,
        build_identity: str,
        unittest_tag: str,
        trigger_sha: str,
        trigger_pr: str,
        integration_run_id: str,
        unit_run_id: str,
):
    """Submit a CI batch job with updated environment variables."""
    job_name = f'jedi-ci-{repo_name}-{pr}-{commit}-{config.build_environment}'
    return get_batch_client().submit_job(
        jobName=job_name,
        jobQueue=config.job_queue,
        jobDefinition=config.job_definition,
        timeout={
            'attemptDurationSeconds': config.timeout,
        },
        containerOverrides={
            'environment': [
                {
                    'name': 'TRIGGER_REPO',
                    'value': repo_name,
                },
                {
                    'name': 'TRIGGER_REPO_FULL',
                    'value': repo_name_full,
                },
                {
                    'name': 'BUILD_IDENTITY',
                    'value': build_identity
                },
                {
                    'name': 'DEBUG_TIME_SECONDS',
                    'value': f'{debug_time_seconds}',
                },
                {
                    'name': 'CONFIGURED_BUNDLE_TARBALL_S3',
                    'value': configured_bundle_tarball,
                },
                {
                    'name': 'UNITTEST_TAG',
                    'value': unittest_tag,
                },
                {
                    'name': 'TRIGGER_SHA',
                    'value': trigger_sha,
                },
                {
                    'name': 'TRIGGER_PR',
                    'value': str(trigger_pr),
                },
                {
                    'name': 'INTEGRATION_RUN_ID',
                    'value': str(integration_run_id),
                },
                {
                    'name': 'UNIT_RUN_ID',
                    'value': str(unit_run_id),
                },
            ],
        },
    )
