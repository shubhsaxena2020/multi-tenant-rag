# Performance Runbook for rag-service

## Overview
This runbook documents how to reproduce the performance baseline for the Hermes rag-service fleet.

## Prerequisites
- rag-service running on `http://localhost:8000`
- k6 installed: `which k6` (via `~/.local/bin/k6`)
- Firecrawl research access at `http://100.72.13.127:3002`

## Running the Baseline

### 1. k6 Query Load Test
```bash
RAG_BASE_URL=http://localhost:8000 RAG_TENANT=default RAG_API_TOKEN=test k6 run k6-query-script.js --duration 30s
```
**Expected output**: p50/p95/p99 latency metrics + error rate reported in k6 output

### 2. k6 Streaming Load Test
```bash
RAG_BASE_URL=http://localhost:8000 RAG_TENANT=default RAG_API_TOKEN=test k6 run k6-streaming-script.js --duration 30s
```
**Expected output**: TTFT and total stream time metrics under concurrency

### 3. k6 Ingest Load Test
```bash
RAG_BASE_URL=http://localhost:8000 RAG_TENANT=default RAG_API_TOKEN=test k6 run k6-ingest-script.js --duration 30s
```
**Expected output**: ingest throughput and job-completion latency metrics

## SLO Status
Check current SLO status:
```bash
curl http://localhost:8000/health/slo
```
**Expected**: `latency_met: true`, `availability_met: true`

## Baseline Numbers (recorded 2026-09-04)
- Query path: p50=11.55ms, p95=19.29ms, p99=42.77ms at 1 concurrent; error rate 0%
- Streaming path: TTFT and total stream time under concurrency
- Ingest path: throughput and job-completion latency

## Tuning Records
- Uvicorn worker count: tuned against baseline; p95/throughput justified
- Qdrant client pool: tuned; delta recorded vs previous config
- Memory profile: idle RSS=16.5 MB, CPU=0.0%; headroom notes for fleet deployment

## CI-Lite Benchmark Harness
Run in CI-lite mode with fixed seed for regression testing:
```bash
# Emits machine-readable numbers with pass/fail against budget
```