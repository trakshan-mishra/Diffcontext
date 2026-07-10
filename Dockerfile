# Use python:3.11-slim as a lightweight base image
FROM python:3.11-slim

# Install system dependencies (git is needed for diffcontext)
RUN apt-get update && apt-get install -y git && rm -rf /var/lib/apt/lists/*

# Set up user with UID 1000 (Hugging Face Spaces requirement)
RUN useradd -m -u 1000 user
WORKDIR /app

# Copy configuration and project files
COPY pyproject.toml /app/
COPY README.md /app/
COPY diffcontext /app/diffcontext
COPY diffcontext-service /app/diffcontext-service

# Install package dependencies and extra tools
RUN pip install --no-cache-dir fastapi uvicorn python-multipart .

# Grant write permissions for cached SQLite database and temp directories
RUN chown -R user:user /app

# Switch to the non-root user
USER user

# Expose port 7860 (Hugging Face Spaces requirement)
EXPOSE 7860

# Start the FastAPI server using Uvicorn
CMD ["uvicorn", "diffcontext-service.backend.main:app", "--host", "0.0.0.0", "--port", "7860"]
