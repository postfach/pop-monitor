# creatives-sync: pulls the booked artwork from SharePoint for the Bookings page.
# Build from the repo root:
#   docker build -f sync/Dockerfile -t pop-monitor/creatives-sync:0.1.0 .
FROM python:3.12-slim
RUN useradd -r -u 10001 pop && mkdir /creatives && chown pop /creatives
COPY sync/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt
COPY sync/mi.py sync/creatives_sync.py sync/pop_upload.py sync/doohly.py sync/videoframes.py /app/
COPY sync/tests /app/tests
# The unit tests run during the build, so a broken image is never produced.
RUN cd /app && OUT_DIR=/tmp/t STATE_DIR=/tmp/t/s python3 -m unittest discover -s tests && rm -rf /tmp/t
USER pop
EXPOSE 8091
VOLUME /creatives
HEALTHCHECK --interval=60s --timeout=5s CMD python3 -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8091/healthz')"
ENTRYPOINT ["python3", "/app/creatives_sync.py"]
