FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN python -m unittest discover -b -s tests
EXPOSE 8080
# Offline by default. For Gemini: docker run -e GEMINI_API_KEY=... -p 8080:8080 acme-invoices --mode gemini
ENTRYPOINT ["python", "main.py", "--serve", "--bind", "0.0.0.0", "--db", "/data/acme.db"]
