# Use Python 3.11 instead of 3.13
FROM python:3.11-slim

# Set working directory inside container
WORKDIR /app

# Copy requirements.txt into the container
COPY requirements.txt .

# Install dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the project files
COPY . .

# Run your bot
CMD ["python", "bot.py"]
