const https = require("https");

const DAILY_ACTIVE_BASE_URL = "https://updates.septemc.com/storydex/feedback/api/stats/dau";

function reportDailyActive(platform, version, requestFunction = https.request) {
  const normalizedPlatform = String(platform || "").trim().toLowerCase();
  if (!new Set(["windows", "android"]).has(normalizedPlatform)) {
    throw new TypeError("Storydex daily active platform is invalid");
  }
  const normalizedVersion = String(version || "unknown").trim() || "unknown";
  const request = requestFunction(
    `${DAILY_ACTIVE_BASE_URL}/${normalizedPlatform}`,
    {
      method: "POST",
      headers: {
        "Content-Length": "0",
        "User-Agent": `Storydex-${normalizedPlatform}/${normalizedVersion}`,
        "X-Storydex-Version": normalizedVersion
      },
      timeout: 4000
    },
    (response) => response.resume()
  );
  request.on("timeout", () => request.destroy());
  request.on("error", () => {});
  request.end();
  return request;
}

module.exports = { DAILY_ACTIVE_BASE_URL, reportDailyActive };
