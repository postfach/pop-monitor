# On-camera build (phase 2). From the repository root:
#   docker build -f acap/Dockerfile --build-arg ARCH=aarch64 -t popmonitor-acap .   # M5526-E
#   docker build -f acap/Dockerfile --build-arg ARCH=armv7hf -t popmonitor-acap .   # P3267-LV
#   docker cp $(docker create popmonitor-acap):/opt/app/acap/app ./build-acap
# The P3227-LVE runs AXIS OS 11.11: build it with an SDK VERSION that targets
# AXIS OS 11 (see Axis "devices and compatibility"), not 12.x.
ARG ARCH=aarch64
ARG VERSION=12.5.0
ARG UBUNTU_VERSION=24.04
ARG REPO=axisecp
ARG SDK=acap-native-sdk

FROM ${REPO}/${SDK}:${VERSION}-${ARCH}-ubuntu${UBUNTU_VERSION}

COPY core /opt/app/core
COPY acap/app /opt/app/acap/app
WORKDIR /opt/app/acap/app
RUN . /opt/axis/acapsdk/environment-setup* && acap-build ./
