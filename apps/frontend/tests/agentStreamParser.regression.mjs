import assert from "node:assert/strict";
import test from "node:test";
import { parseSseFrameData, splitSseFrames } from "../src/api/sseParser.mjs";

test("SSE parser emits complete frames before the request finishes", () => {
  const chunks = [
    'event: RunAccepted\ndata: {"_type":"RunAcc',
    'epted","elapsedMs":0}\n\nevent: TurnPhase\ndata: {"_type":"TurnPhase",',
    '"elapsedMs":600}\n\n'
  ];
  let buffer = "";
  const packets = [];

  buffer += chunks[0];
  let parsed = splitSseFrames(buffer);
  assert.equal(parsed.frames.length, 0);
  buffer = parsed.rest;

  buffer += chunks[1];
  parsed = splitSseFrames(buffer);
  packets.push(...parsed.frames.map(parseSseFrameData));
  buffer = parsed.rest;
  assert.equal(packets.length, 1);
  assert.equal(packets[0]._type, "RunAccepted");
  assert.notEqual(buffer, "");

  buffer += chunks[2];
  parsed = splitSseFrames(buffer);
  packets.push(...parsed.frames.map(parseSseFrameData));
  assert.equal(packets.length, 2);
  assert.equal(packets[1]._type, "TurnPhase");
  assert.equal(parsed.rest, "");
});
