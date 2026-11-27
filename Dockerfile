# Анализатор БАД на собственном сервере: веб-интерфейс + API + серверное распознавание фото.
#   docker build -t bad-checker .
#   docker run -d -p 8000:8000 --restart unless-stopped bad-checker
FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-rus tesseract-ocr-eng \
      tesseract-ocr-deu tesseract-ocr-fra tesseract-ocr-spa tesseract-ocr-ita \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir pytesseract pillow
COPY bad_checker ./bad_checker
COPY site ./site

RUN useradd --create-home app
USER app
EXPOSE 8000
CMD ["python", "-m", "bad_checker.web", "--host", "0.0.0.0", "--port", "8000"]
