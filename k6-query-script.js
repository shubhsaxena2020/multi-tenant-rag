import http from 'k6/http';
import { check, sleep } from 'k6';
import { Trend, Rate } from 'k6/metrics';

// Custom metrics for query performance
export const query_latency = new Trend('query_latency'); // Trend automatically tracks p50/p95/p99
export const error_rate = new Rate('error_rate'); // error rate

// Configuration - stages: ramp-up to 50 VUs, steady state, then ramp-down
export const options = {
  stages: [
    { duration: '5m', target: 5 },   // ramp-up to 5 VUs
    { duration: '10m', target: 50 }, // ramp-up to 50 VUs
    { duration: '10m', target: 50 }, // steady state at 50 VUs
    { duration: '5m', target: 5 },   // ramp-down
    { duration: '1m', target: 0 },   // shut down
  ],
};

const BASE_URL = __ENV.RAG_BASE_URL || 'http://localhost:8000';
const TENANT = __ENV.RAG_TENANT || 'default';
const BEARER_TOKEN = __ENV.RAG_API_TOKEN || 'your-api-token-here';

// Sample query payloads for different query types
const QUERY_PAYLOADS = [
  // Standard query
  JSON.stringify({ question: 'What is the capital of France?', top_k: 5, generate: true }),
  // Context-heavy query
  JSON.stringify({ question: 'Tell me about machine learning algorithms', top_k: 10, generate: true }),
  // Multi-hop query (if supported)
  JSON.stringify({ question: 'What did the document say about embeddings?', top_k: 5, generate: true, hops: 1 }),
];

export default function () {
  const payload = QUERY_PAYLOADS[Math.floor(Math.random() * QUERY_PAYLOADS.length)];
  const params = {
    headers: {
      'Content-Type': 'application/json',
      'Authorization': `Bearer ${BEARER_TOKEN}`,
    },
  };

  const res = http.post(
    `${BASE_URL}/api/v1/${TENANT}/query`,
    payload,
    params
  );

  const success = check(res, {
    'query status 200': (r) => r.status === 200,
    'query has response': (r) => r.body.length > 0,
  });

  if (!success) {
    error_rate.add(1);
  }

  // Track latency - Trend automatically computes p50/p95/p99
  query_latency.add(res.timings.duration || res.timings.duration);

  sleep(0.5);
}