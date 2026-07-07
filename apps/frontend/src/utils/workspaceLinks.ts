const SCHEME_PATTERN = /^[a-z][a-z0-9+.-]*:/i;

export function resolveMarkdownWorkspaceHref(href: string, baseRelativePath = ""): string | null {
  const value = String(href || "").trim();
  if (!value || value.startsWith("#") || value.startsWith("//") || SCHEME_PATTERN.test(value)) {
    return null;
  }

  const pathPart = stripHrefMetadata(value);
  if (!pathPart || pathPart === "." || pathPart === "..") {
    return null;
  }

  const decodedPath = safeDecodeUriComponent(pathPart);
  if (/^[a-z]:[\\/]/i.test(decodedPath) || decodedPath.startsWith("\\\\")) {
    return null;
  }

  const normalizedInput = decodedPath.replace(/\\/g, "/");
  if (normalizedInput.startsWith("//")) {
    return null;
  }

  const joinedPath = normalizedInput.startsWith("/")
    ? normalizedInput.slice(1)
    : [directoryFromRelativePath(baseRelativePath), normalizedInput].filter(Boolean).join("/");

  return collapseWorkspacePath(joinedPath);
}

export function isExternalMarkdownHref(href: string): boolean {
  const value = String(href || "").trim();
  return Boolean(value && (value.startsWith("//") || SCHEME_PATTERN.test(value)));
}

export function findMarkdownLinkAnchor(target: EventTarget | null): HTMLAnchorElement | null {
  let element =
    target instanceof Element
      ? target
      : target instanceof Node
        ? target.parentElement
        : null;
  while (element) {
    if (element instanceof HTMLAnchorElement) {
      return element;
    }
    element = element.parentElement;
  }
  return null;
}

function stripHrefMetadata(value: string): string {
  const queryIndex = value.indexOf("?");
  const hashIndex = value.indexOf("#");
  const endCandidates = [queryIndex, hashIndex].filter((index) => index >= 0);
  const end = endCandidates.length ? Math.min(...endCandidates) : value.length;
  return value.slice(0, end).trim();
}

function safeDecodeUriComponent(value: string): string {
  try {
    return decodeURIComponent(value);
  } catch {
    return value;
  }
}

function directoryFromRelativePath(relativePath: string): string {
  const normalizedPath = collapseWorkspacePath(String(relativePath || "").replace(/\\/g, "/"));
  if (!normalizedPath) {
    return "";
  }
  const slashIndex = normalizedPath.lastIndexOf("/");
  return slashIndex >= 0 ? normalizedPath.slice(0, slashIndex) : "";
}

function collapseWorkspacePath(path: string): string | null {
  const segments: string[] = [];
  for (const rawSegment of String(path || "").split("/")) {
    const segment = rawSegment.trim();
    if (!segment || segment === ".") {
      continue;
    }
    if (segment === "..") {
      if (!segments.length) {
        return null;
      }
      segments.pop();
      continue;
    }
    segments.push(segment);
  }
  return segments.length ? segments.join("/") : null;
}
