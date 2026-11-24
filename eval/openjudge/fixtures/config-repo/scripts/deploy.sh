#!/usr/bin/env bash
set -euo pipefail
kubectl apply -f deploy.yaml
kubectl rollout status deployment/checkout
