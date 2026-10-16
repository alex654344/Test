
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       librandomx0 \
       librandomx-dev \
       coreutils \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY Testy1.py .
COPY run.py .
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
CMD ["python", "run.py"]
