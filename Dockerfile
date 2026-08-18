FROM python:3.11-slim

WORKDIR /app
COPY server.py .
COPY static ./static

# Most hosts inject PORT; DB_PATH should point at a mounted persistent
# volume if your host offers one (otherwise data is lost on redeploy).
ENV HOST=0.0.0.0
ENV COOKIE_SECURE=1

EXPOSE 8765
CMD ["python3", "server.py"]
