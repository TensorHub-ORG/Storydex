export function splitSseFrames(buffer) {
  const normalized = String(buffer || "").replace(/\r\n/g, "\n");
  const parts = normalized.split("\n\n");
  return {
    frames: parts.slice(0, -1),
    rest: parts[parts.length - 1] || ""
  };
}

export function parseSseFrameData(frame) {
  const lines = String(frame || "").split("\n");
  let eventName = "message";
  const dataLines = [];
  for (const rawLine of lines) {
    const line = rawLine.trimEnd();
    if (!line || line.startsWith(":")) continue;
    if (line.startsWith("event:")) {
      eventName = line.slice("event:".length).trim();
      continue;
    }
    if (line.startsWith("data:")) {
      dataLines.push(line.slice("data:".length).trimStart());
    }
  }
  if (!dataLines.length) return null;
  try {
    const parsed = JSON.parse(dataLines.join("\n"));
    return {
      type: typeof parsed.type === "string" ? parsed.type : eventName,
      ...parsed
    };
  } catch {
    return null;
  }
}
