"""GitHub client wrapper for Lambda function.

Our GitHub Lambda function will use a GitHub app integration for accessing
repository metadata and code. GitHub app integration credentials work with
credentials scoped to the "install" which is associated with a user or an
organization; a single application may have many installs.

This library provides the class GitHubAppClientManager whose instances will
evaluate an application and eagerly create the full set  of the GitHub clients.
Using this manager the caller may access the client directly, or make the
'get_repository' call which will automatically use the correct client and
return a github.Repository object.

The GitHubAppClientManager may be initialized directly, or may be initialized
using the following environment variables:
  - GITHUB_APP_ID: GitHub App ID, usually a set of numbers (eg. "12345").
  - GITHUB_APP_KEY_ARN: The AWS ARN identifier for the GitHub app private
                        RSA key.

This utility can also fall back to using a personal access token (PAT) which is
helpful for local development. Set GITHUB_TOKEN_FILE to the location of a file
containing a personal access token. The application credentials will not be used
when a PAT is provided.
"""
import datetime
import github
import jwt
import logging
import os
import random
import time

from library import aws_client


# The init_from_environment() acts as a singleton instantiator to reduce
# duplication of API calls. Once that method is called, the client manager
# object will be kept here.
_GITHUB_ENVIRONMENT_CLIENT = None


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

    def __init__(self,
                 app_id: str,
                 app_private_key: str,
                 personal_access_token: str = ""):
        """Initialize the GitHubAppClientManager."""
        self._app_clients = {}
        self._integration = None
        if (app_id or app_private_key) and personal_access_token:
            raise ValueError(
                "You must initialize with app credentials or personal access "
                "token credentials, the use of both is not allowed.")
        # Personal access token case short circuits much of this object's use
        # but should be supported to allow development elsewhere.
        if personal_access_token:
            self._pat_client = github.Github(personal_access_token)
            return
        # Support for app credentials.
        self._pat_client = None
        self._integration = github.GithubIntegration(
            app_id,
            app_private_key,
            jwt_expiry=599)

        for installation in self._integration.get_installations():
            auth = github.AppAuthentication(
                    app_id=app_id,
                    private_key=app_private_key,
                    installation_id=installation.id)
            client = github.Github(app_auth=auth)
            client_name = installation.raw_data['account']['login'].lower()
            self._app_clients[client_name] = client

    @classmethod
    def init_from_environment(cls):
        global _GITHUB_ENVIRONMENT_CLIENT
        if _GITHUB_ENVIRONMENT_CLIENT:
            return _GITHUB_ENVIRONMENT_CLIENT

        if 'GITHUB_TOKEN_FILE' in os.environ:
            with open(os.environ['GITHUB_TOKEN_FILE'], 'r') as f:
                _GITHUB_ENVIRONMENT_CLIENT = cls(
                    None, None, personal_access_token=f.read().strip())
                return _GITHUB_ENVIRONMENT_CLIENT
        if 'GITHUB_APP_ID' in os.environ and 'GITHUB_APP_KEY_ARN' in os.environ:
            app_key = aws_client.get_secret_string(
                os.environ['GITHUB_APP_KEY_ARN'])
            app_id = os.environ['GITHUB_APP_ID']
            _GITHUB_ENVIRONMENT_CLIENT = cls(app_id, app_key, None)
            return _GITHUB_ENVIRONMENT_CLIENT
        raise EnvironmentError(
            'Environment must have var "GITHUB_TOKEN_FILE" or vars'
            '"GITHUB_APP_ID" and "GITHUB_APP_KEY_ARN".')

    def get_client(self, owner):
        """Select the correct GitHub client for a repository.

        Private repositories need the client associated with the app
        integration. Public repositories can use any client. This routine looks
        for an app integration and if it fails a random client is returned. Note
        that if we add private repositories without an integration, then we
        will get authorization errors.
        """
        owner = owner.lower()
        if owner in self._app_clients:
            return self._app_clients.get(owner.lower())
        return random.choice(list(self._app_clients.values()))

    def get_active_prs(self, repo, owner):
        print(f'Fetching pull requests for {owner}/{repo}')
        repo = self.get_repository(repo, owner)
        return repo.get_pulls(state='open')

    def get_repository(self, repo, owner):
        client = self.get_client(owner)
        return client.get_repo(f'{owner}/{repo}')

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
