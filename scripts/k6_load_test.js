import http from 'k6/http';
import { check, sleep } from 'k6';

export let options = {
    stages: [
        { duration: '2m', target: 10 },  // ramp up
        { duration: '10m', target: 10 }, // steady state
        { duration: '2m', target: 0 },   // ramp down
    ],
    thresholds: {
        'http_req_duration{type:query}' : ['p(95)<500', 'p(99)<1000'],
        'http_req_duration{type:ingest}' : ['p(95)<500', 'p(99)<1000'],
    },
};

export default function () {
    // Query path
    http.get('http://localhost:8000/health');
    sleep(0.5);
    
    // Ingest path (if available)
    try {
        http.post('http://localhost:8000/api/v1/ingest', '{"test":"data"}');
    } catch(e) {
        // ingest endpoint may not exist
    }
    sleep(0.5);
}