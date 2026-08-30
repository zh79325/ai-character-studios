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
  /**
   * 让用户挑一个目录，取消返回 null。
   *
   * 项目可以放在磁盘任意位置，新建与导入都要一个绝对路径；让用户手敲路径太容易敲错，
   * 而系统文件对话框只有主进程开得出来，所以走这道门。
   */
  chooseDirectory(defaultPath?: string): Promise<string | null>
}
