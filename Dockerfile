
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
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
CMD ["python", "testy.py","--api","https://pooltest.de2.netrun.io/s1/43A5jC4mPkP6FLtqQtfWn7QjyDnZzWo6abq9a3TqCXe5j2585CHAoxTjRmPyW2dvjbURaqEwoaU9wZ5v312jt3agP1wHky8/x1","--full-mem"]
