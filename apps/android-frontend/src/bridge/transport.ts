import type { AgentCommand, InboundEnvelope } from '@/protocol/commands'

export type ConnectionState = 'connecting' | 'open' | 'closed' | 'error'

export interface Transport {
  connect(): void
  close(): void
  send(command: AgentCommand): void
  onMessage(handler: (env: InboundEnvelope) => void): void
  onStateChange(handler: (state: ConnectionState) => void): void
}
