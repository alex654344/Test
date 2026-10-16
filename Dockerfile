
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       librandomx0 \
       librandomx-dev \
       coreutils \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY testy.py .
COPY run.py .

CMD ["python", "run.py"]
