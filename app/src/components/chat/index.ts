/**
 * 会话交互这一套的出口。
 *
 * 接一个新领域（地图、场景……）只要两步：后端加该领域的 agent 与 `target_kind`，页面里摆一个
 * `<ChatPanel agentCode="…" targetKind="…" targetRef={id} who="…" starters={[…]} />`。会话按
 * target 一物一条，不用另写创建逻辑。要另一套排布就用 `useConversation` + `MessageList` +
 * `Composer` 自己拼。
 */
export { default, default as ChatPanel } from './ChatPanel'
export { default as ChoicePicker, Row, ROW_LABEL } from './ChoicePicker'
export { default as DraftDiffPanel } from './DraftDiffPanel'
export { default as MessageList } from './MessageList'
export { default as Composer } from './Composer'
export { useConversation } from './useConversation'
export type { Handoff } from './useConversation'
