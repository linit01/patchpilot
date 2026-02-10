FROM python:3.11-slim

# Install Ansible and dependencies
RUN apt-get update && apt-get install -y \
    ansible \
    openssh-client \
    sshpass \
    && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Copy requirements and install Python dependencies
COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY backend/ .

# Create ansible directory
RUN mkdir -p /ansible

# Expose port
EXPOSE 8000

# Run the application
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
