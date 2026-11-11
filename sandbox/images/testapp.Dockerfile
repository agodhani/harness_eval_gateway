# heg-testapp — stub app for sandbox runtime tests (NOT miniloop).
# Build from repo root:
#   docker build -f sandbox/images/testapp.Dockerfile -t heg-testapp:latest sandbox/images/testapp
FROM python:3.12-slim
COPY app.py /app.py
CMD ["python", "/app.py"]
