const FRAGMENTED_LINE_MIN_COUNT = 8;

function charLength(value: string): number {
  return Array.from(value).length;
}

function isListLikeLine(value: string): boolean {
  return /^(\s*[-*+]\s+|\s*\d+[.)、]\s+|\s*\|)/u.test(value);
}

function isCjkOrPunctuationFragment(value: string): boolean {
  return /[\u3400-\u9fff]/u.test(value)
    || /^[，。！？、：；（）《》“”‘’*\s.[\]{}()<>-]+$/u.test(value);
}

function shouldInsertAsciiSpace(left: string, right: string): boolean {
  const leftChars = Array.from(left);
  const rightChars = Array.from(right);
  const last = leftChars[leftChars.length - 1] || "";
  const first = rightChars[0] || "";
  return /[A-Za-z0-9)]/.test(last) && /[A-Za-z0-9(]/.test(first);
}

function joinFragments(lines: string[]): string {
  let output = "";
  for (const line of lines) {
    if (!output) {
      output = line;
      continue;
    }
    output += shouldInsertAsciiSpace(output, line) ? ` ${line}` : line;
  }
  return output;
}

function looksFragmentedShortLines(lines: string[]): boolean {
  const nonEmpty = lines.map((line) => line.trim()).filter(Boolean);
  if (nonEmpty.length < FRAGMENTED_LINE_MIN_COUNT) {
    return false;
  }
  if (nonEmpty.some((line) => line.startsWith("```") || isListLikeLine(line))) {
    return false;
  }

  const shortLines = nonEmpty.filter((line) => charLength(line) <= 4).length;
  const averageLength = nonEmpty.reduce((sum, line) => sum + charLength(line), 0) / nonEmpty.length;
  const cjkLikeLines = nonEmpty.filter(isCjkOrPunctuationFragment).length;
  return (
    shortLines / nonEmpty.length >= 0.7
    && averageLength <= 4.5
    && cjkLikeLines / nonEmpty.length >= 0.5
  );
}

export function collapseFragmentedShortLines(value: string): string {
  const content = String(value || "").replace(/\r\n?/g, "\n");
  const lines = content.split("\n");
  const nonEmpty = lines.map((line) => line.trim()).filter(Boolean);
  if (nonEmpty.length === 0) {
    return content;
  }

  const quoteLineCount = nonEmpty.filter((line) => /^>\s*/.test(line)).length;
  if (quoteLineCount / nonEmpty.length >= 0.7) {
    const stripped = lines.map((line) => line.replace(/^>\s?/, "")).join("\n");
    const collapsed = collapseFragmentedShortLines(stripped);
    if (collapsed === stripped) {
      return content;
    }
    return collapsed
      .split(/\n{2,}/)
      .map((paragraph) => `> ${paragraph.replace(/\n/g, "\n> ")}`)
      .join("\n\n");
  }

  if (!looksFragmentedShortLines(lines)) {
    return content;
  }
  return joinFragments(nonEmpty);
}

export function formatAgentStreamText(value: string): string {
  const content = collapseFragmentedShortLines(String(value || ""));
  if (!content.trim()) {
    return content;
  }
  return content
    .split(/\n{2,}/)
    .map((paragraph) => {
      const lines = paragraph.split("\n");
      if (!looksFragmentedShortLines(lines)) {
        return paragraph;
      }
      return joinFragments(lines.map((line) => line.trim()).filter(Boolean));
    })
    .join("\n\n");
}
