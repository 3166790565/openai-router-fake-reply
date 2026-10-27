FROM python:3.12-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple \
    PIP_TRUSTED_HOST=pypi.tuna.tsinghua.edu.cn \
    PIP_DEFAULT_TIMEOUT=300 \
    PIP_RETRIES=5

# 构建时注入 git 版本号（deploy 脚本会传 --build-arg）
ARG GIT_COMMIT=unknown
ARG GIT_BRANCH=unknown
LABEL git.commit="${GIT_COMMIT}" \
      git.branch="${GIT_BRANCH}" \
      org.opencontainers.image.revision="${GIT_COMMIT}"
ENV APP_GIT_COMMIT="${GIT_COMMIT}" \
    APP_GIT_BRANCH="${GIT_BRANCH}"

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

EXPOSE 8000
VOLUME ["/app/data"]

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]