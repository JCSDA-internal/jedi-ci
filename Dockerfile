# This Dockerfile is used by GitHub Actions to build the JEDI-CI runner container.
# These steps are executed in a step immediately prior to the entrypoint
# which executes the JEDI-CI action.
FROM ghcr.io/jcsda-internal/jedi-ci-base:latest

# Copy launcher package
COPY . /app

# Install the launcher package
RUN cd /app && ls -la && pip install .

# Set entrypoint
ENTRYPOINT ["jedi_ci"]
