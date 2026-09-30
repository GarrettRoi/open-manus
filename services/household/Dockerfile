FROM python:3.13-slim
RUN apt-get update && apt-get install -y --no-install-recommends poppler-utils && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY services/household/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt
COPY services/household /app/services/household
RUN useradd --uid 10001 --create-home household
USER household
CMD ["python", "-m", "services.household.app"]