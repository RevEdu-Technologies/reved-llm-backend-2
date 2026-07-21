// Shared helpers for the RevEd k6 scenarios.
//
// Usage from each scenario:
//   import { buildHeaders, baseUrl, checkOk } from "./common.js";
//
// Env vars consumed:
//   BASE_URL  — defaults to http://localhost:8000. Point at staging in CI:
//               BASE_URL=https://reved-staging.example.com k6 run reads.js
//   AUTH_MODE — "dev" (default) sends X-Dev-Role; AUTH_ENABLED must be false
//               on the target. "bearer" sends Authorization: Bearer ${TOKEN}.
//   TOKEN     — required when AUTH_MODE=bearer.
//   STUDENT_ID — Student.id used by /student/goals/{id} reads. Optional;
//                falls back to the dev stub UUID below.

import { check } from "k6";

const STUB_USER_ID = "00000000-0000-0000-0000-000000000001";

export function baseUrl() {
  return __ENV.BASE_URL || "http://localhost:8000";
}

export function buildHeaders(role) {
  const mode = (__ENV.AUTH_MODE || "dev").toLowerCase();
  if (mode === "bearer") {
    const token = __ENV.TOKEN;
    if (!token) {
      throw new Error("AUTH_MODE=bearer requires TOKEN env var.");
    }
    return {
      Authorization: `Bearer ${token}`,
      "Content-Type": "application/json",
    };
  }
  return {
    "X-Dev-Role": role,
    "Content-Type": "application/json",
  };
}

export function studentIdForReads() {
  return __ENV.STUDENT_ID || STUB_USER_ID;
}

// One thin wrapper so every scenario tags failures with the endpoint
// and asserts on the standard envelope (status === "success" or a
// known soft-failure like rate_limited).
export function checkOk(response, label) {
  return check(
    response,
    {
      [`${label}: 2xx or 429`]: (r) => (r.status >= 200 && r.status < 300) || r.status === 429,
      [`${label}: envelope present`]: (r) => {
        if (r.status === 429) return true; // 429 envelope handled separately
        try {
          const body = r.json();
          return body && (body.status === "success" || body.status === "error");
        } catch (_) {
          return false;
        }
      },
    },
    { endpoint: label },
  );
}
