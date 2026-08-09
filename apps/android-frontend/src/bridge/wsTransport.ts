import type { AgentCommand, InboundEnvelope } from '@/protocol/commands'
import { wrapCommand } from './envelope'
import type { ConnectionState, Transport } from './transport'

export interface WsTransportOptions { url: string; maxRetries?: number; backoffBase?: number }

export class WsTransport implements Transport {
  private ws: WebSocket | null = null
  private msgHandlers: ((env: InboundEnvelope) => void)[] = []
  private stateHandlers: ((state: ConnectionState) => void)[] = []
  private retries = 0
  private closedByUser = false
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null
  constructor(private readonly opts: WsTransportOptions) {}

  connect(): void { this.closedByUser = false; this.open() }

  private open(): void {
    this.emitState('connecting')
    try { this.ws = new WebSocket(this.opts.url) } catch { this.scheduleReconnect(); return }
    this.ws.onopen = () => { this.retries = 0; this.emitState('open') }
    this.ws.onmessage = (ev) => {
      try { const env = JSON.parse(ev.data as string) as InboundEnvelope; this.msgHandlers.forEach(h => h(env)) } catch {}
    }
    this.ws.onerror = () => this.emitState('error')
    this.ws.onclose = () => { if (this.closedByUser) { this.emitState('closed'); return }; this.scheduleReconnect() }
  }

  private scheduleReconnect(): void {
    const max = this.opts.maxRetries ?? 10
    if (this.retries >= max) { this.emitState('error'); return }
    this.emitState('closed')
    const base = this.opts.backoffBase ?? 500
    const delay = Math.min(base * 2 ** this.retries, 10_000)
    this.retries += 1
    this.reconnectTimer = setTimeout(() => this.open(), delay)
  }

  close(): void { this.closedByUser = true; if (this.reconnectTimer) clearTimeout(this.reconnectTimer); this.ws?.close() }
  send(command: AgentCommand): void { if (this.ws?.readyState !== WebSocket.OPEN) return; this.ws.send(JSON.stringify(wrapCommand(command))) }
  onMessage(handler: (env: InboundEnvelope) => void): void { this.msgHandlers.push(handler) }
  onStateChange(handler: (state: ConnectionState) => void): void { this.stateHandlers.push(handler) }
  private emitState(state: ConnectionState): void { this.stateHandlers.forEach(h => h(state)) }
}
