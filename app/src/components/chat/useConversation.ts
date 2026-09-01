/**
 * 一场会话的全部动作：接上会话、发话、订流、中断。
 *
 * 谁都能用：立项对焦、角色设定、以后的地图与场景，换的只是 `targetKind`/`targetRef`/
 * `agentCode` 三个入参。要自己排布界面就直接用这个 hook 拼，不必套 `ChatPanel`。
 *
 * 三条约定改动前先看一遍：
 *
 * 1. 顺序是先 `sendMessage` 再 `subscribeConversation`，后者带 `fresh`。后端开工第一步会清掉
 *    上一轮的增量缓冲，而两个请求谁先到服务端说不准：`fresh` 就是告诉后端缓冲里现存的一
 *    概不算，不然上一轮的回答会被重放进「正在想」的气泡。接别处发起的那一轮时不带：那时
 *    缓冲里就剩这一轮，从头读才能把错过的开头补上。
 * 2. 「在跑」的凭据是库里那条 `status=thinking` 的消息，不是这个组件的 state。切走页面再
 *    回来、进程重启、别处发起的那一轮，都还认得出来在跑。
 * 3. 自己中断的那一轮，发消息那头随后会报 409。那不是意外，用 `cut` 挡掉这一次的报错。
 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { App } from 'antd'
import { useEffect, useRef, useState } from 'react'

import {
  ensureConversation,
  interruptConversation,
  readConversation,
  sendMessage,
  subscribeConversation,
} from '@/api/conversations'
import type { ConversationDetail, TargetKind } from '@/types/api'

/**
 * 从外面递进来的一句话。
 *
 * 只预填不自动发：这句话是平台替用户拟的，直接发出去等于拿用户的名义说了一句他没看过的话。
 */
export interface Handoff {
  text: string
  /** 同一句话可能递两次（用户又按了一遍），用它区分。 */
  nonce: number
}

interface Options {
  projectCode: string
  agentCode: string
  targetKind: TargetKind
  targetRef?: string | null
  /** 新会话的标题，留空让后端按 agent 取一个。 */
  title?: string
  handoff?: Handoff | null
  /** 报出当下看的是哪场会话：评审要拿它做「驳回后自动重生」。 */
  onActiveChange?: (id: string | null) => void
}

export function useConversation({
  projectCode,
  agentCode,
  targetKind,
  targetRef = null,
  title,
  handoff = null,
  onActiveChange,
}: Options) {
  const { message: toast } = App.useApp()
  const queryClient = useQueryClient()
  const [input, setInput] = useState('')
  const [streaming, setStreaming] = useState<string | null>(null)
  /** 已经发出去、还没回到会话详情里的那句话。 */
  const [pending, setPending] = useState<string | null>(null)
  const stop = useRef<(() => void) | null>(null)
  /** 这一轮是自己中断的：发消息那头随后会报 409，那不是意外，不弹错。 */
  const cut = useRef(false)

  // 一物一会话：这一口是幂等的，同一个对象进来永远是同一场，用户不用先做一次「建会话」的动作
  const ensured = useQuery({
    queryKey: ['project', projectCode, 'conversation-ensure', targetKind, targetRef, agentCode],
    queryFn: () =>
      ensureConversation(projectCode, {
        agent_code: agentCode,
        target_kind: targetKind,
        target_ref: targetRef,
        title,
      }),
    staleTime: Infinity,
    refetchOnWindowFocus: false,
  })

  useEffect(() => {
    const fresh = ensured.data
    if (fresh === undefined) return
    queryClient.setQueryData(['project', projectCode, 'conversation', fresh.conversation.id], fresh)
    void queryClient.invalidateQueries({
      queryKey: ['project', projectCode, 'conversations'],
    })
  }, [ensured.data, projectCode, queryClient])

  const id = ensured.data?.conversation.id ?? null

  const detail = useQuery({
    queryKey: ['project', projectCode, 'conversation', id],
    queryFn: () => readConversation(projectCode, id!),
    enabled: id !== null,
  })

  // 离开面板要退订，否则这条连接会一直挂在后端的事件循环上
  useEffect(() => () => stop.current?.(), [])

  useEffect(() => {
    onActiveChange?.(id)
  }, [id, onActiveChange])

  const send = useMutation({
    mutationFn: async (content: string) => {
      if (id === null) throw new Error('还没有会话')
      setStreaming('')
      stop.current?.()
      // 先发出去（不等），后端清掉上一轮缓冲之后这条流才订得上本轮的字
      const turn = sendMessage(projectCode, id, content)
      stop.current = subscribeConversation({
        projectCode,
        conversationId: id,
        fresh: true,
        onDelta: (piece) => setStreaming((prev) => (prev ?? '') + piece),
        // 这一轮出了结果就把攒下的字交给消息列表：POST 马上就回来，气泡先退回转圈
        onTurn: () => setStreaming(''),
        // 失败的措辞由发消息那头统一报，这里再弹一次就是同一件事说两遍
        onError: () => setStreaming(null),
      })
      try {
        return await turn
      } finally {
        stop.current?.()
        stop.current = null
        setStreaming(null)
      }
    },
    onSuccess: async (turn) => {
      // 等详情真拉回来再抖掉那句话，不然气泡会先消失一下再出现
      await queryClient.invalidateQueries({
        queryKey: ['project', projectCode, 'conversation', id],
      })
      setPending(null)
      void queryClient.invalidateQueries({ queryKey: ['project', projectCode, 'conversations'] })
      if (turn.folded_turns.length > 0) {
        toast.info(`上下文快满了，第 ${turn.folded_turns.join('、')} 轮已折进摘要，原文还在`)
      }
    },
    // 后端是先落库再调模型，所以要先看这句话到底存下没：存下了就别再送回输入框，否则重发会冒出两条同样的话
    onError: async (err: Error, content) => {
      await queryClient.invalidateQueries({
        queryKey: ['project', projectCode, 'conversation', id],
      })
      const fresh = queryClient.getQueryData<ConversationDetail>([
        'project',
        projectCode,
        'conversation',
        id,
      ])
      const landed = fresh?.messages.some((one) => one.role === 'user' && one.content === content)
      setPending(null)
      if (landed !== true) setInput((prev) => (prev === '' ? content : prev))
      // 自己掐的那一轮也会从这里回来：用户刚按的那下就是这个结果，再弹一句就是告诉他点错了
      if (cut.current) {
        cut.current = false
        return
      }
      toast.error(err.message)
    },
  })

  const interrupt = useMutation({
    mutationFn: () => interruptConversation(projectCode, id!),
    onSuccess: async () => {
      cut.current = true
      stop.current?.()
      stop.current = null
      setStreaming(null)
      setPending(null)
      await queryClient.invalidateQueries({
        queryKey: ['project', projectCode, 'conversation', id],
      })
    },
    onError: (err: Error) => toast.error(err.message),
  })

  const messages = (detail.data?.messages ?? []).filter((one) => one.role !== 'system')
  /** 库里那条「正在想」。它是这一轮在跑的凭据，切走页面再回来、甚至换个窗口都还在。 */
  const thinkingId = messages.find((one) => one.status === 'thinking')?.id ?? null
  // 这一轮不一定是这个面板发起的：库里说在跑就当在跑，不然会让用户往一个正在生成的会话里插话
  const busy = send.isPending || thinkingId !== null

  useEffect(() => {
    // 别处发起的那一轮也把字接上：只有一个干转的圈，看着跟卡死没区别
    if (thinkingId === null || id === null || send.isPending) return
    setStreaming('')
    const settle = () => {
      setStreaming(null)
      void queryClient.invalidateQueries({
        queryKey: ['project', projectCode, 'conversation', id],
      })
    }
    return subscribeConversation({
      projectCode,
      conversationId: id,
      onDelta: (piece) => setStreaming((prev) => (prev ?? '') + piece),
      onTurn: settle,
      onError: settle,
    })
  }, [thinkingId, id, send.isPending, projectCode, queryClient])

  // 只认 nonce：同一句话递两次是两件事，而重渲染不是
  const handled = useRef(0)
  useEffect(() => {
    if (handoff === null || handoff.nonce === handled.current) return
    handled.current = handoff.nonce
    setInput(handoff.text)
  }, [handoff])

  /** 发一句话出去。选择组件拼出来的那句也走这里，跟手打的一样进气泡。 */
  const say = (content: string) => {
    if (busy) return
    cut.current = false
    // 先清输入框、先把话摆上去：点完发送还看见自己那段字蹲在输入框里，像没发出去
    setInput('')
    setPending(content)
    send.mutate(content)
  }

  const submit = () => {
    const content = input.trim()
    if (content) say(content)
  }

  return {
    id,
    detail: detail.data ?? null,
    detailLoading: detail.isLoading,
    messages,
    pending,
    streaming,
    busy,
    /** 会话还没接上来：这一刻页面上什么都做不了，摆个转圈就行。 */
    preparing: id === null && ensured.error === null,
    /** 会话接不上来时的错因。有它就别再摆转圈，否则用户会一直等一件不会来的事。 */
    failure: ensured.error?.message ?? '',
    input,
    setInput,
    say,
    submit,
    interrupt: () => interrupt.mutate(),
    interrupting: interrupt.isPending,
  }
}
