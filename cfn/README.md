# Jedi CI pipeline

This directory contains AWS CloudFormation templates for the JEDI CI stack and
substacks to support the pipeline for presubmit testing, presubmit integration
testing and continuous integration.


## Parameters

See jedi-ci.yaml for the set of parameters that must be set for a new
deployment. Each additional substack yaml has parameters which may be tuned
by altering their default or by passing through to the jedi-ci config, although
most of these default values are acceptable or preferrable.


## Installation


### Before installation

While a great deal of the infrastructure is defined by the YAML files in this
diretory, these configs require some manual steps in their initial setup and
some manual steps to deploy. While some of the work below could be automated
a fair amount of it requires human intervention since it handles the boundaries
between system components.

In order to follow these instructions you must have a developer machine with
the AWS CLI installed and with access to the AWS console. You must have a GitHub
account and you must have admin access on the target GitHub Organization. Your
AWS IAM user must have full access to the following AWS services:
  - CloudFormation
  - S3
  - Lambda
  - CodePipeline
  - CodeBuild
  - Elastic Container Registry

### Installation steps

1. Create a GitHub app with permissions to the appropriate repos. Once the app
   is created ensure that the app is installed in the organization or in the
   target repositories. See [Creating a Github Application](https://docs.github.com/en/apps/creating-github-apps/creating-github-apps/creating-a-github-app)
   for details.
   * Once the app is created and installed, use the app administration panel to
     create a private key which you will be able to download as a ".pem" file.
     Save this for the later step where you will create AWS secrets.

2. Identify the S3 bucket for the account used for CloudFormation templates or
   other IAC. Create this bucket if the account does not already have one.

3. Create the GitHub application secret key; text field. Get the contents of the pem
   file generated for the GitHub application in step 1 and save it as a secret in AWS Secrets Manager.

4. Upload the jedi-ci-action.yaml template in jedi-ci/cfn to the account's
   IAC/CloudFormation bucket. This step will be repeated when changes are made
   to the template.
   * `aws s3 cp cfn/jedi-ci-action.yaml s3://jcsda-usaf-iac-artifacts/jedi-ci/cfn/jedi-ci-action.yaml`

5. Create a new stack using the yaml loaded to your bucket in step 4.
  * Navigate to "CloudFormation" in the AWS console.
  * On the upper right, select "Create Stack" then click the "With new
    resources" option.
    - Select "Template is ready", then enter the S3 url of "jedi-ci-action.yaml". Note
      that the URL is an actual https URL (not a "s3://" path); it can be found
      by navigating to the template in the S3 browser.
    - Click "next"
  * Enter a unique but identifiable stack name, like "jedi-ci-action" and fill
    all template variable values. Then click next.
  * Review default CloudFormation config values before launching the create
    process, don't change the defaults without justification.


## Updating the stack

* Upload the updated jedi-ci-action.yaml template to your artifacts bucket:
  - `aws s3 cp cfn/jedi-ci-action.yaml s3://jcsda-usaf-iac-artifacts/jedi-ci/cfn/jedi-ci-action.yaml`
* Use this bash script to get the latest version https link to the file.
  ```
  get_versioned_url() {
    file_name="$1"
    ci_file_version=$(aws s3api list-object-versions --bucket jcsda-usaf-iac-artifacts --prefix "${file_name}" | jq -r '.Versions[] | select(.IsLatest==true) | .VersionId')
    echo "https://jcsda-usaf-iac-artifacts.s3.us-east-2.amazonaws.com/${file_name}?versionId=${ci_file_version}"
  }
  get_versioned_url jedi-ci/cfn/jedi-ci-action.yaml
  ```
* Using the AWS Console, navigate to your stack in CloudFormation.
* On the upper right, click "update"
* Select "Replace current template"
* Enter the URL of the updated "jedi-ci-action.yaml" file then click "Next" (it will
  probably be the same URL as used in the initial setup).
  - https://jcsda-usaf-iac-artifacts.s3.us-east-2.amazonaws.com/CI/cfn/jedi-ci-action.yaml
* Review all current and new parameters and update values as necessary. Then
  click "Next".
* Review CloudFormation options (preserved from initial setup), then click
  "Next".
* You will be given a full change-review page with a summary of the config and
  changes. To invoke the change, you must click "Submit" at the bottom right.

