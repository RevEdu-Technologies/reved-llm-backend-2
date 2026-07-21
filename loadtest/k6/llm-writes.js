// Scenario 2 — LLM writes.
//
// 20 concurrent VUs hit POST /student/ask at ~1 req/min each for 10 min.
// LLM_LIMIT in the backend is 10/min per caller (slowapi), so 1/min per
// VU stays well under the per-key limit; aggregate ≈ 20 req/min hits
// Groq for real.
//
// On staging, EXPECT some 429s if multiple VUs share the same auth key
// (dev-mode is per-role, so all 20 VUs share one key in AUTH_MODE=dev).
// Provision distinct test-user JWTs and pass via TOKEN per-VU group to
// get a clean run.
//
// Run:
//   BASE_URL=https://reved-staging.example.com k6 run loadtest/k6/llm-writes.js

import http from "k6/http";
import { sleep } from "k6";
import { baseUrl, buildHeaders, checkOk } from "./common.js";

export const options = {
  scenarios: {
    llm_writes: {
      executor: "constant-vus",
      vus: 20,
      duration: "10m",
    },
  },
  thresholds: {
    // Allow up to 20% non-success because some calls may be rate-limited
    // when AUTH_MODE=dev (all VUs share one rate-limit key).
    "http_req_failed{endpoint:student_ask}": ["rate<0.20"],
    // p95 budget for a single Groq round-trip incl. retrieval.
    "http_req_duration{endpoint:student_ask}": ["p(95)<5000"],
  },
};

const QUESTIONS = [
  "What is photosynthesis and how does it work?",
  "Explain Newton's three laws of motion.",
  "How do you solve a quadratic equation?",
  "What causes the seasons on Earth?",
  "Why is the sky blue?",
  "What is the difference between mitosis and meiosis?",
  "Explain how the heart pumps blood.",
  "What is the periodic table and how is it organized?",
];

const CLASSES = ["JSS1", "JSS2", "JSS3", "SS1", "SS2"];
const SUBJECTS = ["biology", "physics", "chemistry", "mathematics"];

function pick(arr) {
  return arr[Math.floor(Math.random() * arr.length)];
}

export default function () {
  const base = baseUrl();
  const headers = buildHeaders("student");

  const body = JSON.stringify({
    question: pick(QUESTIONS),
    student_class: pick(CLASSES),
    subject: pick(SUBJECTS),
  });

  const r = http.post(`${base}/api/v1/student/ask`, body, {
    headers,
    tags: { endpoint: "student_ask" },
    timeout: "30s",
  });
  checkOk(r, "student_ask");

  // 1 req/min per VU.
  sleep(60);
}
