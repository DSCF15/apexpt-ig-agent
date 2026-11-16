FROM python:3.12-slim
WORKDIR /srv
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY knowledge ./knowledge
COPY scripts ./scripts
RUN useradd -m agent && chown -R agent /srv
USER agent
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
