FROM python:3.12-slim
ARG TORCH_INDEX_URL=https://download.pytorch.org/whl/cpu
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 MPLCONFIGDIR=/tmp/matplotlib
WORKDIR /opt/glazza
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg libglib2.0-0 libgl1 && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
COPY analyzer/requirements.txt analyzer/requirements.txt
RUN pip install torch==2.5.1 torchvision==0.20.1 --index-url ${TORCH_INDEX_URL} && pip install -r analyzer/requirements.txt && useradd --uid 10001 --create-home glazza
COPY analyzer ./analyzer
USER glazza
EXPOSE 8090
CMD ["python", "-m", "uvicorn", "analyzer.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8090", "--no-access-log"]
