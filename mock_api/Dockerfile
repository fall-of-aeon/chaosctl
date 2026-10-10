FROM python:3.12-slim
RUN pip install --no-cache-dir fastapi==0.118.0 uvicorn==0.37.0
COPY app.py /app/app.py
WORKDIR /app
EXPOSE 8080
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080"]
