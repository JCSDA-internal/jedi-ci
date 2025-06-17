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


def ttl_lru_cache(ttl, maxsize=100):
    """Least-recently-used (LRU) cache function decorator with time-to-live
    (TTL) windowing.

    This cache is not the most advanced implementation of the idea but it
    is (relatively) simple. The TTL is enforced via time windowing by feeding
    the time window to the builtin LRU cache as a parameter. While cached
    objects may expire sooner than strictly necessary, the cache will never
    return objects that have outlived their TTL.

    Args:
      ttl: the number of seconds for cache item TTL.
      maxsize: the maximum number of items allowed in the cache before
               new items evict the least recently used items.
    """

    def decorator(f):
        # Define the cached function that will be called.
        @functools.lru_cache(maxsize)
        def ttl_keyed_function(*args, ttl_hash, **kwargs):
            del ttl_hash  # The hash is only used to key the lru_cache.
            return f(*args, **kwargs)

        # The wrapped function with the original signature will generate the
        # TTL hash and call the TTL keyed function. This function will be
        # returned by the decorator.
        @functools.wraps(f)
        def wrapped_func_hidden_ttl_key(*args, **kwargs):
            return ttl_keyed_function(
                *args, ttl_hash=int(time.time()/ttl), **kwargs)

        return wrapped_func_hidden_ttl_key
    return decorator


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
        job_definitions = sorted(response['jobDefinitions'], key=lambda x: x['revision'], reverse=True)
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


def submit_test_batch_job(
        config: BatchSubmitConfig,
        repo_name: str,
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
