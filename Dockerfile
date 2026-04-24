FROM python:3.11-slim

WORKDIR /app

# Copy app files
COPY chat_server.py .
COPY index.html .

# Create media directory
RUN mkdir -p zapchat_media

# HF Spaces requires port 7860
ENV PORT=7860
EXPOSE 7860

# Run the server
CMD ["python3", "chat_server.py"]
