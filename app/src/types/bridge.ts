/** preload 暴露给渲染层的那点能力。主进程与渲染层共用这一份定义，避免两边各写一遍走形。 */
export interface AtelierBridge {
  /** 后端端口；后端没起来时是 null。 */
  port(): Promise<number | null>
  /** 后端起不来的原因，起来了就是 null。 */
  startupError(): Promise<string | null>
  /** 开窗之前的那段后端输出。 */
  logBacklog(): Promise<string[]>
  /** 订阅后端输出，返回退订函数。 */
  onBackendLog(handler: (line: string) => void): () => void
}
