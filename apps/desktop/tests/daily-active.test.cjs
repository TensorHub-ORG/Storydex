const assert = require("node:assert/strict");
const { EventEmitter } = require("node:events");
const test = require("node:test");

const { DAILY_ACTIVE_BASE_URL, reportDailyActive } = require("../electron/daily-active.cjs");

test("daily active sends an empty non-blocking platform request", () => {
  let captured;
  const requestFunction = (url, options, callback) => {
    captured = { url, options, callback, ended: false };
    const request = new EventEmitter();
    request.end = () => { captured.ended = true; };
    request.destroy = () => {};
    return request;
  };

  reportDailyActive("windows", "2.0.5", requestFunction);

  assert.equal(captured.url, `${DAILY_ACTIVE_BASE_URL}/windows`);
  assert.equal(captured.options.method, "POST");
  assert.equal(captured.options.timeout, 4000);
  assert.equal(captured.options.headers["Content-Length"], "0");
  assert.equal(captured.options.headers["X-Storydex-Version"], "2.0.5");
  assert.equal(captured.ended, true);
});

test("daily active rejects unknown platforms", () => {
  assert.throws(() => reportDailyActive("web", "1.0.0"), /platform is invalid/);
});
