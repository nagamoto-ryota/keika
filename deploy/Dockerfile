# 審査用の公開版（Cloud Run）。架空データだけで動かす。
# 元の DB（base.db）はリポジトリに入れず、デプロイの時だけ置く。API キーと合言葉は Secret Manager から環境変数で渡す。
FROM python:3.12-slim
WORKDIR /app
COPY deploy/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY src ./src
COPY sample_data ./sample_data
COPY base.db /app/base/keika.db
ENV PYTHONPATH=/app/src \
    KEIKA_DRIVE_ROOT=/app/sample_data/drive \
    KEIKA_DB_PATH=/tmp/keika/keika.db \
    KEIKA_BASE_DB=/app/base/keika.db \
    KEIKA_AS_OF=2026-11-13 \
    KEIKA_DAILY_LIMIT_USD=10
CMD exec uvicorn keika.public:app_from_env --factory --host 0.0.0.0 --port ${PORT:-8080} --log-level warning
