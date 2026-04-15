"""S3-compatible storage for uploading PDFs and manifests (DO Spaces, AWS S3, MinIO)."""
from __future__ import annotations

import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)


class S3Uploader:
    """Upload files to an S3-compatible bucket.

    Configured entirely via environment variables:
        ITAT_SPACES_BUCKET  — bucket name (e.g. "itat-archive")
        ITAT_SPACES_REGION  — region (e.g. "blr1" for DO, "us-east-1" for AWS)
        ITAT_SPACES_KEY     — access key ID
        ITAT_SPACES_SECRET  — secret access key
        ITAT_SPACES_ENDPOINT — (optional) custom endpoint URL.
                               Defaults to https://{region}.digitaloceanspaces.com
    """

    def __init__(self) -> None:
        import boto3

        self.bucket = os.environ["ITAT_SPACES_BUCKET"]
        region = os.environ["ITAT_SPACES_REGION"]
        endpoint = os.environ.get(
            "ITAT_SPACES_ENDPOINT",
            f"https://{region}.digitaloceanspaces.com",
        )
        self._client = boto3.client(
            "s3",
            endpoint_url=endpoint,
            region_name=region,
            aws_access_key_id=os.environ["ITAT_SPACES_KEY"],
            aws_secret_access_key=os.environ["ITAT_SPACES_SECRET"],
        )

    def upload_file(self, local_path: Path, s3_key: str) -> None:
        """Upload a single file. Overwrites if the key already exists."""
        self._client.upload_file(str(local_path), self.bucket, s3_key)
        log.debug("uploaded %s -> s3://%s/%s", local_path, self.bucket, s3_key)

    def upload_pdf(self, local_path: Path, bench: str, year: int) -> str:
        """Upload a PDF and return the S3 key used."""
        key = f"{bench}/{year}/{local_path.name}"
        self.upload_file(local_path, key)
        return key

    def upload_leaf_files(self, leaf: Path, bench: str, year: int) -> None:
        """Upload manifest.jsonl, failures.csv, missing_pdfs.csv for a leaf."""
        for name in ("manifest.jsonl", "failures.csv", "missing_pdfs.csv"):
            path = leaf / name
            if path.is_file():
                self.upload_file(path, f"{bench}/{year}/{name}")


def create_uploader() -> S3Uploader | None:
    """Create an S3Uploader if the required env vars are set, else None."""
    required = ("ITAT_SPACES_BUCKET", "ITAT_SPACES_REGION",
                "ITAT_SPACES_KEY", "ITAT_SPACES_SECRET")
    if all(os.environ.get(k) for k in required):
        return S3Uploader()
    return None
