// Scenario 3 — mixed.
//
// 30 minutes of 80% reads / 20% LLM writes. Reads are evenly split
// across the same three endpoints as reads.js; writes hit /student/ask.
// Total aggregate ≈ 100 req/s read + 1-2 req/s LLM-write.
//
// Run:
//   BASE_URL=https://reved-staging.example.com k6 run loadtest/k6/mixed.js

import http from "k6/http";
import { sleep } from "k6";
import { baseUrl, buildHeaders, checkOk } from "./common.js";

export const options = {
  scenarios: {
    reads: {
      executor: "constant-arrival-rate",
      rate: 100,             // req/s
      timeUnit: "1s",
      duration: "30m",
      preAllocatedVUs: 50,
      maxVUs: 200,
      exec: "readsIteration",
    },
    writes: {
      executor: "constant-arrival-rate",
      rate: 1,               // req/s
      timeUnit: "1s",
      duration: "30m",
      preAllocatedVUs: 10,
      maxVUs: 40,
      exec: "writesIteration",
    },
  },
  thresholds: {
    "http_req_failed{kind:read}": ["rate<0.01"],
    "http_req_failed{kind:write}": ["rate<0.20"],
    "http_req_duration{kind:read}": ["p(95)<1000"],
    "http_req_duration{kind:write}": ["p(95)<5000"],
  },
};

const ROLES = ["student", "parent", "teacher"];

export function readsIteration() {
  const role = ROLES[Math.floor(Math.random() * ROLES.length)];
  const base = baseUrl();
  const headers = buildHeaders(role);

  const path =
    role === "student"
      ? "/api/v1/student/conversations"
      : role === "parent"
      ? "/api/v1/parent/child-activity"
      : "/api/v1/teacher/class-progress";

  const r = http.get(`${base}${path}`, {
    headers,
    tags: { kind: "read", endpoint: `read_${role}` },
  });
  checkOk(r, `read_${role}`);
}

export function writesIteration() {
  const base = baseUrl();
  const headers = buildHeaders("student");
  const body = JSON.stringify({
    question: "What is the speed of light?",
    student_class: "SS1",
    subject: "physics",
  });
  const r = http.post(`${base}/api/v1/student/ask`, body, {
    headers,
    tags: { kind: "write", endpoint: "student_ask" },
    timeout: "30s",
  });
  checkOk(r, "student_ask");
}
