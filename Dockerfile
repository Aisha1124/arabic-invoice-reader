# Demo image for a Hugging Face Docker Space. Built by Hugging Face, not locally.
# Copies only app/ and static/: no .env, .cache/, data/ or eval/ can reach the image,
# and DEMO_MODE=1 means nothing reads an API key. Every requirement has a prebuilt
# manylinux wheel for Python 3.12, zxing-cpp and Pillow included, so nothing compiles.
FROM python:3.12-slim

# Spaces run the container as user 1000.
RUN useradd --create-home --uid 1000 user
WORKDIR /home/user/app

COPY --chown=user requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY --chown=user app ./app
COPY --chown=user static ./static

USER user
ENV DEMO_MODE=1 \
    PYTHONUNBUFFERED=1

EXPOSE 7860
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "7860"]
