#!/usr/bin/env bash
set -euo pipefail
BUCKET="$1"

mkdir -p data/index
python -c "
import json, glob
out = [json.load(open(f)) for f in glob.glob('data/restaurants/*.json') if not f.split('/')[-1].startswith('_')]
json.dump(out, open('data/index/restaurants.json', 'w'), indent=2)
"

aws s3 cp data/index/restaurants.json "s3://$BUCKET/index/restaurants.json" ${AWS_PROFILE:+--profile "$AWS_PROFILE"}
echo "Restaurant data published to s3://$BUCKET/index/restaurants.json"
echo "(Vectors live in Pinecone — run 'python ingest.py' to update those.)"
