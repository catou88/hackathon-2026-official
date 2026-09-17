FROM python:3.12-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    TMPDIR=/out/tmp XDG_CACHE_HOME=/out/cache
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
# Copy only runtime code: no credentials, benchmark labels, results, or raw telemetry.
COPY run.py ./
COPY mini_rca/ ./mini_rca/
COPY prompts/ ./prompts/
COPY knowledge/ ./knowledge/
CMD ["python", "run.py", "--dataset", "/data", "--queries", "/data/query.csv", "--out", "/out"]
