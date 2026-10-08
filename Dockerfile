# Fruit Fly TV — works on Render, Railway, Fly.io, or any Docker host.
FROM python:3.12-slim
WORKDIR /app
COPY fruit-fly-tv.html serve.py ./
COPY logo ./logo
# The host usually injects PORT; this default covers plain `docker run -p 8765:8765`.
ENV PORT=8765 CACHE_DIR=/app/cache
RUN mkdir -p /app/cache
EXPOSE 8765
CMD ["python", "serve.py"]
