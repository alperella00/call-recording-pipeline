FROM python:3.12-slim
WORKDIR /crm
COPY server.py calendar.json ./
ENV PYTHONUNBUFFERED=1
EXPOSE 8080
CMD ["python", "server.py"]
