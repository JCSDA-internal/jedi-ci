import json
import logging
import os
import re
from typing import Any, Mapping, NamedTuple, Tuple, Union

import github

from ci_action.library import github_client
from ci_action.library import aws_client

LOG = logging.getLogger("pr_resolve")

logging.basicConfig(level=logging.INFO)


INTEGRATED_ORG_WHITELIST = frozenset(['jcsda-internal', 'jcsda', 'geos-esm'])

# Build group regex searches for instances of "build-group=<PR link>". The link
# may be a literal GitHub URL, or it can be a short-link that is also respected
# by the GitHub UI. Because the link format isn't known initially, we match
# against any possible set of URL characters and refine the match in a later
# step.
BUILD_GROUP_RE = re.compile(
    r'^build-group\s?=\s?([a-zA-Z0-9\/:#\._-]{10,70})\s*$', re.MULTILINE | re.IGNORECASE)
# Once the build group line is captured, this is used to parse the repository
# and pull request number from the captured text.
BUILD_GROUP_LINK = re.compile(
    r'([A-Za-z0-9._-]{3,30})/([A-Za-z0-9._-]{3,40})(?:#|/pull/)([0-9]{1,7})\s*$')
CACHE_BEHAVIOR_RE = re.compile(
    r'^jedi-ci-build-cache\s?=\s?(skip|rebuild)\s*$', re.MULTILINE | re.IGNORECASE)
DRAFT_PR_RUN_RE = re.compile(
    r'^run-ci-on-draft\s?=\s?([a-zA-Z]{0,10})\s*$', re.MULTILINE | re.IGNORECASE)
DEBUG_CI_RE = re.compile(
    r'^jedi-ci-debug\s?=\s?t(rue)?\s*$', re.MULTILINE | re.IGNORECASE)
NEXT_CI_RE = re.compile(
    r'^jedi-ci-next\s?=\s?t(rue)?\s*$', re.MULTILINE | re.IGNORECASE)
CI_TEST_SELECT_RE = re.compile(
    r'^jedi-ci-test-select\s?=\s?(random|all|intel|gcc|gcc11)?\s*$', re.MULTILINE | re.IGNORECASE)
JEDI_BUNDLE_BRANCH_RE = re.compile(
    r'^jedi-ci-bundle-branch\s?=\s?([a-zA-Z0-9\/:#\._-]{1,70})?\s*$', re.MULTILINE | re.IGNORECASE)
MANIFEST_BRANCH_RE = re.compile(
    r'^jedi-ci-manifest-branch\s?=\s?([a-zA-Z0-9\/:#\._-]{1,70})?\s*$', re.MULTILINE | re.IGNORECASE)



class TestAnnotations(NamedTuple):
    # A dict mapping repository names to pull request numbers. This map is
    # used to generate the pull request build group.
    build_group_map: Mapping[str, int]

    # A string value representing a json boolean ("true" or "false"), this is
    # passed to the build-info json file. If set to "true" the test runner will
    # not read from the cache and will build all code.
    skip_cache: str

    # A string value representing a json boolean ("true" or "false"). This is
    # passed to the build-info json file. If set to "true" the cache will be
    # re-built.
    rebuild_cache: str

    # If False, tests will not be run for this change. This setting is used
    # when evaluating draft pull requests which will not run by default but
    # may be enabled with an annotation.
    run_tests: bool

    # If True, a 2-hour sleep will be added to the conclusion of a test.
    debug_mode: bool

    # Suffix used to select the CI environment. May be an empty sting or
    # may be "-next" meaning that the "<env-name>-next" environment will be
    # used for testing.
    next_ci_suffix: str

    # Selects which test to run, may be "random", "all", or one of the three
    # valid build environments.
    test_select: str

    # Sets the jedi-bundle branch used for building the tests. If this value
    # is not set (or set to an empty string) the test runner will checkout the
    # default branch. This branch must exist in `JCSDA-internal/jedi-bundle`.
    jedi_bundle_branch: str

    # Set the CI test manifest config file branch used for evaluating the test
    # dependencies. If this value is not set (or is set to an empty string) the
    # lambda function will use the 'develop' branch. Any specified branch
    # must exist in `JCSDA-internal/CI` or the test will fail to configure.
    jedi_ci_manifest_branch: str


def read_test_annotations(
        repo_uri: str,
        pr_number: int,
        pr_payload: Union[Mapping[str, Any], None],
        testmode: bool,
        build_group_regex=BUILD_GROUP_RE,
        cache_regex=CACHE_BEHAVIOR_RE,
        draft_regex=DRAFT_PR_RUN_RE,
        debug_regex=DEBUG_CI_RE,
        test_select_regex=CI_TEST_SELECT_RE,
        next_ci_regex=NEXT_CI_RE,
        jedi_bundle_branch_regex=JEDI_BUNDLE_BRANCH_RE,
        manifest_branch_regex=MANIFEST_BRANCH_RE,
        ) -> TestAnnotations:
    """Reads all jedi-ci specific behavior annotations from a pull request.

    Returns a TestAnnotations named-tuple with all values set from the pull
    request description or set to the default.
    """
    # Get the PR description if it was not provided by the caller.
    if not pr_payload:
        github_client.validate_github_uri(repo_uri=repo_uri)
        repo, org = github_client.get_repo_tuple_from_github_uri(repo_uri=repo_uri)
        grepo = github_client.get_client().get_repository(repo, org)
        pr_payload = grepo.get_pull(pr_number)._rawData
        pr_body = pr_payload["body"]
    else:
        pr_body = pr_payload["body"]

    LOG.info(f'pr_body: {pr_body}')
    # GitHub may use windows newlines (\r\n), this swap here ensures that no
    # matter what newline type is returned, the text is evaluated with standard
    # newlines.
    pr_body = '\n'.join(pr_body.splitlines())
    # Build Group
    build_group_members = []
    build_group_matches = build_group_regex.findall(pr_body)
    LOG.info(f'build_group_matches: {build_group_matches}')
    for group_match in build_group_matches:
        build_group_members.append(group_match)
    print(f'Intermediate group matches: {build_group_members}')
    build_group_pr_map = get_build_group_pr_map(build_group_members)
    if not testmode:
        # If this is not a self-test then the target repo is added to
        # the build group PR map since it will be used for bundle rewriting.
        repo_name, org = github_client.get_repo_tuple_from_github_uri(repo_uri=repo_uri)
        build_group_pr_map[f'{org.lower()}/{repo_name.lower()}'] = int(pr_number)

    # Cache behavior: note that skip cache controls read behavior while
    # rebuild_cache controls write behavior. The correct global behavior can
    # be understood globally from the keyword used since rebuilding the cache
    # requires also skipping cache lookup a user skipping the cache may not
    # want their change to update the shared binary cache.
    cache_behavior = cache_regex.findall(pr_body)
    if cache_behavior and cache_behavior[0].lower() == 'skip':
        skip_cache = 'true'
        rebuild_cache = 'false'
    elif cache_behavior and cache_behavior[0].lower() == 'rebuild':
        skip_cache = 'true'
        rebuild_cache = 'true'
    else:
        skip_cache = 'false'
        rebuild_cache = 'false'
    # Draft pull requests must be annotated for tests to run. If a pull request
    # is a draft pull request, the tests will be skipped unless the author
    # has added an annotation.
    run_tests = False  # Start with negative assumption.
    if not pr_payload.get('draft'):
        run_tests = True  # Run tests if pr is not a draft.
    else:
        # Run tests if PR is a draft and annotated to run anyways.
        draft_pr_note = draft_regex.findall(pr_body)
        if draft_pr_note and draft_pr_note[0].lower() in ['t', 'true', 'yes']:
            run_tests = True

    # Check if debug mode is enabled.
    debug_mode = bool(debug_regex.findall(pr_body))
    # Check if "next" CI is enabled and set the suffix
    next_ci = bool(next_ci_regex.findall(pr_body))
    next_ci_suffix = '-next' if next_ci else ''

    test_select_found = test_select_regex.findall(pr_body)
    if test_select_found:
        test_select = test_select_found[0]
    else:
        test_select = 'random'

    # Determine if there is a nonstandard jedi-bundle branch. Finding any
    # value here updates the cache behavior to skip since the bundle changes
    # may alter the build dependency DAG.
    bundle_branch_config = jedi_bundle_branch_regex.findall(pr_body)
    bundle_branch = ''
    if bundle_branch_config:
        bundle_branch = bundle_branch_config[0]
        skip_cache = 'true'  # Do not read from the cache.
        rebuild_cache = 'false'  # Do not save build results to the cache.

    # If an alternative manifest branch is set, fetch it. Finding any value
    # here updates the cache behavior to skip since the manifest may contain
    # conflicting cache directives.
    manifest_branch_config = manifest_branch_regex.findall(pr_body)
    manifest_branch = ''
    if manifest_branch_config:
        manifest_branch = manifest_branch_config[0]
        skip_cache = 'true'  # Do not read from the cache.
        rebuild_cache = 'false'  # Do not save build results to the cache.


    return TestAnnotations(
        build_group_map=build_group_pr_map,
        skip_cache=skip_cache,
        rebuild_cache=rebuild_cache,
        run_tests=run_tests,
        debug_mode=debug_mode,
        next_ci_suffix=next_ci_suffix,
        test_select=test_select,
        jedi_bundle_branch=bundle_branch,
        jedi_ci_manifest_branch=manifest_branch,
    )


def get_build_group_pr_map(build_group_members):
    pr_map = {}
    for member in build_group_members:
        member_match = BUILD_GROUP_LINK.search(member)
        if not member_match:
            continue
        owner, repo, pull_number = member_match.groups()
        pr_map[f'{owner.lower()}/{repo.lower()}'] = int(pull_number)
    return pr_map


def gather_build_group_hashes(build_group_mapping):
    """Colects the commit hash for each repository in the build group."""
    pr_group_map_out = {}

    for repo_name_key, pr_number in build_group_mapping.items():
        org, repo = repo_name_key.split('/')
        grepo = github_client.get_client().get_repository(repo, org)
        pr = grepo.get_pull(pr_number)
        pr_group_map_out[repo_name_key] = {
            "name_key": repo_name_key,
            "uri": grepo.clone_url,
            "version_ref": {
                "pr_id": pr.number,
                "branch": pr.head.ref,
                "commit": pr.head.sha,
            },
        }
    return pr_group_map_out
