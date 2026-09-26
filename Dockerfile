FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PORT=8080
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bot.py team.json conversation_handlers.py ./
COPY vera ./vera
EXPOSE 8080
# Exactly ONE worker: all state is in memory, and a second worker would not see it.
CMD ["sh", "-c", "uvicorn bot:app --host 0.0.0.0 --port ${PORT} --workers 1"]
