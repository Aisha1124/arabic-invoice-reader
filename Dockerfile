# Demo image, built by the host (Render) from this repo, not locally.
# Copies only requirements.txt, app/ and static/: no .env, .cache/, data/ or eval/ can
# reach the image, and DEMO_MODE=1 means nothing reads an API key. Every requirement
# has a prebuilt manylinux wheel for Python 3.12, zxing-cpp and Pillow included, so
# nothing compiles.
FROM python:3.12-slim

# Run as an unprivileged user, not root.
RUN useradd --create-home --uid 1000 user
WORKDIR /home/user/app

COPY --chown=user requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY --chown=user app ./app
COPY --chown=user static ./static

USER user
ENV DEMO_MODE=1 \
    PYTHONUNBUFFERED=1

# Render tells the service which port to bind in PORT; 7860 when nothing does.
# sh expands the variable; exec makes uvicorn PID 1 so it receives stop signals.
EXPOSE 7860
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port \"${PORT:-7860}\""]
