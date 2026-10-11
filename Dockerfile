FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY app.py ./
COPY finder ./finder
COPY static ./static
USER 65532:65532
EXPOSE 13006
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 CMD python -c "import urllib.request; urllib.request.build_opener(urllib.request.ProxyHandler({})).open('http://127.0.0.1:13006/api/health', timeout=3).close()"
CMD ["python", "app.py", "--host", "0.0.0.0", "--no-browser"]
