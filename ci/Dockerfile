FROM alpine:3.22
# Build this administrator-owned tools image and pin its digest in GitLab.
RUN apk add --no-cache curl python3 helm ca-certificates
USER 1000:1000
ENV PYTHONDONTWRITEBYTECODE=1
