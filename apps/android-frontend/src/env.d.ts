/// <reference types="vite/client" />

interface Window {
  CoomiAndroid?: {
    openDashboard(): void
    getFilesDirPath?(): string
    /** 内置环境中存放所有故事项目的根目录（filesDir/stories）。 */
    getStoriesRootPath?(): string
    getStoryProjectPath?(): string
    /** 切换故事项目目录（仅接受内置环境 filesDir 内的路径，越界返回 false）。 */
    setStoryProjectPath?(path: string): boolean
    getThemeMode?(): string
    setThemeMode?(mode: string): void
    importFiles?(): void
    importFilesForRequest?(requestId: string): void
    authorizeFolder?(): void
    exportFile?(path: string, suggestedName: string): void
    exportFileForRequest?(requestId: string, path: string, suggestedName: string): void
    openFile?(path: string): void
    /** 保存图片（data URL）到相册或下载目录。 */
    saveImageData?(dataUrl: string, fileName: string): void
    /** 通知原生层任务运行状态（更新通知栏：执行中 / 已完成）。 */
    updateTaskStatus?(status: string): void
    /** 获取设备与 App 诊断信息（报错反馈使用，不含对话内容）。 */
    getDiagnostics?(): string
    /** 原生上报报错反馈（绕过 WebView CORS）：json 为反馈体，callbackId 用于异步回调。 */
    sendFeedback?(json: string, callbackId: string): void
  }
}

declare module '*.vue' {
  import type { DefineComponent } from 'vue'
  const component: DefineComponent<{}, {}, any>
  export default component
}
