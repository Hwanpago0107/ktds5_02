FROM python:3.11-slim

# System deps
RUN apt-get update \
 && apt-get install -y --no-install-recommends nginx ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# Python deps
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

# App code
WORKDIR /app
COPY . /app

# Nginx config and start script
COPY deploy/nginx.conf /etc/nginx/nginx.conf
COPY deploy/start.sh /start.sh
# Normalize line endings to LF for Linux runtime and ensure execute bit
RUN sed -i 's/\r$//' /start.sh /etc/nginx/nginx.conf \
 && chmod +x /start.sh

# Expose Nginx port (App Service for Containers → set WEBSITES_PORT=8080)
EXPOSE 8080

ENV STREAMLIT_BROWSER_GATHERUSAGESTATS=false \
    STORAGE_BACKEND=sqlite \
    SQLITE_PATH=/app/data/app.db

CMD ["/start.sh"]

