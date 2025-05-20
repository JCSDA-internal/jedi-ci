#!/usr/bin/env python3
"""
check_run.py manages the GitHub code check representation of our pre-submit
tests. This script creates the check run, updates the check when tests
start, and completes the check run when the tests are done.


## Subcommands

Commands for interacting with GitHub Check Runs.
  * new:    Create a new GitHub check run. This will generate a standard
            test name from the flags and will populate basic fields.
  * update: Fine grained control of mutable check-run fields. This is used
            for basic state updates and for adding the details URL.
  * end:    Evaluate ctest results and advance a check run to complete status
            while adding a test details document describing any observed
            failures and linking to the test logs and cdash page.

Other commands:
  * eval_test_xml: Given a passing test percentage, determine if a Test.xml file
                   meets the criteria for acceptance. Exit code of 1 if the test
                   failure rate is above the test fail percentage.


## Unresolved issues

    - This needs to be updated to include info on build group.


## What are check runs?

  GitHub check runs are replacing the older commit status API. Check runs offer
  more control over test result displays and status visible in the GitHub
  Pull request UI. Some additional caveats to keep in mind.
    - **NOTE**: Check runs can only be created by GitHub applications. They
      cannot be created by user credentials.
    - Run state (queued, running, complete) is updated separately from results.
    - Results can include a title, a markdown document that will be rendered
      GitHub UI, and (not used here) code annotations that will be rendered in
      the pull request diff.

    More information on GitHub check runs are available on GitHub's docs:
    https://docs.github.com/en/rest/checks/runs?apiVersion=2022-11-28


## Examples:

    # Create a new test in the queued state. If this command is successful it
    # will print the check run ID to stdout.
    $ CHECK_RUN_ID=$(
        python3 check_run.py \
            new \
            --app-id=321331 \
            --app-private-key=$HOME/.ssh/my_key.pem \
            --repo=eap/skylab_env \
            --commit=054d80255cb6351ae629f2caca3344537866e598 \
            --test-type=unit \
            --test-platform='arm' \
            --test-logs-url="http://en.wikipedia.org/wiki/Shark")
    $ echo $CHECK_RUN_ID
    14633852039


    # You can update some attributes of a check run using the 'update' command.
    # This example sets the status to "in_progress" and updates the details
    # url. Note the "title" is distinct from the test name and is shown after
    # the state in GitHub. The test name cannot be edited.
    $ python3 check_run.py \
        update \
        --app-id=321331 \
        --app-private-key=$HOME/.ssh/my_key.pem \
        --repo=eap/skylab_env \
        --check-run-id=$CHECK_RUN_ID \
        --status=in_progress \
        --details-url="http://en.wikipedia.org/wiki/Sharknado" \
        --title='Test is now Sharknado'

     # In this 'update' example I am just changing the details URL and title
     # without editing the state.
     $ python3 ./check_run.py update \
          --app-id=321331 \
          --app-private-key=$HOME/.ssh/my_key.pem  \
          --repo=eap/skylab_env \
          --check-run-id=$CHECK_RUN_ID \
          --details-url="http://en.wikipedia.org/wiki/Unit_testing" \
          --title='Build environment allocated'

    # It is also possible to set a simple conclusion using update.
    $ python3 ./check_run.py update \
          --app-id=321331 \
          --app-private-key=$HOME/.ssh/my_key.pem  \
          --repo=eap/skylab_env \
          --check-run-id=$CHECK_RUN_ID \
          --details-url="http://en.wikipedia.org/wiki/Unit_testing" \
          --title='test skipped due to prior failure' \
          --status=completed \
          --conclusion=skipped

    # Finalize test run with results from test output.
    $ python3 check_run.py \
        end \
        --state-file=/tmp/test_config.json \
        --test-xml=/home/build/Testing/20290101-1234/Test.xml \
        --test-logs-url="http://en.wikipedia.org/wiki/Stingray" \
        --max-failure-percentage=0
"""

import argparse
import collections
import json
import os
import requests
import sys
import textwrap
import urllib.parse
import xml.etree.ElementTree

import github

# Values needed when interacting with the GitHub API.
ALLOWED_STATUSES = ['queued', 'in_progress', 'completed']
ALLOWED_CONCLUSIONS = [
    "action_required",
    "cancelled",
    "failure",
    "neutral",
    "success",
    "skipped",
    "stale",
    "timed_out",
]
NotSet = github.GithubObject.NotSet


# Arguments common to sub-commands.
PARSER = argparse.ArgumentParser()

SUBPARSERS = PARSER.add_subparsers(help='sub-command help')
PARSER_NEW = SUBPARSERS.add_parser(
    'new',
    help='create a new queued check run for a commit to a repository.')
PARSER_UPDATE = SUBPARSERS.add_parser(
    'update',
    help='Update an existing check run.')
PARSER_END = SUBPARSERS.add_parser(
    'end',
    help='complete a check run, evaluate results adding the status and a '
         'results document')


# Common arguments for parsers that interact with the API.
for subparser in [PARSER_NEW, PARSER_UPDATE, PARSER_END]:
    subparser.add_argument(
        '--app-id',
        required=True,
        help='The integer App ID for the GitHub application.')
    subparser.add_argument(
        '--app-private-key',
        required=True,
        help='File path to the app private key.')
    subparser.add_argument(
        '--repo',
        required=True,
        help='The repository path in the form of "user/repo-name".')


PARSER_NEW.add_argument(
    '--commit',
    required=True,
    help='The full commit hash of the test target.')
PARSER_NEW.add_argument(
    '--test-platform',
    required=True,
    help='The test platform used for test output and workflow titles.')
PARSER_NEW.add_argument(
    '--test-type',
    required=True,
    choices=['unit', 'integration'],
    help='The test platform used for test output and workflow titles.')
PARSER_NEW.add_argument(
    '--ecs-metadata-uri',
    default='',
    help='URI for the AWS ECS metadata server (generally found using the '
         'ECS_CONTAINER_METADATA_URI_V4 environment variable).')
PARSER_NEW.add_argument(
    '--batch-task-id',
    default='',
    help='The AWS Batch task ID.')


PARSER_UPDATE.add_argument(
    '--check-run-id',
    required=True,
    type=int,
    help='The ID of the GitHub check run that will be edited.')
PARSER_UPDATE.add_argument(
    '--title',
    required=True,
    help='A title that will be displayed visibly in GitHub.')
PARSER_UPDATE.add_argument(
    '--status',
    choices=ALLOWED_STATUSES,
    help='status that will be applied to the check run. If setting "completed"'
         'then --conclusion must also be set.')
PARSER_UPDATE.add_argument(
    '--conclusion',
    choices=ALLOWED_CONCLUSIONS,
    help='A test conclusion that will be set. This flag may only be supplied'
         'if also setting --status=completed')
PARSER_UPDATE.add_argument(
    '--ecs-metadata-uri',
    default='',
    help='URI for the AWS ECS metadata server (generally found using the '
         'ECS_CONTAINER_METADATA_URI_V4 environment variable).')
PARSER_UPDATE.add_argument(
    '--batch-task-id',
    default='',
    help='The AWS Batch task ID.')
PARSER_UPDATE.add_argument(
    '--public-log-link',
    default='',
    help='URL pointing to logs that can be accessed without AWS authentication')


PARSER_END.add_argument(
    '--check-run-id',
    required=True,
    type=int,
    help='The ID of the GitHub check run that will be edited.')
PARSER_END.add_argument(
    '--test-xml',
    required=True,
    help='ctest output xml file with detailed test results.')
PARSER_END.add_argument(
    '--cdash-url',
    required=True,
    help='URL for the cdash output associated with this test.')
PARSER_END.add_argument(
    '--max-failure-percentage',
    type=int,
    required=True,
    help='What percentage of tests may fail without failing the test.')
PARSER_END.add_argument(
    '--ecs-metadata-uri',
    default='',
    help='URI for the AWS ECS metadata server (generally found using the '
         'ECS_CONTAINER_METADATA_URI_V4 environment variable).')
PARSER_END.add_argument(
    '--batch-task-id',
    default='',
    help='The AWS Batch task ID.')
PARSER_END.add_argument(
    '--public-log-link',
    default='',
    help='URL pointing to logs that can be accessed without AWS authentication')


PARSER_EVAL = SUBPARSERS.add_parser('eval_test_xml', help='check if tests pass')
# First argument is a hidden flag used to detect this state.
PARSER_EVAL.add_argument(
    '--eval-xml-flag',
    action='store_true',
    default=True,
    help=argparse.SUPPRESS)
PARSER_EVAL.add_argument(
    '--test-xml',
    required=True,
    help='ctest output xml file with detailed test results.')
PARSER_EVAL.add_argument(
    '--max-failure-percentage',
    type=int,
    required=True,
    help='What percentage of tests may fail without failing the test.')


TEST_GENERAL_INFO = textwrap.dedent(f"""
    ## CI System Information

    A full explanation of all features, behaviors, and configuration options
    can be found in the JEDI Infra knowledge base [article on CI](https://wiki.ucar.edu/display/JEDI/CI).

    ## Quick reference

    ```
    Presubmit tests can be controlled by single-line annotations in the pull
    request description. These annotations will be re-examined for each run.
    Here is an example of their use:

    # Build tests with other unsubmitted packages.
    build-group=https://github.com/JCSDA-internal/oops/pull/2284
    build-group=https://github.com/JCSDA-internal/saber/pull/651

    # Disable the build-cache for tests.
    jedi-ci-build-cache=skip

    Each configuration setting must be on a single line, but order and
    position does not matter.

    # Enable tests for your draft PR (disabled by default).
    run-ci-on-draft=true

    # Use a specific compiler instead of selecting one at random.
    # Must be "gcc", "gcc11", or "intel"
    jedi-ci-test-select=gcc

    # Select the jedi-bundle branch used for building. Using this option
    # disables the build cache.
    jedi-ci-bundle-branch=feature/my-bundle-change

    ```
""")



class TestOutput(
    collections.namedtuple('TestOutput', [
        'all_tests',
        'passed',
        'not_passed',
        'status_dict',
        'not_passing_percent'
    ])
):
    """Using a named-tuple to store Test.xml outputs."""

    __slots__ = ()  # This is a tuple and needs no instance dict.

    def format_not_passed_for_output(self, max_tests=30):
        """convert the not_passed list to a useful output format."""
        tests = []
        for i, test_name in enumerate(self.not_passed):
            test_status = self.status_dict[test_name]
            tests.append(f'{test_name:.<80}..{test_status:.>10}')
            if i + 1 == max_tests:
                omitted = len(self.not_passed) - max_tests
                tests.append(f'Omitting {omitted} results')
                test_name = self.not_passed[-1]
                test_status = self.status_dict[test_name]
                tests.append(f'{test_name:.<80}..{test_status:.>10}')
                break
        return tests

    @classmethod
    def from_test_xml(cls, test_output_xml):
        '''Parse the Test.xml file.

        Once ctest completes a test run, it generates a file called "Test.xml".
        this file summarizes the test results and gives details such as call
        arguments, execution time, concurrency, and other configurable values.

        This function parses the Test.xml file and fills test output values
        used by this script to publish test results and set test status.
        
        The Test.xml file has the following structure (unused fields left out):

             <Site>  // one
                <Testing> // one
                    <TestList> // one
                        <Test>./path/to/package/test_full_name</Test> //repeats
                    </TestList>
                    <Test Status='somestatus'> // repeats
                       <Name>test_name</Name>  // one
                       ... other important test fields.
                    </Test>
                </Testing>
            </Site>
        '''
        all_tests = []
        passed_tests = []
        not_passed = []
        status_dict = {}

        # Open the xml file.
        tree = xml.etree.ElementTree.parse(test_output_xml)
        root = tree.getroot()  # This is the "Site" element.

        # Fetch the Testing child of Site. There is exactly one.
        testing = root.find('Testing')
        if testing is None:
            raise ValueError('Test.xml file mssing <Testing> tag.')

        # 'findall' iterates over direct children of an element with the given
        # specific name. The direct-child relationship is important since
        # TestList contains 'Test' elements with a different structure.
        for i, test in enumerate(testing.findall('Test')):

            # Get test name and add to all_tests.
            name_elem = test.find('Name')
            if name_elem is not None:
                name = name_elem.text
            else:
                name = f'unknown_test_{i}'
            all_tests.append(name)

            # Check if test passed.
            status = test.attrib.get('Status')
            status_dict[name] = status
            if status == 'passed':
                passed_tests.append(name)
            else:
                not_passed.append(name)

        if len(all_tests) == 0:
            # This case is really undefined, but we need a number here. The
            # caller should verify tests were run before using this property.
            not_passing_percent = 100
        else:
            not_passing_percent = 100 * (len(not_passed) / float(len(all_tests)))

        return cls(
            all_tests,
            passed_tests,
            not_passed,
            status_dict,
            not_passing_percent)


class ECSTaskMetaData(object):
    """Class to fetch and query ECS Task MetaData from the MetaData server."""

    def __init__(self, metadata_url=None, job_id=None):
        """Query ECS metadata and init. Object gives dummy values if no URL."""
        self._data = None
        self._job_id = job_id
        self._group = None
        self._region = None
        self._stream = None
        # Get job data.
        if metadata_url:
            response = requests.get(f'{metadata_url}/task')
            self._data = response.json()
        if not self._data:
            return
        containers = self._data.get('Containers', [])
        if not containers:
            return
        log_options = containers[0].get('LogOptions', None)
        if not log_options:
            return
        self._group = log_options.get('awslogs-group')
        self._region = log_options.get('awslogs-region')
        self._stream = log_options.get('awslogs-stream')

    def logs_url(self):
        urlencoded_group = urllib.parse.quote_plus(self._group)
        urlencoded_stream = urllib.parse.quote_plus(self._stream)
        url = (f'https://{self._region}.console.aws.amazon.com/'
               f'cloudwatch/home?region={self._region}#logsV2:log-groups/'
               f'log-group/{urlencoded_group}/log-events/'
               f'{urlencoded_stream}')
        return url

    def batch_task_url(self):
        return (f'https://{self._region}.console.aws.amazon.com/'
                f'batch/home?region={self._region}#jobs/detail/'
                f'{self._job_id}')



def get_authed_github_client(app_id, app_private_key, repo_owner, repo_name):
    """Get an authorized client from a GitHub app ID, key, and repository."""
    app_integration = github.GithubIntegration(
        app_id,
        app_private_key,
        jwt_expiry=599,
    )
    installation = app_integration.get_repo_installation(repo_owner, repo_name)
    auth = github.AppAuthentication(
        app_id=app_id,
        private_key=app_private_key,
        installation_id=installation.id,
    )
    return github.Github(app_auth=auth)


def _create_check_run(app_client, repo, commit, run_name, details_url=NotSet):
    """Create a new GitHub check run."""
    repo_object = app_client.get_repo(repo)
    check_run = repo_object.create_check_run(
        run_name,
        commit,
        details_url=details_url,
        status='queued',
    )
    return check_run


#
# The following functions are receivers for the argparse subparsers.
#


def check_run_new(args, app_id, app_key, repo_owner, repo_name):
    """Create a new check run. Used for "new" subparser."""
    commit = args.commit
    client = get_authed_github_client(
        app_id, app_key, repo_owner, repo_name)
    test_name = f'JEDI {args.test_type} test: {args.test_platform}'

    metadata = ECSTaskMetaData(args.ecs_metadata_uri, args.batch_task_id)
    run = _create_check_run(
        app_client=client,
        repo=f'{repo_owner}/{repo_name}',
        commit=commit,
        run_name=test_name,
        details_url=metadata.batch_task_url())

    print(f'{run.id}')


def check_run_update(args, app_id, app_key, repo_owner, repo_name):
    """Advance a check run's state from queued to running."""

    # Validate a couple of mutually exclusive flag cases. These are documented
    # in the help, but we must try to catch them here.
    if args.status == 'completed' and not args.conclusion:
        raise ValueError(
            'When setting --status=completed a value for --conclusion '
            'must also be included.')
    if args.status != 'completed' and args.conclusion:
        raise ValueError(
            'When setting a value for --conclusion you must always set '
            '--status=completed')
    update_kwargs = {}

    # Get the original check run.
    run_id = args.check_run_id
    client = get_authed_github_client(
        app_id, app_key, repo_owner, repo_name)
    repo = client.get_repo(f'{repo_owner}/{repo_name}')
    check_run = repo.get_check_run(run_id)

    test_info_links = ''  # Empty default will be overridden if possible.
    if args.public_log_link:
        test_info_links += f' * [Build logs]({args.public_log_link})\n'
    # Get batch task info if present.
    if args.batch_task_id:
        metadata = ECSTaskMetaData(args.ecs_metadata_uri, args.batch_task_id)
        update_kwargs['details_url'] = metadata.batch_task_url()
        test_info_links += (
            f' * [Test task]({metadata.batch_task_url()}) (requires AWS login)\n'
            f' * [Test logs]({metadata.logs_url()}) (requires AWS login)\n\n')

    output_md = f'## {check_run.name}\n\n' + test_info_links

    if args.status:
        update_kwargs['status'] = args.status
    if args.conclusion:
        update_kwargs['conclusion'] = args.conclusion

    update_kwargs['output'] = {
        'title': args.title,
        'summary': '',
        'text': output_md + TEST_GENERAL_INFO,
    }

    check_run.edit(**update_kwargs)
    print(f'Successfully updated run {check_run.id}:\n'
          f'{update_kwargs}')


def check_run_end(args, app_id, app_key, repo_owner, repo_name):
    """Update run state to complete and set conclusion from test XML output."""
    run_id = args.check_run_id
    # Validate and load args.
    if args.max_failure_percentage > 100 or args.max_failure_percentage < 0:
        raise ValueError(
            'Flag --max-failure-percentage must be between 0 and 100')

    # Get client.
    client = get_authed_github_client(
        app_id, app_key, repo_owner, repo_name)

    # Summarize test results and make a pass/nopass conclusion.
    results = TestOutput.from_test_xml(args.test_xml)
    count_fail = len(results.not_passed)
    count_pass = len(results.passed)
    count_tests = len(results.all_tests)


    # Get the check run first since the summary document requires
    # info from the check run.
    repo = client.get_repo(f'{repo_owner}/{repo_name}')
    check_run = repo.get_check_run(run_id)
    metadata = ECSTaskMetaData(args.ecs_metadata_uri, args.batch_task_id)

    output_md = textwrap.dedent(f"""
        ## {check_run.name}

        Ran {count_tests} tests. Observed {count_pass} passing
        tests and {count_fail} tests not passing.

        * [CDash results for test]({args.cdash_url})
        * [Build log]({args.public_log_link}) (available after job completes)
        * [CI Job]({metadata.batch_task_url()}) (requires AWS login)
        * [CI Job logs]({metadata.logs_url()}) (requires AWS login)

    """)

    if count_tests == 0:
        conclusion = 'failure'
        title = 'tests failed to run'
    elif count_fail == 0:
        conclusion = 'success'
        title = f'ran {count_tests} tests'
    # Here we can assume count_fail and count_tests is above zero.
    else:
        # If any failures are present (even for passing tests) we should
        # summarize failures in our output document.
        failure_summary = ['\n### Failures', '', '```']
        for not_passed_line in results.format_not_passed_for_output():
            failure_summary.append(f'{not_passed_line}')
        output_md += '\n'.join(failure_summary) + '\n```\n'

        if results.not_passing_percent <= args.max_failure_percentage:
            conclusion = 'success'
            title = (f'Success with some failures; {count_pass} of {count_tests} '
                     'tests passing')
        else:
            conclusion = 'failure'
            title = (f'Failure - {count_pass} of {count_tests} '
                     'tests passing')
    output_md = output_md + TEST_GENERAL_INFO
    check_run.edit(
        status='completed',
        conclusion=conclusion,
        output={'title': title, 'summary': '', 'text': output_md},
        details_url=args.cdash_url)
    print(f'Successfully updated run {check_run.id} to status "completed".')


def eval_test_xml(args):
    if args.max_failure_percentage > 100 or args.max_failure_percentage < 0:
        raise ValueError(
            'Flag --max-failure-percentage must be between 0 and 100')
    results = TestOutput.from_test_xml(args.test_xml)

    if len(results.all_tests) == 0:
        print(f'tests failed to run.')
        sys.exit(1)
    elif results.not_passing_percent > args.max_failure_percentage:
        print(f'Failure rate of {results.not_passing_percent:.1f}% is greater than '
              f'the max failure percentage {args.max_failure_percentage}%.')
        sys.exit(1)
    sys.exit(0)


def print_help(_):
    PARSER.print_help()


if __name__ == '__main__':
    # Setting the arg "func" value must be done at the end of this file due to
    # Python's lexical scoping of identifiers.
    PARSER.set_defaults(func=print_help)
    PARSER_NEW.set_defaults(func=check_run_new)
    PARSER_UPDATE.set_defaults(func=check_run_update)
    PARSER_END.set_defaults(func=check_run_end)
    PARSER_EVAL.set_defaults(func=eval_test_xml)
    args = PARSER.parse_args()

    if 'eval_xml_flag' in args:
        args.func(args)
    else:
        app_id = args.app_id
        key_file = args.app_private_key
        repo_owner, repo_name = args.repo.split('/')
        if not os.path.isfile(key_file):
            raise ValueError(f'No file found for -app-private-key "{key_file}".')
        with open(key_file, 'r') as f:
            app_key = f.read()

        args.func(args, app_id, app_key, repo_owner, repo_name)
