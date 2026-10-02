# Playwright's official image ships a matching Python + Chromium + system
# deps combo, so the audit engine doesn't need its own browser-install step.
FROM mcr.microsoft.com/playwright/python:v1.48.0-jammy

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/

# The commit this image was built from. scripts/deploy-box.sh passes it and
# /health reports it, so which build is serving is a fact rather than a
# guess. Last, so a new commit rebuilds nothing but this line.
ARG BUILD_SHA=unknown
ENV HUBVIBE_BUILD_SHA=$BUILD_SHA

EXPOSE 8080

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
