import os
import json
import logging
from typing import Optional

logger = logging.getLogger("court_scraper.storage")
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

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
            target_bucket = S3_BUCKET.replace("s3://", "").strip().strip("/")
            
            s3 = boto3.client(
                "s3",
                region_name=S3_REGION,
                aws_access_key_id=AWS_ACCESS_KEY_ID,
                aws_secret_access_key=AWS_SECRET_ACCESS_KEY
            )

            # Auto-detect / verify bucket name
            try:
                resp = s3.list_buckets()
                user_buckets = [b["Name"] for b in resp.get("Buckets", [])]
                if target_bucket not in user_buckets:
                    if len(user_buckets) == 1:
                        target_bucket = user_buckets[0]
                    else:
                        for b in user_buckets:
                            if "court" in b or "florida" in b:
                                target_bucket = b
                                break
            except Exception as lb_err:
                logger.warning(f"Could not list buckets: {lb_err}")

            s3.upload_file(
                local_path,
                target_bucket,
                destination_key,
                ExtraArgs={"ContentType": "application/pdf" if local_path.endswith(".pdf") else "text/csv"}
            )
            url = f"https://{target_bucket}.s3.{S3_REGION}.amazonaws.com/{destination_key}"
            logger.info(f"[+] Uploaded to S3: {url}")
            print(f"    ├── Synced to S3: {url}")
            return url
        except Exception as e:
            logger.error(f"[-] S3 upload failed for {local_path}: {e}")
            print(f"    [!] S3 upload warning: {e}")
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


def verify_s3_contents() -> list:
    """Live audit of S3 bucket to verify all files currently in AWS."""
    if not (AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY):
        print("[!] AWS credentials not available in environment.")
        return []

    try:
        import boto3
        target_bucket = S3_BUCKET.replace("s3://", "").strip().strip("/") if S3_BUCKET else "florida-court-records-machine"
        s3 = boto3.client(
            "s3",
            region_name=S3_REGION,
            aws_access_key_id=AWS_ACCESS_KEY_ID,
            aws_secret_access_key=AWS_SECRET_ACCESS_KEY
        )

        try:
            resp = s3.list_buckets()
            user_buckets = [b["Name"] for b in resp.get("Buckets", [])]
            if target_bucket not in user_buckets:
                for b in user_buckets:
                    if "court" in b or "florida" in b:
                        target_bucket = b
                        break
        except Exception:
            pass

        print(f"\n{'='*60}")
        print(f"[*] LIVE AWS S3 VERIFICATION AUDIT")
        print(f"[*] Target Bucket: {target_bucket}")
        print(f"{'='*60}")

        paginator = s3.get_paginator("list_objects_v2")
        all_objects = []
        for p in paginator.paginate(Bucket=target_bucket):
            for obj in p.get("Contents", []):
                item = {
                    "key": obj["Key"],
                    "size": obj["Size"],
                    "last_modified": str(obj["LastModified"])
                }
                all_objects.append(item)
                print(f"  [AWS S3 CONFIRMED] {obj['Key']} ({obj['Size']:,} bytes, {obj['LastModified']})")

        print(f"[*] Total confirmed files in S3: {len(all_objects)}")
        print(f"{'='*60}\n")

        audit_path = os.path.join(BASE_DIR, "s3_verification_audit.json")
        with open(audit_path, "w") as f:
            json.dump({"bucket": target_bucket, "total": len(all_objects), "files": all_objects}, f, indent=2)

        return all_objects
    except Exception as e:
        print(f"[-] AWS S3 audit exception: {e}")
        return []

