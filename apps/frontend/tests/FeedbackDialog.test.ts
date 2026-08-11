import { flushPromises, mount } from "@vue/test-utils";
import { beforeEach, describe, expect, it, vi } from "vitest";
import FeedbackDialog from "@/components/FeedbackDialog.vue";
import { submitFeedback } from "@/api/system";

vi.mock("@/api/system", () => ({ submitFeedback: vi.fn() }));

describe("FeedbackDialog", () => {
  beforeEach(() => {
    vi.stubGlobal("FileReader", class {
      result: string | ArrayBuffer | null = "data:image/png;base64,aW1hZ2UtZGF0YQ==";
      error: DOMException | null = null;
      onload: ((event: ProgressEvent<FileReader>) => void) | null = null;
      onerror: ((event: ProgressEvent<FileReader>) => void) | null = null;
      readAsDataURL(): void {
        this.onload?.({} as ProgressEvent<FileReader>);
      }
    });
    vi.mocked(submitFeedback).mockResolvedValue({
      data: { feedbackId: "feedback-1" },
      trace: null,
      audit: []
    });
  });

  it("submits error diagnostics and an attached image", async () => {
    const wrapper = mount(FeedbackDialog, {
      props: {
        open: true,
        source: "error",
        errorMessage: "provider request failed",
        errorType: "ProviderError",
        errorDetails: { status: 400 },
        diagnostics: { traceId: "trace-1", provider: "test" }
      }
    });
    await wrapper.find("textarea").setValue("Provider failed while generating a chapter.");
    const file = new File(["image-data"], "failure.png", { type: "image/png" });
    const input = wrapper.find<HTMLInputElement>('input[type="file"]');
    Object.defineProperty(input.element, "files", { configurable: true, value: [file] });
    await input.trigger("change");
    await flushPromises();
    await wrapper.find(".feedback-actions .primary").trigger("click");
    await flushPromises();

    expect(submitFeedback).toHaveBeenCalledTimes(1);
    expect(vi.mocked(submitFeedback).mock.calls[0][0]).toMatchObject({
      source: "error",
      errorMessage: "provider request failed",
      errorType: "ProviderError",
      errorDetails: { status: 400 },
      diagnostics: { traceId: "trace-1", provider: "test" }
    });
    expect(vi.mocked(submitFeedback).mock.calls[0][0].images).toHaveLength(1);
    expect(wrapper.text()).toContain("feedback-1");
  });

  it("prefills an error report without exposing conversation content", () => {
    const wrapper = mount(FeedbackDialog, {
      props: {
        open: true,
        source: "error",
        errorMessage: "provider request failed",
        errorType: "ProviderError"
      }
    });

    expect(wrapper.find("textarea").element.value).toBe("provider request failed");
    expect(wrapper.text()).toContain("不会上传对话正文、小说内容、API Key 或项目文件");
  });

  it("rejects images larger than five megabytes before upload", async () => {
    const wrapper = mount(FeedbackDialog, { props: { open: true, source: "settings" } });
    const file = new File([new Uint8Array(5 * 1024 * 1024 + 1)], "large.webp", { type: "image/webp" });
    const input = wrapper.find<HTMLInputElement>('input[type="file"]');
    Object.defineProperty(input.element, "files", { configurable: true, value: [file] });
    await input.trigger("change");
    expect(wrapper.text()).toContain("5 MB");
    expect(wrapper.findAll(".feedback-preview-list figure")).toHaveLength(0);
  });
});
