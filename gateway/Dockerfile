# pop-gateway container. Build from the repository root:
#   docker build -f gateway/Dockerfile -t pop-gateway .
#   docker run -d --name pop-gateway --restart unless-stopped \
#     -e TZ=Pacific/Auckland -e POP_API_KEY=... -e POP_HMAC_KEY=... \
#     -e POP_CAMERA_PASSWORD=... -e POP_TEAMS_WEBHOOK=... \
#     -v /etc/pop-gateway/config.json:/etc/pop-gateway/config.json:ro \
#     -v pop-queue:/var/lib/pop-gateway pop-gateway
FROM ubuntu:24.04 AS build
RUN apt-get update && apt-get install -y --no-install-recommends \
      build-essential cmake pkg-config \
      libavformat-dev libavcodec-dev libavutil-dev libswscale-dev \
      libcurl4-openssl-dev libturbojpeg0-dev libcjson-dev libssl-dev \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /src
COPY CMakeLists.txt .
COPY core core
COPY gateway gateway
COPY tools tools
RUN cmake -B build -DCMAKE_BUILD_TYPE=Release && cmake --build build -j && \
    ctest --test-dir build --output-on-failure

FROM ubuntu:24.04
RUN apt-get update && apt-get install -y --no-install-recommends \
      libavformat60 libavcodec60 libavutil58 libswscale7 \
      libcurl4t64 libturbojpeg libcjson1 ca-certificates tzdata ffmpeg \
    && rm -rf /var/lib/apt/lists/* && useradd -r -u 10001 pop && \
    mkdir -p /var/lib/pop-gateway/queue && chown -R pop /var/lib/pop-gateway
COPY --from=build /src/build/pop-gateway /src/build/popreplay /usr/local/bin/
LABEL org.opencontainers.image.title="pop-gateway" org.opencontainers.image.version="0.1.0"
WORKDIR /var/lib/pop-gateway
USER pop
VOLUME /var/lib/pop-gateway
ENTRYPOINT ["/usr/local/bin/pop-gateway", "-c", "/etc/pop-gateway/config.json"]
