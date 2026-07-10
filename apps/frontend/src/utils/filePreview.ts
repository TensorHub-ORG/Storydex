import router from "@/router";

const PREVIEW_WINDOW_NAME = "storydex-preview-window";

export async function openFilePreviewWindow(relativePath: string): Promise<boolean> {
  const normalizedPath = normalizeRelativePath(relativePath);
  if (!normalizedPath) {
    return false;
  }

  if (window.storydexDesktop?.openPreviewWindow) {
    return window.storydexDesktop.openPreviewWindow(normalizedPath);
  }

  const target = router.resolve({
    name: "preview-file",
    query: { relativePath: normalizedPath }
  }).href;

  const opened = window.open(target, PREVIEW_WINDOW_NAME);
  return Boolean(opened);
}

function normalizeRelativePath(value: string): string {
  return String(value || "").trim().replace(/\\/g, "/").replace(/^\/+|\/+$/g, "").trim();
}
