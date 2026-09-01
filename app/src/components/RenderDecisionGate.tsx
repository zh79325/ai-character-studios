/**
 * 会话内的效果图收口：采用这一张，或再画一张。
 *
 * 定稿始终人工确认——画师自动出图只到「给你看」，采用与否由用户拍板。想改具体细节不在这儿填：
 * 关掉这层抽屉，在下面输入框直接说，效果图评审阶段的输入会被后端当成「这张图要改哪里」驱动
 * 重画（见 `agents/orchestrator.py::in_render_review`）。
 *
 * 「再画一张」空手重生一版：新候选到了，`CharacterPage` 会拿新的 generation id 换出这层抽屉，
 * 于是又摆到最新那张图上让用户拍。
 */
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { App, Button, Space, Typography } from 'antd'

import { confirmRender, renderCharacter } from '@/api/characters'

export default function RenderDecisionGate({
  projectCode,
  characterId,
  generationId,
}: {
  projectCode: string
  characterId: string
  generationId: string
}) {
  const { message } = App.useApp()
  const queryClient = useQueryClient()

  const adopt = useMutation({
    mutationFn: () => confirmRender(projectCode, characterId, generationId, ''),
    onSuccess: async () => {
      await Promise.all([
        queryClient.invalidateQueries({
          queryKey: ['project', projectCode, 'character', characterId],
        }),
        queryClient.invalidateQueries({
          queryKey: ['project', projectCode, 'character-renders', characterId],
        }),
      ])
      message.success('效果图已定稿')
    },
    onError: (error: Error) => message.error(error.message),
  })

  const redraw = useMutation({
    mutationFn: () => renderCharacter(projectCode, characterId, '', ''),
    onSuccess: async () => {
      await Promise.all([
        // 前缀键：一并刷这个角色那场会话，让新的画师消息与带图气泡落定
        queryClient.invalidateQueries({ queryKey: ['project', projectCode, 'conversation'] }),
        queryClient.invalidateQueries({
          queryKey: ['project', projectCode, 'character-renders', characterId],
        }),
        queryClient.invalidateQueries({
          queryKey: ['project', projectCode, 'character', characterId],
        }),
      ])
    },
    onError: (error: Error) => message.error(error.message),
  })

  const busy = adopt.isPending || redraw.isPending

  return (
    <Space direction="vertical" size={8} style={{ width: '100%' }}>
      <Typography.Text strong style={{ fontSize: 13 }}>
        这张效果图
      </Typography.Text>
      <Typography.Text type="secondary" style={{ fontSize: 12 }}>
        采用它就定稿；想再看一版点「再画一张」；想改具体细节关掉这层，在下面直接说。
      </Typography.Text>
      <Space size={8} wrap>
        <Button
          type="primary"
          loading={adopt.isPending}
          disabled={busy}
          onClick={() => adopt.mutate()}
        >
          采用这张
        </Button>
        <Button loading={redraw.isPending} disabled={busy} onClick={() => redraw.mutate()}>
          再画一张
        </Button>
      </Space>
    </Space>
  )
}
