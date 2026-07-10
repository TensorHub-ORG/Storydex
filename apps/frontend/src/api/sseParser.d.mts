export function splitSseFrames(buffer: string): { frames: string[]; rest: string };
export function parseSseFrameData(frame: string): Record<string, unknown> | null;
