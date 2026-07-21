// Scenario 1 — reads.
//
// 100 concurrent VUs round-robin three read endpoints at ~1 req/s each
// for 10 min. The role rotates per-VU so we exercise the per-role
// scoping paths (student conversation list, parent child-activity,
// teacher class-progress) under realistic auth mix.
//
// Run:
//   k6 run loadtest/k6/reads.js
//
// Thresholds intentionally generous; tighten once you have baseline.

import http from "k6/http";
import { sleep } from "k6";
import { baseUrl, buildHeaders, checkOk, studentIdForReads } from "./common.js";

const DURATION = __ENV.DURATION || "10m";
const VUS = parseInt(__ENV.VUS || "100", 10);

export const options = {
  scenarios: {
    reads: {
      executor: "constant-vus",
      vus: VUS,
      duration: DURATION,
    },
  },
  thresholds: {
    "http_req_failed{endpoint:student_conversations}": ["rate<0.01"],
    "http_req_failed{endpoint:parent_child_activity}": ["rate<0.01"],
    "http_req_failed{endpoint:teacher_class_progress}": ["rate<0.01"],
    "http_req_duration{endpoint:student_conversations}": ["p(95)<1000"],
    "http_req_duration{endpoint:parent_child_activity}": ["p(95)<1000"],
    "http_req_duration{endpoint:teacher_class_progress}": ["p(95)<1000"],
  },
};

const ROLES = ["student", "parent", "teacher"];

export default function () {
  // Spread VUs across roles deterministically.
  const role = ROLES[__VU % ROLES.length];
  const base = baseUrl();
  const headers = buildHeaders(role);

  if (role === "student") {
    const r = http.get(`${base}/api/v1/student/conversations`, {
      headers,
      tags: { endpoint: "student_conversations" },
    });
    checkOk(r, "student_conversations");
  } else if (role === "parent") {
    const r = http.get(`${base}/api/v1/parent/child-activity`, {
      headers,
      tags: { endpoint: "parent_child_activity" },
    });
    checkOk(r, "parent_child_activity");
  } else {
    const r = http.get(`${base}/api/v1/teacher/class-progress`, {
      headers,
      tags: { endpoint: "teacher_class_progress" },
    });
    checkOk(r, "teacher_class_progress");
  }

  sleep(1);
}
