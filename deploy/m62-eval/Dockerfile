FROM cozedev/coze-studio-server:latest AS cached_python
FROM postgres:16.12-alpine3.23 AS dependencies
# Reuse only the cached Alpine interpreter, not Coze code/configuration.
COPY --from=cached_python /app/.venv/bin/python3 /usr/bin/python3.12
COPY --from=cached_python /usr/lib/libpython3.12.so.1.0 /usr/lib/
COPY --from=cached_python /usr/lib/python3.12 /usr/lib/python3.12
COPY --from=cached_python /usr/lib/libffi.so.8* /usr/lib/
COPY --from=cached_python /usr/lib/libbz2.so.1* /usr/lib/
COPY --from=cached_python /usr/lib/liblzma.so.5* /usr/lib/
COPY --from=cached_python /usr/lib/libsqlite3.so.0* /usr/lib/
COPY --from=cached_python /usr/lib/libexpat.so.1* /usr/lib/
COPY --from=cached_python /app/.venv/lib/python3.12/site-packages/pip /opt/bootstrap/pip
ENV PATH="/opt/evalvenv/bin:/usr/bin:/bin:/usr/local/bin:/usr/sbin:/sbin" PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
COPY requirements*.lock pyproject.toml /opt/dependencies/
ARG HTTP_PROXY=http://host.docker.internal:7892
ARG HTTPS_PROXY=http://host.docker.internal:7892
ARG ALL_PROXY=http://host.docker.internal:7892
RUN /usr/bin/python3.12 -m venv --without-pip /opt/evalvenv && \
    PYTHONPATH=/opt/bootstrap /opt/evalvenv/bin/python -m pip install --disable-pip-version-check --retries 0 --timeout 60 \
      -r /opt/dependencies/requirements-formal-eval.lock \
      fastapi==0.146.0 SQLAlchemy==2.1.5 alembic==1.20.0 psycopg[binary]==3.3.6 uvicorn==0.54.0 jieba==0.42.1 pytest==9.0.2
RUN addgroup -g 10001 evalcontrol && adduser -D -u 10001 -G evalcontrol evalcontrol && \
    addgroup -g 10002 evalapp && adduser -D -u 10002 -G evalapp evalapp && \
    addgroup -g 10003 evalissuer && adduser -D -u 10003 -G evalissuer evalissuer
RUN PYTHONPATH=/opt/bootstrap /opt/evalvenv/bin/python -m pip install --disable-pip-version-check --retries 0 --timeout 60 openai==2.14.0
FROM scratch AS application
COPY --from=dependencies / /
ENV PATH="/opt/evalvenv/bin:/usr/bin:/bin:/usr/local/bin:/usr/sbin:/sbin" PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /opt/shoprelay
COPY code/ /opt/shoprelay/
USER 10002:10002
ENTRYPOINT ["/opt/evalvenv/bin/python"]
CMD ["-m","uvicorn","eval.m6_1.container_boundary:app","--host","0.0.0.0","--port","8080"]
FROM application AS control
COPY control/ /opt/shoprelay/
ENV PYTHONPATH="/opt/shoprelay:/opt/shoprelay/tests"
USER 10001:10001
CMD ["-c","import time; time.sleep(8640000)"]
