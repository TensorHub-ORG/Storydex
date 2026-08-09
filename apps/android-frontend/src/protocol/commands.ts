import type { AgentEvent } from './events'

export type PermissionMode = 'ask' | 'auto' | 'full'
export type ReasoningEffort = 'auto' | 'low' | 'medium' | 'high' | 'xhigh' | 'max'
export type ApprovalDecision = 'allow' | 'deny' | 'always'

export interface SendMessageCommand { command: 'send_message'; text: string }
export interface CancelCommand { command: 'cancel' }
export interface JumpInCommand { command: 'jump_in'; text: string }
export interface ApproveToolCommand { command: 'approve_tool'; call_id: string; decision: ApprovalDecision }
export interface AnswerQuestionCommand { command: 'answer_question'; call_id: string; answer: string }
export interface SetPermissionModeCommand { command: 'set_permission_mode'; mode: PermissionMode }
export interface EnterPlanModeCommand { command: 'enter_plan_mode' }
export interface ExitPlanModeCommand { command: 'exit_plan_mode' }
export interface SelectModelCommand { command: 'select_model'; provider_id: string; model: string }
export interface SetReasoningEffortCommand { command: 'set_reasoning_effort'; effort: ReasoningEffort }
export interface FileTransferResultCommand { command: 'file_transfer_result'; request_id: string; paths: string[] }
export interface SendGuideCommand { command: 'send_guide'; key: string }

export type AgentCommand =
  | SendMessageCommand | CancelCommand | JumpInCommand | ApproveToolCommand
  | AnswerQuestionCommand | SetPermissionModeCommand | EnterPlanModeCommand
  | ExitPlanModeCommand | SelectModelCommand | SetReasoningEffortCommand | FileTransferResultCommand
  | SendGuideCommand

export const PROTOCOL_VERSION = 1
export type EnvelopeType = 'event' | 'command' | 'ack' | 'error'

export interface Envelope<P = unknown> {
  v: number; type: EnvelopeType; id?: string; ts: number; payload: P
}

export type EventEnvelope = Envelope<AgentEvent> & { type: 'event' }
export type CommandEnvelope = Envelope<AgentCommand> & { type: 'command' }
export type AckEnvelope = Envelope<{ ok: boolean }> & { type: 'ack' }
export type ErrorEnvelope = Envelope<{ message: string; code?: string }> & { type: 'error' }
export type InboundEnvelope = EventEnvelope | AckEnvelope | ErrorEnvelope
