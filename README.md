# JEDI-CI

A GitHub Action for running JEDI unit tests and integration tests by building
the jedi-bundle with target repositories.

## Overview

JEDI-CI is a GitHub Action that replaces the previous "JCSDA-internal/ci"
continuous integration tool. It provides simplified infrastructure for testing
testing JEDI repositories by building the jedi-bundle and running unit test
and integration tests.

This action includes following improvements over the previous system

- Eliminates AWS Lambda: Replaces the AWS Lambda-based actuator script with
  a fast running GitHub Action allowing easier debugging and better error visibility.
- Streamlined CMake hanndling:
  * Eliminates unnecessary repoistory hash collection for non-test repositories
  * CMake file rewriting is now handled in the actuator rather than during test
    execution (reducing data packaging and transfer complexity)
  * Rewriting functionality is now implemented as a library instead of a script
    with numerous arguments

## Project Structure

### Core Components

| Component | Description |
|-----------|-------------|
| [`action.yml`](./action.yml) | Defines the GitHub Action interface and arguments |
| [`Dockerfile`](./Dockerfile) | Container environment definition and build instructions |
| [`ci_action/`](./ci_action/) | Python library and entry point for test actuator (launches AWS Batch test execution) |
| [`shell/`](./shell/) | Shell scripts for test execution on AWS Batch infrastructure |
| [`cfn/`](./cfn/) | Infrastructure as code, used to define the AWS Batch backend and associated infrastructure |
| [`test/`](./test/) | Tests for testing my tests  |

## Usage

Put this in a GitHub workflow

```
jobs:
  jedi-ci:
    runs-on: ubuntu-latest
    permissions:
      id-token: write
      contents: read
    steps:
      - name: Generate CI App token
        id: generate-token
        uses: actions/create-github-app-token@v1
        with:
          # Owner is specified to scope the token to the org install
          # otherwise the token will be scoped to the repository.
          app-id: 321361
          private-key: ${{ secrets.CI_APP_PRIVATE_KEY }}
          owner: ${{ github.repository_owner }}

      - name: checkout repository
        uses: actions/checkout@v4
        with:
          path: target_repository

      - name: Configure AWS credentials
        uses: aws-actions/configure-aws-credentials@v4
        with:
          # This role only has the permission to write to our lfs archive s3 bucket path.
          role-to-assume: arn:aws:iam::747101682576:role/service-role/jedi-ci-action-runner-backend-GitHubActionsIAMRole-HkHdJRVEFw3x
          aws-region: us-east-2

      - name: Run JEDI CI
        uses: JCSDA-internal/jedi-ci@feature/ci-v3
        with:
          container_version: 'latest'
        env:
          TARGET_REPO_DIR: target_repository
          GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
          JEDI_CI_TOKEN: ${{ steps.generate-token.outputs.token }}
```
