FROM python:3.11-slim

WORKDIR /app

# Install git (needed for cloning repos)
RUN apt-get update && apt-get install -y git && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python", "ultimate_hosting_bot_fixed.py"]
