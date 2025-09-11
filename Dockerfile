# Use an official Python runtime as a parent image
FROM python:3.10-slim

# Install Tesseract OCR
RUN apt-get update && apt-get install -y tesseract-ocr

# Set the working directory in the container
WORKDIR /app

# Copy the dependencies file to the working directory
COPY requirements.txt .

# Install any needed packages specified in requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application's code
COPY . .

# Create the uploads directory
RUN mkdir -p /app/uploads

# Make port 5000 available to the world outside this container
EXPOSE 5000

# Define environment variable
ENV FLASK_APP app.py

# Run app.py when the container launches
CMD ["flask", "run", "--host=0.0.0.0"]
