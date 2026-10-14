# Interim backend + review page for the field trial. Build from the repo root:
#   docker build -f deploy/receiver.Dockerfile -t pop-monitor/receiver:0.1.0 .
FROM python:3.12-slim
RUN useradd -r -u 10001 pop && mkdir /data && chown pop /data
# numpy + Pillow: matching PoP images against the booked artwork (Bookings page)
RUN pip install --no-cache-dir "numpy>=1.26" "pillow>=10"
COPY tools/receiver.py tools/popscore.py tools/store.py /app/
USER pop
EXPOSE 8080
VOLUME /data
HEALTHCHECK --interval=30s --timeout=5s CMD python3 -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8080/healthz')"
ENTRYPOINT ["python3", "/app/receiver.py", "--bind", "0.0.0.0", "--port", "8080", "--out", "/data"]
