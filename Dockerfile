# This Dockerfile is used by GitHub Actions to build the JEDI-CI runner container.
# These steps are executed in a step immediately prior to the entrypoint
# which executes the JEDI-CI action.
FROM python:3.11-slim

# Install required packages
RUN apt-get update && apt-get install -y \
    git \
    jq \
    curl \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Install Python build tools
RUN pip install --no-cache-dir pip setuptools

# Copy launcher package
COPY . /app

# Install the launcher package
RUN cd /app && ls -la && pip install .

# Set entrypoint
ENTRYPOINT ["jedi_ci"]
