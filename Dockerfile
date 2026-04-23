FROM python:3.11-slim

WORKDIR /app

# Copy app files
COPY chat_server.py .
COPY index.html .

# Create media directory
RUN mkdir -p zapchat_media

# Expose port (Railway overrides with $PORT)
EXPOSE 8080

# Run the server
CMD ["python3", "chat_server.py", "--port", "7860"]
