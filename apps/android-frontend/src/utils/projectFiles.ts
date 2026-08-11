import { authedFetch } from '@/bridge/http'

export function currentProjectRoot(): string {
  return (window.CoomiAndroid?.getStoryProjectPath?.() ?? '').replace(/\/+$/, '')
}

export function projectFile(relative: string): string {
  const root = currentProjectRoot()
  if (!root) throw new Error('尚未选择故事项目')
  const clean = relative.replace(/\\/g, '/').replace(/^\/+/, '')
  if (!clean || clean.split('/').includes('..')) throw new Error('无效的项目文件路径')
  return `${root}/${clean}`
}

export async function readProjectText(relative: string): Promise<string | null> {
  const response = await authedFetch(`/api/fs/raw?path=${encodeURIComponent(projectFile(relative))}`)
  if (response.status === 404) return null
  if (!response.ok) throw new Error(`读取项目文件失败（HTTP ${response.status}）`)
  return response.text()
}

export async function readProjectJson<T>(relative: string): Promise<T | null> {
  const raw = await readProjectText(relative)
  if (raw == null) return null
  try { return JSON.parse(raw) as T } catch { throw new Error(`${relative} 不是有效 JSON`) }
}

export async function writeProjectText(relative: string, content: string): Promise<string> {
  const path = projectFile(relative)
  const response = await authedFetch('/api/fs/write', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ path, content }),
  })
  if (!response.ok) {
    let detail = `HTTP ${response.status}`
    try { detail = (await response.json())?.error ?? detail } catch { /* ignore */ }
    throw new Error(`写入项目文件失败：${detail}`)
  }
  return path
}

export async function writeProjectJson(relative: string, value: unknown): Promise<string> {
  return writeProjectText(relative, `${JSON.stringify(value, null, 2)}\n`)
}

export async function deleteProjectFile(relative: string): Promise<void> {
  const response = await authedFetch('/api/fs/delete', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ path: projectFile(relative) }),
  })
  if (!response.ok && response.status !== 404) throw new Error(`删除项目文件失败（HTTP ${response.status}）`)
}

export async function exportProjectContent(relative: string, content: string, suggestedName: string): Promise<void> {
  const staging = `.storydex/exports/${safeFilename(suggestedName)}`
  const path = await writeProjectText(staging, content)
  if (window.CoomiAndroid?.exportFile) {
    window.CoomiAndroid.exportFile(path, suggestedName)
    return
  }
  const url = URL.createObjectURL(new Blob([content], { type: 'application/octet-stream' }))
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = suggestedName
  anchor.click()
  URL.revokeObjectURL(url)
}

export function safeFilename(value: string): string {
  const clean = value.trim().replace(/[\\/:*?"<>|\u0000-\u001f]/g, '-').replace(/\.+$/g, '')
  return clean || 'storydex-item'
}

export function createId(prefix: string): string {
  const random = globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random().toString(16).slice(2)}`
  return `${prefix}-${random}`
}
