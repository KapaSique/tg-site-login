FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
COPY examples ./examples
RUN pip install .

RUN useradd --system --uid 10001 --no-create-home app
USER app

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3)"

# The API. The bot service overrides this with: python -m tg_site_login.bot
CMD ["uvicorn", "tg_site_login.wiring:create_app_from_env", "--factory", "--host", "0.0.0.0", "--port", "8000"]
