"""GitHub client wrapper.

This class wraps a number of GitHub client types and functions. As
currently implemented it doesn't do too much but exists to support
the implementation inhereted from the GitHub Lambda function which
required a more complex app-integration client
"""
import datetime
import github
import logging
import os
import random
import time
from functools import lru_cache

from ci_action.library import aws_client

LOG = logging.getLogger("github_client")

GITHUB_URI = "https://github.com/"

GITHUB_URI = "https://github.com/"


class GitHubAppClientManager(object):
    """A wrapper for the GitHub client that efficiently uses app credentials.

    Figuring out which app credential should be used to fetch a repository
    requires using the App's JSON Web Token (JWT) credentials to query the
    target repository for installations, or it requires using credentials to
    pre-fetch all installations. This utility takes the latter approach and
    pre-create the correctly authenticated GitHub client for each target
    install.

    This manager relies on the undocumented (but public) API feature in
    pygithub allowing the use of an AppAuthentication struct to create a Github
    client with refreshable JWT credentials. The client for each install can be
    reused indefinitely since credential expiration is tracked and refreshed
    by the client interface.

    In addition to supporting application authentication, this manager supports
    personal access tokens so that it can be used during local development. If
    a personal access token is used then app credentials cannot be used.
    """

    def __init__(self, personal_access_token: str):
        """Initialize the GitHubAppClientManager."""
        LOG.info(f'Initializing GitHubAppClientManager with personal_access_token, string of length {len(personal_access_token)}')
        if not personal_access_token:
            raise ValueError("argument personal_access_token is required and must be a non-empty string")
        self.client = github.Github(personal_access_token)

    @classmethod
    def init_from_environment(cls):
        # If environment variable JEDI_CI_TOKEN is set, use it to create a client.
        # This is the preferred token used in the GitHub Action workflow.
        if 'JEDI_CI_TOKEN' in os.environ:
            return cls(personal_access_token=os.environ['JEDI_CI_TOKEN'])

        # If environment variable GITHUB_TOKEN is set, use it to create a client.
        if 'GITHUB_TOKEN' in os.environ:
            return cls(personal_access_token=os.environ['GITHUB_TOKEN'])

        # If environment variable GITHUB_TOKEN_FILE is set, read the content 
        # # and use it as a personal access token
        if 'GITHUB_TOKEN_FILE' in os.environ:
            with open(os.environ['GITHUB_TOKEN_FILE'], 'r') as f:
                return cls(personal_access_token=f.read().strip())

        raise EnvironmentError(
            'Environment must have "GITHUB_TOKEN" or "GITHUB_TOKEN_FILE" vars')

    def get_repository(self, repo, owner):
        LOG.info(f'Fetching repository {owner}/{repo}')
        return self.client.get_repo(f'{owner}/{repo}')

    def create_check_run(self, repo, owner, commit, run_name):
        """Create a new GitHub check run."""
        repo = self.get_repository(repo, owner)
        check_run = repo.create_check_run(
            run_name,
            commit,
            status='queued',
        )
        return check_run


def create_check_runs(build_environment, repo, owner, trigger_commit, next_suffix):
    """Create check runs (unit and integration) for a given build environment.

    Args:
        build_environment: intel, gcc, or gcc11 (or any other supported build
            environment).
        repo: The name of the repository.
        owner: The owner of the repository (probably "jcsda-internal").
        trigger_commit: the commit associated with the check run.
        next_suffix: an empty string or "-next" used to indicate use of the
                     pre-release test images in the job name.

    Returns:
        Struct of check run ID's.
        {
            "integration": 14645415163,
            "unit": 14645415264,
        }
    """
    build_environment_name = build_environment + next_suffix
    github_app = GitHubAppClientManager.init_from_environment()
    unit_run_name = f'JEDI unit test: {build_environment_name}'
    integration_run_name = f'JEDI integration test: {build_environment_name}'
    unit_run = github_app.create_check_run(
        repo, owner, trigger_commit, unit_run_name)
    integration_run = github_app.create_check_run(
        repo, owner, trigger_commit, integration_run_name)
    return {'unit': unit_run.id, 'integration': integration_run.id}


@lru_cache(maxsize=1)
def get_client():
    """Lazily initialize and cache the GitHub client manager from environment."""
    return GitHubAppClientManager.init_from_environment()


def validate_github_uri(repo_uri: str) -> str:
    if not repo_uri.startswith(GITHUB_URI) and repo_uri.endswith(".git"):
        raise ValueError(
            f'Uri for {repo_uri} is invalid. It should containt '
            f'{GITHUB_URI} and end in .git.')


def get_fullname_from_github_uri(repo_uri: str) -> str:
    """Converts https://github.com/org/repo.git or https://github.com/org/repo into org/repo."""
    # Remove the GitHub URI prefix
    repo_path = repo_uri[len(GITHUB_URI):]
    # Remove .git suffix if present
    if repo_path.endswith('.git'):
        repo_path = repo_path[:-4]
    return repo_path


def get_repo_tuple_from_github_uri(repo_uri: str) -> str:
    """Converts https://github.com/org/repo.git into a ("repo", "org") tuple."""
    full_repo = get_fullname_from_github_uri(repo_uri)
    org, repo = full_repo.split('/', 1)
    return repo, org
