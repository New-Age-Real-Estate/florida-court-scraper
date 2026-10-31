import os
import json
import logging
from typing import Optional

logger = logging.getLogger("court_scraper.storage")

# Cloud Storage configuration
S3_BUCKET = os.getenv("S3_BUCKET")
S3_REGION = os.getenv("AWS_DEFAULT_REGION", "us-east-1")
AWS_ACCESS_KEY_ID = os.getenv("AWS_ACCESS_KEY_ID")
AWS_SECRET_ACCESS_KEY = os.getenv("AWS_SECRET_ACCESS_KEY")
WEBHOOK_URL = os.getenv("PIPELINE_WEBHOOK_URL")

def upload_file_to_cloud(local_path: str, destination_key: str) -> Optional[str]:
    """
    Uploads a downloaded vector PDF or report to cloud storage (S3/Compatible).
    Returns the public/signed URL or None if local-only.
    """
    if not os.path.exists(local_path):
        logger.error(f"File not found for upload: {local_path}")
        return None

    if S3_BUCKET and AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY:
        try:
            import boto3
            s3 = boto3.client(
                "s3",
                region_name=S3_REGION,
                aws_access_key_id=AWS_ACCESS_KEY_ID,
                aws_secret_access_key=AWS_SECRET_ACCESS_KEY
            )
            s3.upload_file(
                local_path,
                S3_BUCKET,
                destination_key,
                ExtraArgs={"ContentType": "application/pdf" if local_path.endswith(".pdf") else "text/csv"}
            )
            url = f"https://{S3_BUCKET}.s3.{S3_REGION}.amazonaws.com/{destination_key}"
            logger.info(f"[+] Uploaded to S3: {url}")
            return url
        except Exception as e:
            logger.error(f"[-] S3 upload failed for {local_path}: {e}")
            return None
    
    return local_path

def notify_downstream_machine(payload: dict):
    """
    Sends notification to your AI / ingestion pipeline webhook when daily batch is complete.
    """
    if not WEBHOOK_URL:
        return

    try:
        import urllib.request
        req = urllib.request.Request(
            WEBHOOK_URL,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            logger.info(f"[+] Notified machine webhook: HTTP {resp.status}")
    except Exception as e:
        logger.error(f"[-] Webhook notification failed: {e}")
