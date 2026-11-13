# Web UI + run pods for k3s: the UI (webui/app.py) starts Jobs that run jfstress.py --jsonl.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN pip install --no-cache-dir httpx==0.28.1 rich==15.0.0
WORKDIR /app
COPY jfcore.py jfstress.py /app/
COPY webui/ /app/webui/
USER 65534:65534
EXPOSE 8080
CMD ["python", "/app/webui/app.py"]
