FROM mcr.microsoft.com/playwright/python:v1.46.0-jammy

WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY app.py /app/app.py
COPY run.sh /app/run.sh
RUN chmod +x /app/run.sh

EXPOSE 8080
CMD ["/app/run.sh"]
