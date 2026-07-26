import MarkdownIt from "markdown-it";
import type { Options } from "markdown-it";

export type MarkdownRendererFeatures = {
  linkifyWorkspaceMarkdownFiles?: boolean;
};

export function createMarkdownRenderer(
  options: Options = {},
  features: MarkdownRendererFeatures = {}
): MarkdownIt {
  const markdown = new MarkdownIt({
    html: false,
    breaks: true,
    linkify: true,
    typographer: false,
    ...options
  });

  markdown.linkify.set({
    fuzzyLink: Boolean(features.linkifyWorkspaceMarkdownFiles),
    fuzzyEmail: false
  });

  if (features.linkifyWorkspaceMarkdownFiles) {
    const renderLinkOpen = markdown.renderer.rules.link_open;
    markdown.renderer.rules.link_open = (tokens, index, rendererOptions, env, self) => {
      const token = tokens[index];
      const labelToken = tokens[index + 1];
      const label = labelToken?.type === "text" ? labelToken.content.trim() : "";
      const href = token.attrGet("href") || "";

      // linkify-it treats the .md country-code TLD as a website. Only rewrite
      // its fuzzy, scheme-less form; explicit https:// links remain external.
      if (
        token.markup === "linkify" &&
        /^[^\\/?#\s]+\.md$/i.test(label) &&
        href.toLowerCase() === `http://${label}`.toLowerCase()
      ) {
        token.attrSet("href", label);
      }

      return renderLinkOpen
        ? renderLinkOpen(tokens, index, rendererOptions, env, self)
        : self.renderToken(tokens, index, rendererOptions);
    };
  }

  return markdown;
}
