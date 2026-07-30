FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY pyproject.toml README.md ./
COPY giftpulse ./giftpulse
RUN pip install --no-cache-dir .

# No shell, no root. A process that routes trades has no business running as root.
RUN useradd --create-home --shell /usr/sbin/nologin giftpulse
USER giftpulse

EXPOSE 8080
CMD ["giftpulse", "all"]
