ARG BUILDKIT_VERSION=v0.25.0
FROM moby/buildkit:${BUILDKIT_VERSION} AS buildkit
FROM python:3.12-alpine
COPY --from=buildkit /usr/bin/buildctl /usr/local/bin/buildctl
WORKDIR /app
COPY server.py .
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DOCKER_CONFIG=/registry-auth
USER 1000:1000
EXPOSE 8080
ENTRYPOINT ["python", "/app/server.py"]
