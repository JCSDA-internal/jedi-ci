#!/usr/bin/env python3
import argparse
import time
import sys

# Python has a bunch of jwt implementations, but PyJWT is by far the most used.
# Unfortunately, calling "pip install jwt" will install a lesser used package
# that breaks the GitHub API wrapper.
try:
    import jwt
    from jwt import PyJWT
except ImportError:
    raise EnvironmentError(
        'generate_github_token.py requires PyJWT>=2.0 and cannot use '
        'the "jwt" library developed by Gehirn Inc. To fix this error '
        'first uninstall jwt by running `pip3 uninstall jwt` then install '
        'PyJWT by running `pip3 install PyJWT`.')

# Args
PARSER = argparse.ArgumentParser(
    description='Generate a JWT for GitHub application authentication.')
PARSER.add_argument(
    '--pem-file',
    required=True,
    help='Path to the PEM file of the GitHub app private key.')
PARSER.add_argument(
    '--app-id',
    required=True,
    help='The ID of the GitHub application.')


def generate_token(pem_file, app_id):
    """Use a PEM key file and Application ID to generate a GitHub app JWT."""

    with open(pem_file, 'r') as f:
        key_text = f.read()

    payload = {
        # Issued at time, subtract 60 seconds to account for clock drift.
        'iat': int(time.time()) - 60,
        # JWT expiration time.
        'exp': int(time.time()) + 600,
        # GitHub App's identifier
        'iss': app_id
    }

    encoded_jwt = jwt.encode(payload, key_text, algorithm='RS256')
    if isinstance(encoded_jwt, bytes):
        encoded_jwt = encrypted.decode("utf-8")
    return encoded_jwt


if __name__ == '__main__':
    args = PARSER.parse_args()
    encoded_jwt = generate_token(args.pem_file, args.app_id)
    print(f"{encoded_jwt}")
