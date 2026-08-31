/**
 * 插件管理页：列出可安装的插件，点「安装」后台下载，进度条实时更新。
 *
 * 语音识别模型是第一个插件（约 3GB）。有插件在装时才开轮询，装完/空闲就停，别空转。
 */
import { CheckCircleOutlined, DownloadOutlined } from '@ant-design/icons'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { App, Button, Card, Progress, Space, Tag, Typography } from 'antd'
import { useEffect } from 'react'

import { installPlugin, listPlugins, type Plugin } from '@/api/plugins'

export default function PluginsPage() {
  const { message } = App.useApp()
  const queryClient = useQueryClient()

  const plugins = useQuery({
    queryKey: ['plugins'],
    queryFn: listPlugins,
    // 有插件正在装就每 1.5s 刷一次进度，否则不轮询
    refetchInterval: (query) =>
      (query.state.data ?? []).some((one) => one.running) ? 1500 : false,
  })

  const install = useMutation({
    mutationFn: (id: string) => installPlugin(id),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ['plugins'] }),
    onError: (err: Error) => message.error(err.message),
  })

  // 装失败时把后端记的原因弹出来，比只在卡片里显示更醒目
  const data = plugins.data
  useEffect(() => {
    for (const one of data ?? []) {
      if (one.message && !one.running && !one.installed) message.error(one.message)
    }
  }, [data, message])

  return (
    <Space direction="vertical" size={16} style={{ width: '100%' }}>
      <Typography.Title level={4} style={{ margin: 0 }}>
        插件管理
      </Typography.Title>
      <Typography.Text type="secondary">
        插件是「用得上但不随代码走」的大件，按需在后台下载安装到本地。
      </Typography.Text>

      {(plugins.data ?? []).map((plugin) => (
        <PluginCard
          key={plugin.id}
          plugin={plugin}
          installing={install.isPending && install.variables === plugin.id}
          onInstall={() => install.mutate(plugin.id)}
        />
      ))}
    </Space>
  )
}

function PluginCard({
  plugin,
  installing,
  onInstall,
}: {
  plugin: Plugin
  installing: boolean
  onInstall: () => void
}) {
  const busy = plugin.running || installing
  return (
    <Card size="small">
      <Space direction="vertical" size={8} style={{ width: '100%' }}>
        <Space style={{ justifyContent: 'space-between', width: '100%' }}>
          <Space direction="vertical" size={0}>
            <Typography.Text strong>{plugin.name}</Typography.Text>
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              {plugin.description}
            </Typography.Text>
          </Space>
          {plugin.installed ? (
            <Tag icon={<CheckCircleOutlined />} color="success">
              已安装
            </Tag>
          ) : (
            <Button
              type="primary"
              icon={<DownloadOutlined />}
              loading={busy}
              disabled={busy}
              onClick={onInstall}
            >
              {busy ? '安装中' : '安装'}
            </Button>
          )}
        </Space>
        {plugin.running && <Progress percent={plugin.progress} status="active" />}
      </Space>
    </Card>
  )
}
