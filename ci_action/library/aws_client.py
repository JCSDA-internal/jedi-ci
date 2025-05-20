"""Wrappers for AWS functions where direct API access isn't desired.

"""

import boto3
import os
import time
import functools

SECRET_CLIENT = boto3.session.Session().client(service_name='secretsmanager')
BATCH_CLIENT = boto3.session.Session().client(service_name='batch')

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


@ttl_lru_cache(ttl=3600)  # Secrets expire after an hour.
def get_secret_string(secret_arn):
    """Get a secret value from aws secret store."""
    get_secret_value_response = SECRET_CLIENT.get_secret_value(
        SecretId=secret_arn
    )
    # Secrets Manager decrypts the secret value using the associated KMS CMK
    # Depending on whether the secret was a string or binary, only one of these
    # fields will be populated
    if 'SecretString' in get_secret_value_response:
        return get_secret_value_response['SecretString']
    else:
        return get_secret_value_response['SecretBinary']


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
    return BATCH_CLIENT.submit_job(
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
