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
        {plugin.running && (
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            {plugin.total_bytes > 0
              ? `${formatBytes(plugin.downloaded_bytes)} / ${formatBytes(plugin.total_bytes)}`
              : formatBytes(plugin.downloaded_bytes)}
            {plugin.speed_bytes != null && ` · ${formatBytes(plugin.speed_bytes)}/s`}
            {' · '}
            {plugin.eta_seconds == null ? '剩余估算中…' : `剩余 ${formatEta(plugin.eta_seconds)}`}
          </Typography.Text>
        )}
      </Space>
    </Card>
  )
}

// 把剩余秒数揉成「X 分 Y 秒 / X 秒 / X 小时 Y 分」这种一眼能看的样子。
function formatEta(seconds: number): string {
  const s = Math.max(0, Math.round(seconds))
  if (s < 60) return `${s} 秒`
  const m = Math.floor(s / 60)
  if (m < 60) return `${m} 分 ${s % 60} 秒`
  const h = Math.floor(m / 60)
  return `${h} 小时 ${m % 60} 分`
}

// 字节数揉成 B/KB/MB/GB（十进制，跟下载工具一致）。
function formatBytes(bytes: number): string {
  const n = Math.max(0, bytes)
  if (n < 1000) return `${n} B`
  const units = ['KB', 'MB', 'GB', 'TB']
  let value = n / 1000
  let i = 0
  while (value >= 1000 && i < units.length - 1) {
    value /= 1000
    i += 1
  }
  return `${value.toFixed(value < 10 ? 2 : 1)} ${units[i]}`
}
