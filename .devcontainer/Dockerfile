FROM mcr.microsoft.com/devcontainers/python:3.11-bookworm

ENV POETRY_VERSION=1.8.3

RUN pip install --no-cache-dir "poetry==${POETRY_VERSION}" && \
    mkdir -p /workspaces/Evolution-Coding

WORKDIR /workspaces/Evolution-Coding
