import json
import logging
import os
import re
from typing import Any, Mapping, NamedTuple, Tuple, Union

import github

from library import github_client
from library import aws_client


logging.basicConfig(level=logging.INFO)
GITHUB_URI = "https://github.com/"

GITHUB_CLIENT = github_client.GitHubAppClientManager.init_from_environment()

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


@aws_client.ttl_lru_cache(ttl=5*60)
def get_test_manifest(branch_arg: str, manifest_path: str = 'test_manifest.json'):
    # Default branch used if the test description did not override this.
    branch_name = os.environ.get('GITHUB_CI_REPO_BRANCH', 'develop')
    if branch_arg:
        branch_name = branch_arg
    repo = GITHUB_CLIENT.get_repository('CI', 'JCSDA-internal')
    contents = repo.get_contents(manifest_path, ref=branch_name)
    json_content =  json.loads(contents.decoded_content)
    return json_content


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
        _validate_repo_uri(repo_uri=repo_uri)
        repo, org = _repo_tuple_from_uri(repo_uri=repo_uri)
        grepo = GITHUB_CLIENT.get_repository(repo, org)
        pr_payload = grepo.get_pull(pr_number)._rawData
        pr_body = pr_payload["body"]
    else:
        pr_body = pr_payload["body"]
    # GitHub may use windows newlines (\r\n), this swap here ensures that no
    # matter what newline type is returned, the text is evaluated with standard
    # newlines.
    pr_body = '\n'.join(pr_body.splitlines())
    # Build Group
    build_group_members = []
    build_group_matches = build_group_regex.findall(pr_body)
    for group_match in build_group_matches:
        build_group_members.append(group_match)
    print(f'Intermediate group matches: {build_group_members}')
    build_group_pr_map = get_build_group_pr_map(build_group_members)
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


def gather_pr_group(test_manifest: dict, trigger_repo_key: str, curr_pr_id: str, pr_group_map):
    group = []

    # Loop over all repositories in the manifest. Later "continue" calls
    # all come back here (the "repository loop").
    for manifest_entry in test_manifest["repositories"]:
        repo_uri = manifest_entry["uri"]
        repo_manifest_name = manifest_entry["name"]
        _validate_repo_uri(repo_uri=repo_uri)
        repo_name, org = _repo_tuple_from_uri(repo_uri=repo_uri)
        repo_name_key = org.lower() + '/' + repo_name.lower()

        # External orgs (including public "jcsda") are not evaluated for
        # pull request matching. We just use the identified release branch.
        if org.lower() not in INTEGRATED_ORG_WHITELIST:
            group.append(
                {
                    "name": repo_manifest_name,
                    "uri": repo_uri,
                    "version_ref": {
                        "pr_id": "",
                        "branch": manifest_entry['branch'],
                    },
                }
            )
            continue  # Continue the repository loop.

        # This step only works for git repos with our app integration.
        grepo = GITHUB_CLIENT.get_repository(repo_name, org)


        # Handle self-reference differently (we already know pull #, etc).
        if repo_name_key == trigger_repo_key:
            pr = grepo.get_pull(curr_pr_id)
            group.append(
                {
                    "name": repo_manifest_name,
                    "uri": repo_uri,
                    "version_ref": {
                        "pr_id": pr.number,
                        "branch": pr.head.ref,
                        "commit": pr.head.sha,
                    },
                }
            )
            continue  # Continue the repository loop.

        if repo_name_key in pr_group_map:
            pr_number = pr_group_map[repo_name_key]
            pr = grepo.get_pull(pr_number)
            group.append(
                    {
                        "name": repo_manifest_name,
                        "uri": repo_uri,
                        "version_ref": {
                            "pr_id": pr.number,
                            "branch": pr.head.ref,
                            "commit": pr.head.sha,
                        },
                    }
                )
            continue

        # If there is no build group or no pull request that matches then we
        # will get the default branch.
        if 'branch' in manifest_entry:
            default_branch = grepo.get_branch(manifest_entry['branch'])
            ref_name = f'refs/heads/{manifest_entry["branch"]}'
            commit_sha = default_branch.commit.sha
        elif 'release_tag' in manifest_entry:
            # Release tags can use the built-in version reference and only
            # need to be configured if a pull request group is specified. This
            # configuration is handled by the section that checks if the repo
            # is in the `pr_group_map` dictionary.
            continue

        group.append(
            {
                "name": repo_manifest_name,
                "uri": repo_uri,
                "version_ref": {
                    "pr_id": "",
                    "branch": ref_name,
                    "commit": commit_sha,
                },
            }
        )
    # Return the gathered manifest branch version references.
    return group


def get_prs(trigger_repo: str, trigger_uri: str, trigger_pr_id: str, trigger_commit: str, pr_payload: Union[Mapping[str, Any], None]):
    """Evaluate a commit and configure tests based on the manifest and annotations.

    Args:
      trigger_repo: name of the triggering repository.
      trigger_uri: https URI of the triggering repository.
      trigger_pr_id: (string) the pull request number expressed as a string.
      trigger_commit: full SHA hash of the trigger commit.

    Returns a 3-tuple of:
      build_info_payload: (dict) The "build info" json used to configure a test.
      run_tests: (bool) go/no-go for tests.
      test_select: string, may be one of {random, all, gcc, gcc11, intel}
      job_suffix: Used as the suffix for the job definition. May be an
                    empty string or "-next" to test updates to the CI
                    environment.
    """
    test_annotations = read_test_annotations(
        repo_uri=trigger_uri, pr_number=trigger_pr_id, pr_payload=pr_payload)

    test_manifest = get_test_manifest(test_annotations.jedi_ci_manifest_branch)

    # Get group name, test tags, and other attributes from current PR
    real_repo_name, org = _repo_tuple_from_uri(repo_uri=trigger_uri)
    repo_name_key = org.lower() + '/' + real_repo_name.lower()
    current_repo_manifest = list(
        filter(
            lambda repo_manifest: repo_manifest["uri"].lower() == trigger_uri.lower(),
            test_manifest["repositories"]
        )
    )[0]
    # Retreive current repo uri
    curr_repo_uri = current_repo_manifest["uri"]
    curr_repo_manifest_name = current_repo_manifest["name"]
    test_tag = current_repo_manifest.get("test_tag", "")


    # If not running tests, return early to avoid unnecessary GitHub API calls.
    if not test_annotations.run_tests:
        return None, False, "", ""

    print(f"Build group mapping found: {test_annotations.build_group_map}")
    pr_group = gather_pr_group(
        test_manifest, repo_name_key, trigger_pr_id, test_annotations.build_group_map)

    build_info_payload = {
        "trigger_pr_number": trigger_pr_id,
        "trigger_commit_sha": trigger_commit,
        "trigger_repo":  trigger_repo,
        "manifest_name": curr_repo_manifest_name,
        "test_tag": test_tag,
        "dependencies": current_repo_manifest.get('dependencies', []),
        "test_script": current_repo_manifest.get('test_script'),
        "build_group": test_annotations.build_group_map,
        "skip_cache": test_annotations.skip_cache,
        "rebuild_cache": test_annotations.rebuild_cache,
        "version_map": pr_group,
        "debug_time": 120*60 if test_annotations.debug_mode else 0,
        "jedi_bundle_branch": test_annotations.jedi_bundle_branch,
    }


    return (
        build_info_payload,
        True,
        test_annotations.test_select,
        test_annotations.next_ci_suffix,
    )


def _validate_repo_uri(repo_uri: str) -> str:
    if not repo_uri.startswith(GITHUB_URI) and repo_uri.endswith(".git"):
        raise ValueError(
            f'Uri for {repo_name} is invalid. It should containt '
            f'{GITHUB_URI} and end in .git.')


def _get_fullname_from_uri(repo_uri: str) -> str:
    """Converts https://github.com/org/repo.git into org/repo."""
    return repo_uri[len(GITHUB_URI) : -4 : 1]


def _repo_tuple_from_uri(repo_uri: str) -> str:
    """Converts https://github.com/org/repo.git into a ("repo", "org") tuple."""
    full_repo = _get_fullname_from_uri(repo_uri)
    org, repo = full_repo.split('/', 1)
    return repo, org
