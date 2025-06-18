#!/usr/bin/env python3

# This script prints a GitHub Application authentication token to stdout.
#
# This script depends on the 'PyJWT' library and depends on configuration
# parameters set in the environment:
#    GITHUB_APP_PRIVATE_KEY: The file path of a GitHub app private key.
#    GITHUB_APP_ID: The application ID of the GitHub app.
#    GITHUB_INSTALL_ID: The install ID of the GitHub app.
#
# To use this script, set it as the value of git askPass core config.
#   git config --global core.askPass "git_askPass_app_credentials.py"

import os
import time
import sys
import subprocess
import json

# Python has a bunch of jwt implementations, but PyJWT is by far the most used.
# Unfortunately, calling "pip install jwt" will install a lesser used package
# that breaks the GitHub API wrapper.
try:
    import jwt
    from jwt import PyJWT
except ImportError:
    raise EnvironmentError(
        'git_askPass_app_credentials.py requires PyJWT>=2.0 and cannot use '
        'the "jwt" library developed by Gehirn Inc. To fix this error '
        'first uninstall jwt by running `pip3 uninstall jwt` then install '
        'PyJWT by running `pip3 install PyJWT`.')

_TEMP_TOKEN_STORE = f'{os.environ.get("HOME")}/.github_app_token'
_CURRENT_TIME = int(time.time())


def generate_token(pem_file, app_id, install_id, current_time):
    """Use a PEM key file and Application ID to generate a GitHub app JWT."""

    with open(pem_file, 'r') as f:
        key_text = f.read()
    # This is used to make sure that we never issue "future" tokens that will
    # not be honored by GitHub. This factor is also subtracted from local
    # expiration since GitHub allows 10 minutes TTL for auth tokens. 
    clock_drift_factor = 10
    issue_time = current_time - clock_drift_factor
    expiration_time = issue_time + 600

    payload = {
        'iat': issue_time,
        'exp': expiration_time,
        'iss': app_id
    }

    encoded_jwt = jwt.encode(payload, key_text, algorithm='RS256')
    if isinstance(encoded_jwt, bytes):
        encoded_jwt = encrypted.decode('utf-8')
    token_response = subprocess.check_output(
        ['curl',
         '--request', 'POST',
         '--url', f'https://api.github.com/app/installations/{install_id}/access_tokens',
         '--header', 'Accept: application/vnd.github+json',
         '--header', f'Authorization: Bearer {encoded_jwt}',
         '--header', 'X-GitHub-Api-Version: 2022-11-28'])
    token = json.loads(token_response).get('token')
    return token, expiration_time


def generate_or_fetch_token(pem_file, app_id, install_id):
    """Fetch a token from the cache or make a new one and update the cache."""
    expires_at = _CURRENT_TIME - 100  # Default; token is already expired.
    auth_token = ''
    # Get the existing token written by this script if it exists.
    if os.path.isfile(_TEMP_TOKEN_STORE):
        with open(_TEMP_TOKEN_STORE, 'r') as f:
            expires_at_raw, auth_token = f.read().strip().split(',', 1)
            expires_at = int(expires_at_raw)

    if expires_at > _CURRENT_TIME:
        return auth_token

    # Either the token file doesn't exist or the token expired, we must make
    # a new token and store it along with the expiration timestamp.
    auth_token, expires_at = generate_token(pem_file, app_id, install_id, _CURRENT_TIME)
    # Subtract an additional 5 seconds to prevent race condition of a token
    # being returned as "fresh" momentarily before expiring.
    expires_at = expires_at - 5
    with open(_TEMP_TOKEN_STORE, 'w') as f:
        f.write(f'{expires_at},{auth_token}')

    return auth_token

if __name__ == '__main__':
    # Validate environment.
    pem_file = os.environ.get('GITHUB_APP_PRIVATE_KEY')
    if not pem_file:
        raise EnvironmentError('Environment value GITHUB_APP_PRIVATE_KEY must be set')
    app_id = os.environ.get('GITHUB_APP_ID')
    if not app_id:
        raise EnvironmentError('Environment value GITHUB_APP_ID must be set')
    install_id = os.environ.get('GITHUB_INSTALL_ID')
    if not install_id:
        raise EnvironmentError('Environment value GITHUB_INSTALL_ID must be set.')
    # Validate file path from environment.
    if not os.path.isfile(pem_file):
        raise EnvironmentError(
            f'GITHUB_APP_PRIVATE_KEY "{pem_file}" is not a file.')

    encoded_jwt = generate_or_fetch_token(pem_file, app_id, install_id)
    print(f"{encoded_jwt}")
