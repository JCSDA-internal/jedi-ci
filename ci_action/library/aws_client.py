"""Wrappers for AWS functions where direct API access isn't desired.

"""

import boto3
import os
import time
import functools

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


class BatchSubmitConfigFromEnv(object):
    """Collect batch job config values from the environment.

    This class is configured using the following environment variables. If any
    are unset, the class cannot be instantiated.

    Environment configuration:
        BATCH_JOB_DEFINITION_CLANG: the ARN of the "clang" test batch job def.
        BATCH_JOB_DEFINITION_GNU: the ARN of the "gcc" test batch job def.
        BATCH_JOB_DEFINITION_INTEL: the ARN of the "intel" test batch job def.
        BATCH_JOB_QUEUE: the ARN of the Batch job queue used for testing jobs.
    """

    def __init__(self, timeout):
        """Init from environment."""
        self._job_def_map = {
            'gcc11': os.environ.get('BATCH_JOB_DEFINITION_CLANG', ''),
            'gcc': os.environ.get('BATCH_JOB_DEFINITION_GNU', ''),
            'intel': os.environ.get('BATCH_JOB_DEFINITION_INTEL', ''),
            'gcc11-next': os.environ.get('BATCH_JOB_DEFINITION_CLANG_NEXT', ''),
            'gcc-next': os.environ.get('BATCH_JOB_DEFINITION_GNU_NEXT', ''),
            'intel-next': os.environ.get('BATCH_JOB_DEFINITION_INTEL_NEXT', ''),
        }
        self._job_queue = os.environ.get('BATCH_JOB_QUEUE', '')
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


def submit_test_batch_job(
        config: BatchSubmitConfig,
        repo_name: str,
        commit: str,
        pr: int,
        test_script: str,
        build_identity: str,
        debug_time: int,
        build_info: str,
    ):
    """Submit a CI batch job."""
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
                    'name': 'BUILD_IDENTITY',
                    'value': build_identity
                },
                {
                    'name': 'DEBUG_TIME_SECONDS',
                    'value': f'{debug_time}',
                },
                {
                    'name': 'BUILD_INFO_B64',
                    'value': build_info,
                },
                {
                    'name': 'TEST_SCRIPT',
                    'value': test_script,
                }
            ],
        },
    )
