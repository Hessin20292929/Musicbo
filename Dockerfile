# Use an official Python runtime as a parent image
FROM python:3.10-slim

# Set the working directory in the container
WORKDIR /app

# Install system dependencies:
# - ffmpeg for audio processing
# - libsodium-dev for PyNaCl (helps ensure PyNaCl builds correctly)
# - libopus-dev for discord.py's voice (Opus audio codec)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    libsodium-dev \
    libopus-dev \
    && rm -rf /var/lib/apt/lists/*

# Copy the requirements file into the container at /app
COPY requirements.txt ./

# Install any needed packages specified in requirements.txt
# --no-cache-dir reduces image size
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application's code into the container at /app
COPY . .

# Command to run the bot when the container starts
# This assumes your bot token is set as an environment variable on Render
CMD ["python", "music_bot.py"]
