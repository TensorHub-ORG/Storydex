import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount } from "@vue/test-utils";
import UpdateNotification from "@/components/UpdateNotification.vue";

function updaterState(patch: Partial<StorydexDesktopUpdaterState> = {}): StorydexDesktopUpdaterState {
  return {
    supported: true,
    status: "idle",
    currentVersion: "1.0.0",
    availableVersion: "",
    releaseNotes: "",
    progress: null,
    error: "",
    feedUrl: "https://updates.example.test/",
    diagnosticLog: "",
    ...patch
  };
}

function installBridge(updater: StorydexDesktopUpdaterBridge): void {
  Object.defineProperty(window, "storydexDesktop", {
    configurable: true,
    value: {
      platform: "win32",
      versions: { tauri: "2.0.5" },
      updater
    } satisfies StorydexDesktopBridge
  });
}

describe("UpdateNotification", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    Object.defineProperty(window, "storydexDesktop", { configurable: true, value: undefined });
  });

  afterEach(() => {
    Object.defineProperty(window, "storydexDesktop", { configurable: true, value: undefined });
  });

  it("automatically checks, shows a new version, and installs it with one click", async () => {
    const detach = vi.fn();
    const check = vi.fn().mockResolvedValue(updaterState({ status: "available", availableVersion: "1.2.0" }));
    const download = vi.fn().mockResolvedValue(updaterState({ status: "downloaded", availableVersion: "1.2.0" }));
    const install = vi.fn().mockResolvedValue(true);
    installBridge({
      getState: vi.fn().mockResolvedValue(updaterState()),
      check,
      download,
      install,
      onState: vi.fn().mockReturnValue(detach)
    });

    const wrapper = mount(UpdateNotification);
    await flushPromises();

    expect(check).toHaveBeenCalledTimes(1);
    expect(wrapper.get("[data-testid='update-notification']").text()).toContain("发现新版本 v1.2.0");

    await wrapper.get(".update-notification-action").trigger("click");
    await flushPromises();

    expect(download).toHaveBeenCalledTimes(1);
    expect(install).toHaveBeenCalledTimes(1);
    wrapper.unmount();
    expect(detach).toHaveBeenCalledTimes(1);
  });

  it("renders download progress and installs only once when events arrive before download resolves", async () => {
    let emitState: ((state: StorydexDesktopUpdaterState) => void) | null = null;
    let resolveDownload!: (state: StorydexDesktopUpdaterState) => void;
    const download = vi.fn().mockImplementation(() => new Promise<StorydexDesktopUpdaterState>((resolve) => {
      resolveDownload = resolve;
    }));
    const install = vi.fn().mockResolvedValue(true);
    installBridge({
      getState: vi.fn().mockResolvedValue(updaterState({ status: "available", availableVersion: "1.2.0" })),
      check: vi.fn(),
      download,
      install,
      onState: vi.fn((listener) => {
        emitState = listener;
        return vi.fn();
      })
    });

    const wrapper = mount(UpdateNotification);
    await flushPromises();
    await wrapper.get(".update-notification-action").trigger("click");

    emitState?.(updaterState({
      status: "downloading",
      availableVersion: "1.2.0",
      progress: { percent: 25, transferred: 250, total: 1000, bytesPerSecond: 100 }
    }));
    await flushPromises();
    expect(wrapper.get("[role='progressbar']").text()).toContain("25%");

    emitState?.(updaterState({
      status: "downloading",
      availableVersion: "1.2.0",
      progress: { percent: 68, transferred: 680, total: 1000, bytesPerSecond: 120 }
    }));
    await flushPromises();
    expect(wrapper.get("[role='progressbar']").text()).toContain("68%");

    const downloaded = updaterState({ status: "downloaded", availableVersion: "1.2.0" });
    emitState?.(downloaded);
    await flushPromises();
    expect(install).toHaveBeenCalledTimes(1);

    resolveDownload(downloaded);
    await flushPromises();
    expect(download).toHaveBeenCalledTimes(1);
    expect(install).toHaveBeenCalledTimes(1);
    wrapper.unmount();
  });

  it.each([
    ["Error: net::ERR_CONNECTION_TIMED_OUT", "连接更新服务器超时，请稍后重试。"],
    ["getaddrinfo ENOTFOUND updates.example.test", "无法解析更新服务器地址，请检查网络或 DNS 设置。"],
    ["self signed certificate in certificate chain", "无法验证更新服务器的安全证书，请检查系统时间或网络环境。"],
    ["Cannot download installer.exe, status 403: Forbidden", "更新服务器拒绝了访问，请稍后重试。"],
    ["Cannot download installer.exe, status 404: Not Found", "更新安装包不存在或尚未发布完成，请稍后重试。"],
    ["Cannot download installer.exe, status 503: Service Unavailable", "更新服务器暂时不可用，请稍后重试。"],
    ["HTTP 429 Too Many Requests", "检查更新过于频繁，请稍后再试。"],
    ["ERR_UPDATER_CHANNEL_FILE_NOT_FOUND: Cannot find channel latest.yml update info: status 404", "暂未获取到更新信息，请稍后再试。"],
    ["ERR_UPDATER_INVALID_UPDATE_INFO: Cannot parse update info from latest.yml", "更新信息格式异常，请稍后重试。"],
    ["ERR_UPDATER_INVALID_VERSION: App version is not a valid semver version", "更新版本信息无效，请稍后重试。"],
    ["ERR_UPDATER_NO_FILES_PROVIDED: No files provided", "更新安装包尚未发布完整，请稍后重试。"],
    ["ERR_UPDATER_NO_CHECKSUM: Update info doesn't contain nor sha256 neither sha512 checksum", "更新信息缺少安全校验值，已停止下载。"],
    ["ERR_CHECKSUM_MISMATCH: sha512 checksum mismatch", "更新包校验失败，文件可能不完整，请重新下载。"],
    ["ERR_UPDATER_INVALID_SIGNATURE: New version is not signed by the application owner", "更新包签名验证失败，为保护你的设备已停止安装。"],
    ["ENOSPC: no space left on device", "磁盘空间不足，请清理空间后重试。"],
    ["EACCES: permission denied", "没有写入或安装权限，请确认安装目录可写后重试。"],
    ["EBUSY: installer.exe is being used by another process", "更新文件正被其他程序占用，请完全退出 Storydex 后重试。"],
    ["powershell unavailable", "系统更新助手无法启动，请确认 PowerShell 可用后重试。"],
    ["No update filepath provided, can't quit and install", "未找到已下载的安装包，请重新下载更新。"],
    ["installer exited with code 7", "安装程序启动失败，请重试或使用完整安装包更新。"],
    ["ERR_UPDATER_INVALID_PROVIDER_CONFIGURATION", "更新源配置异常，请联系 Storydex 支持。"],
    ["Proxy authentication failed with status 407", "代理服务器连接失败，请检查代理设置后重试。"],
    ["Request has been aborted by the server", "更新下载已中断，可点击重试继续。"],
    ["Too many redirects (> 10)", "更新服务器重定向异常，请稍后重试。"],
    ["Unexpected updater failure ABC-123", "更新未完成，请重试；若仍失败，请展开详情并联系 Storydex 支持。"]
  ])("shows an actionable message for updater error: %s", async (error, summary) => {
    installBridge({
      getState: vi.fn().mockResolvedValue(updaterState({
        status: "error",
        availableVersion: "1.2.0",
        error
      })),
      check: vi.fn(),
      download: vi.fn(),
      install: vi.fn(),
      onState: vi.fn().mockReturnValue(vi.fn())
    });

    const wrapper = mount(UpdateNotification);
    await flushPromises();

    expect(wrapper.get(".update-notification-detail").text()).toBe(summary);
    wrapper.unmount();
  });

  it("stays closed for the dismissed version and reappears for a newer version", async () => {
    let emitState: ((state: StorydexDesktopUpdaterState) => void) | null = null;
    installBridge({
      getState: vi.fn().mockResolvedValue(updaterState({ status: "available", availableVersion: "1.2.0" })),
      check: vi.fn(),
      download: vi.fn(),
      install: vi.fn(),
      onState: vi.fn((listener) => {
        emitState = listener;
        return vi.fn();
      })
    });

    const wrapper = mount(UpdateNotification);
    await flushPromises();
    expect(wrapper.find("[data-testid='update-notification']").exists()).toBe(true);

    await wrapper.get(".update-notification-close").trigger("click");
    expect(wrapper.find("[data-testid='update-notification']").exists()).toBe(false);

    emitState?.(updaterState({ status: "available", availableVersion: "1.2.0" }));
    await flushPromises();
    expect(wrapper.find("[data-testid='update-notification']").exists()).toBe(false);

    emitState?.(updaterState({ status: "available", availableVersion: "1.3.0" }));
    await flushPromises();
    expect(wrapper.get("[data-testid='update-notification']").text()).toContain("v1.3.0");
    wrapper.unmount();
  });

  it("renders nothing outside the packaged desktop bridge", async () => {
    const wrapper = mount(UpdateNotification);
    await flushPromises();
    expect(wrapper.find("[data-testid='update-notification']").exists()).toBe(false);
    wrapper.unmount();
  });
});
