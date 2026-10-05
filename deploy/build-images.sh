#!/bin/sh
# Build both images on the Docker host (run from anywhere inside the repo).
# Then deploy deploy/docker-compose.yml as a Portainer stack.
set -e
cd "$(dirname "$0")/.."
VERSION=${VERSION:-0.1.0}
docker build -f gateway/Dockerfile        -t pop-monitor/gateway:$VERSION  -t pop-monitor/gateway:latest .
docker build -f deploy/receiver.Dockerfile -t pop-monitor/receiver:$VERSION -t pop-monitor/receiver:latest .
echo "Built pop-monitor/gateway:$VERSION and pop-monitor/receiver:$VERSION"
