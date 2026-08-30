/**
 * 侧栏顶部的项目切换器。
 *
 * 项目是这个工具里几乎所有内容的作用域（素材、会话、生成记录都在项目自带的库里），所以
 * 它常驻在导航上，任何页面都能看到「现在在哪个项目里」。
 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { App, Select, Space, Typography } from 'antd'

import { listProjects, switchProject } from '@/api/projects'

export default function ProjectSwitcher() {
  const { message } = App.useApp()
  const queryClient = useQueryClient()
  const list = useQuery({ queryKey: ['projects'], queryFn: () => listProjects() })

  const doSwitch = useMutation({
    mutationFn: (code: string) => switchProject(code),
    onSuccess: (fresh) => {
      queryClient.setQueryData(['projects'], fresh)
      // 换项目等于换一个库，缓存里所有项目相关的东西一律作废。逐个点名 key 迟早会漏，
      // 而这是用户主动做的动作，全刷一遍的代价可以忽略。
      void queryClient.invalidateQueries()
    },
    onError: (err: Error) => message.error(err.message),
  })

  const projects = list.data?.projects ?? []

  return (
    <Space direction="vertical" size={4} style={{ width: '100%' }}>
      <Typography.Text type="secondary" style={{ fontSize: 12 }}>
        当前项目
      </Typography.Text>
      <Select
        style={{ width: '100%' }}
        value={list.data?.current ?? undefined}
        placeholder={projects.length ? '选一个项目' : '还没有项目'}
        loading={list.isLoading || doSwitch.isPending}
        disabled={projects.length === 0}
        onChange={(code: string) => doSwitch.mutate(code)}
        options={projects.map((item) => ({
          value: item.code,
          // 目录不在（外置盘没挂、被搬走）就别让用户切过去，切过去每个页面都会报错
          disabled: item.missing,
          label: item.missing ? `${item.name}（目录不在）` : item.name,
        }))}
      />
    </Space>
  )
}
