# Web, token and seed services all run from this one image (11-03, WD-3).
# The base is pinned to an exact tag (STACK-03).
FROM python:3.12.14-slim-trixie

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Runtime dependencies come from the exact pins in pyproject.toml. The project
# itself is never pip-installed: that would move src into site-packages and
# break DEMO_DATA_DIR, which resolves to /app/demo-data from /app/src.
# Only direct dependencies are pinned, so transitive versions resolve at build
# time; this drift is accepted (D-06, no constraints file).
COPY pyproject.toml ./
RUN python -c "import tomllib, pathlib; deps = tomllib.loads(pathlib.Path('pyproject.toml').read_text())['project']['dependencies']; pathlib.Path('/tmp/requirements.txt').write_text('\n'.join(deps) + '\n')" \
    && pip install -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt

# Explicit paths only; .dockerignore is a deny-all allow-list. verify_counts.py serves make verify (T083).
COPY src/ ./src/
COPY scripts/mock_oauth_token_server.py ./scripts/mock_oauth_token_server.py
COPY scripts/verify_counts.py ./scripts/verify_counts.py
COPY demo-data/synthetic/ ./demo-data/synthetic/

# Non-root runtime user; a new named volume inherits /data's ownership.
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin tbweb \
    && mkdir /data \
    && chown 10001:10001 /data

USER 10001

EXPOSE 8000

CMD ["python", "-m", "uvicorn", "src.web.app:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
