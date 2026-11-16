FROM python:3.10-slim

# Install Docker CLI dependencies and Docker CLI itself
RUN apt-get update && apt-get install -y \
    docker.io \
    dcm2niix \
    bc \
    dc \
    && rm -rf /var/lib/apt/lists/*
# dc and bc are needed for FSL pipelines siena, sienax, xtract.
    
WORKDIR /app
 
# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Declare port
EXPOSE 9200

# Make the tmp directories with correct permission/ownership 
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh
ENTRYPOINT ["/entrypoint.sh"]

CMD ["python", "run.py"]