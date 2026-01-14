FROM python:3.11-slim


# install small system deps required by Playwright & browsers
RUN apt-get update && apt-get install -y \
wget ca-certificates libnss3 libatk1.0-0 libatk-bridge2.0-0 libx11-xcb1 libxcomposite1 libxdamage1 libxrandr2 libasound2 libgbm1 \
--no-install-recommends && rm -rf /var/lib/apt/lists/*


WORKDIR /app
COPY requirements.txt /app/
RUN pip install --upgrade pip
RUN pip install -r requirements.txt


# install browsers for Playwright
RUN python -m playwright install --with-deps


COPY . /app


ENV PORT=10000
EXPOSE 10000
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "10000"]
