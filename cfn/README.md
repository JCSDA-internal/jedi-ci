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

2. Create a CodePipeline Connection to the GitHub app through the AWS Console
   (CodePipeline->Settings->Connections). See
   [Step 2: Create a connection to Github](https://docs.aws.amazon.com/codepipeline/latest/userguide/connections-github.html#connections-github-console)
   for details.

3. Create a cloud trail to log s3 data calls.
   TODO: More directions
   TODO: Add to cloudformation

4. Identify the s3 bucket for the account used for CloudFormation templates or
   other IAC. Create this bucket if the account does not already have one.

5. Create the following secrets and note their ARN which will be needed to
   configure the CloudFormation template.
   * GitHub webhook secret string; text field. You will need to generate
     this string yourself since it will be used to configure the WebHook and
     the Lambda service. Use this command: `openssl rand -hex 15`.
   * GitHub application secret key; text field. Get the contents of the pem
     file generated for the GitHub application in step 1 and save it as a
     secret.

6. Upload all CloudFormation templates in ./cfn to the account's
   IAC/CloudFormation bucket. This step will be repeated when changes are made
   to the templates.
   * `aws s3 cp ./cfn/ s3://jcsda-usaf-iac-artifacts/CI/cfn/ --recursive --exclude "*" --include "*.yaml"`

7. Upload the Github Webhook Lambda folder to the bucket from Step 4. See
   [GitHub Webhooks Lambda Instructions](src/github_webhooks_lambda/README.md)
   for details.

8. Complete the GitHub webhooks lambda build process detailed below.

9. Create a new stack using the yaml loaded to your bucket in step 4.
  * Navigate to "CloudFormation" in the AWS console.
  * On the upper right, select "Create Stack" then click the "With new
    resources" option.
    - Select "Template is ready", then enter the S3 url of "jedi-ci.yaml". Note
      that the URL is an actual https URL (not a "s3://" path); it can be found
      by navigating to the template in the S3 browser.
    - Click "next"
  * Enter a unique but identifiable stack name, like "jedi-ci-pipeline" and fill
    all template variable values. Then click next.
  * Review default CloudFormation config values before launching the create
    process, don't change the defaults without justification.


## Lambda Build

This process only has to be done for the initial setup, or when the webhook code
has changed.

Build requirements: you must build this in a host with docker and with the intel
processor architecture. For users of Apple laptops with M series processors,
you will probably need build this on a cloud VM.

1) From the repository root, navigate to the webhook code. `cd src/github_webhooks_lambda`.
2) Run `./package.sh`
3) Copy the output zip layers to our s3 bucket.
  - `aws s3 cp github_webhooks_lambda.zip s3://jcsda-usaf-iac-artifacts/github_webhooks_lambda/github_webhooks_lambda.zip`
  - `aws s3 cp pygithub_layer.zip s3://jcsda-usaf-iac-artifacts/github_webhooks_lambda/pygithub_layer.zip`


# Future improvements
1. Filter GitHub events to appropriate events and document event setup with
   detailed event configuration.
2. Add CloudTrail creation to CloudFormation


## Updating the stack

* Load all templates and code to your artifacts bucket.
  - `aws s3 cp ./cfn/ s3://jcsda-usaf-iac-artifacts/CI/cfn/ --recursive --exclude "*" --include "*.yaml"`
* Using the AWS Console, navigate to your stack in CloudFormation.
* On the upper right, click "update"
* Select "Replace current template"
* Enter the URL of the updated "jedi-ci.yaml" file then click "Next" (it will
  probably be the same URL as used in the initial setup).
  - https://jcsda-usaf-iac-artifacts.s3.us-east-2.amazonaws.com/CI/cfn/jedi-ci.yaml
* Review all current and new parameters and update values as necessary. Then
  click "Next".
* Review CloudFormation options (preserved from initial setup), then click
  "Next".
* You will be given a full change-review page with a summary of the config and
  changes. To invoke the change, you must click "Submit" at the bottom right.

