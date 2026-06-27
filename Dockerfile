FROM python:3.12-slim

# CA certs para que las llamadas HTTPS por urllib (Supabase, OpenAI, Chatwoot, descarga de
# adjuntos) validen el certificado. Sin esto, SSL falla en runtime.
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml ./
COPY app ./app
RUN pip install --no-cache-dir .

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
