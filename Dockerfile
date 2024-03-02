FROM python:3.9-slim

WORKDIR /usr/src/app

COPY build .

RUN pip install --no-cache-dir -r requirements.txt

CMD ["python", "-u", "-m", "kopf", "run", "--all-namespaces", "app.py"]
