#!/bin/bash

# Quick test script used to prep this for use in JCSDA-internal/test.

# Exit on error
set -e

# Docker image tag
IMAGE_TAG="eparker05/jedi-ci-test:test"

echo "Building Docker image: $IMAGE_TAG"
docker build -t $IMAGE_TAG .

echo "Pushing Docker image to Docker Hub"
docker push $IMAGE_TAG

echo "Build and push completed successfully" 