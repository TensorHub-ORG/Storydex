import MarkdownIt from "markdown-it";
import type { Options } from "markdown-it";

export function createMarkdownRenderer(options: Options = {}): MarkdownIt {
  const markdown = new MarkdownIt({
    html: false,
    breaks: true,
    linkify: true,
    typographer: false,
    ...options
  });

  markdown.linkify.set({
    fuzzyLink: false,
    fuzzyEmail: false
  });

  return markdown;
}
