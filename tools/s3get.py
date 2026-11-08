#!/usr/bin/env python3
"""Download one object from the old network volume's S3 API.

    s3get.py <bucket> <key> <local file>

Credentials/endpoint/region come from AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY,
AWS_ENDPOINT_URL, AWS_DEFAULT_REGION. Uses boto3 rather than the aws CLI: the
CLI's requests get 403 from RunPod's S3 API, boto3's (path-style, no default
integrity checksums) do not.
"""
import os
import sys

import boto3
from boto3.s3.transfer import TransferConfig
from botocore.config import Config

bucket, key, dest = sys.argv[1:4]
s3 = boto3.client(
    "s3",
    endpoint_url=os.environ["AWS_ENDPOINT_URL"],
    region_name=os.environ["AWS_DEFAULT_REGION"],
    config=Config(
        s3={"addressing_style": "path"},
        retries={"max_attempts": 10, "mode": "standard"},
        request_checksum_calculation="when_required",
        response_checksum_validation="when_required",
    ),
)
size = s3.head_object(Bucket=bucket, Key=key)["ContentLength"]
print(f"{key}: {size / 1e9:.2f} GB", flush=True)
s3.download_file(bucket, key, dest,
                 Config=TransferConfig(multipart_chunksize=64 * 1024 * 1024, max_concurrency=8))
got = os.path.getsize(dest)
if got != size:
    sys.exit(f"size mismatch: got {got}, want {size}")
