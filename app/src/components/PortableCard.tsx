/**
 * 整包导入导出。
 *
 * 导出默认不带 key——那份是能发给同事的模板；要带 key 得自己勾上，这是用户主动的选择。
 * 导入分 merge / replace：replace 会把库里没在包里出现的账号一并删掉，所以要二次确认。
 */
import { DownloadOutlined, UploadOutlined } from '@ant-design/icons'
import { useMutation } from '@tanstack/react-query'
import { Alert, App, Button, Card, Checkbox, Popconfirm, Radio, Space, Typography } from 'antd'
import { useState } from 'react'

import { exportConfig, importConfig } from '@/api/providers'
import type { ImportResult } from '@/types/api'

export default function PortableCard({ onImported }: { onImported: () => void }) {
  const { message } = App.useApp()
  const [includeKeys, setIncludeKeys] = useState(false)
  const [mode, setMode] = useState<'merge' | 'replace'>('merge')
  const [text, setText] = useState('')
  const [result, setResult] = useState<ImportResult | null>(null)

  const doExport = useMutation({
    mutationFn: () => exportConfig(includeKeys),
    onSuccess: (payload) => {
      setText(JSON.stringify(payload, null, 2))
      message.success(includeKeys ? '已导出（含明文 key，注意别外传）' : '已导出模板（不含 key）')
    },
    onError: (err: Error) => message.error(err.message),
  })

  const doImport = useMutation({
    mutationFn: () => {
      const parsed = JSON.parse(text) as { providers?: Record<string, unknown> }
      // 允许直接粘 provider_agents.json 全文（带 providers 外层），也允许只粘里面那层
      const providers = parsed.providers ?? (parsed as Record<string, unknown>)
      return importConfig(providers, mode)
    },
    onSuccess: (res) => {
      setResult(res)
      message.success(`新增 ${res.created.length}、更新 ${res.updated.length}`)
      onImported()
    },
    onError: (err: Error) => message.error(err.message),
  })

  return (
    <Card size="small" title="整包导入导出">
      <Space direction="vertical" size={12} style={{ width: '100%' }}>
        <Space wrap>
          <Button
            icon={<DownloadOutlined />}
            loading={doExport.isPending}
            onClick={() => doExport.mutate()}
          >
            导出到下面的文本框
          </Button>
          <Checkbox checked={includeKeys} onChange={(e) => setIncludeKeys(e.target.checked)}>
            带上明文 api_key
          </Checkbox>
          <Radio.Group
            value={mode}
            onChange={(e) => setMode(e.target.value as 'merge' | 'replace')}
            options={[
              { value: 'merge', label: 'merge（只增改）' },
              { value: 'replace', label: 'replace（先清空）' },
            ]}
          />
          <Popconfirm
            title={mode === 'replace' ? '确定先清空全部账号？' : '开始导入？'}
            description={
              mode === 'replace'
                ? '库里现有的账号连额度、用量、绑定一起删，然后灌入这份包。'
                : '包里没提到的账号会留着不动；包里不带 key 的话本机已有的 key 不会被抹掉。'
            }
            okButtonProps={{ danger: mode === 'replace' }}
            onConfirm={() => doImport.mutate()}
          >
            <Button
              type="primary"
              icon={<UploadOutlined />}
              loading={doImport.isPending}
              disabled={!text.trim()}
            >
              导入
            </Button>
          </Popconfirm>
        </Space>

        <textarea
          value={text}
          onChange={(e) => setText(e.target.value)}
          placeholder="把 provider_agents.json 的内容粘进来，或者点上面「导出」看现有配置长什么样"
          spellCheck={false}
          style={{
            width: '100%',
            height: 220,
            fontFamily: 'Menlo, monospace',
            fontSize: 12,
            padding: 8,
          }}
        />

        {result && (
          <Alert
            type={result.warnings.length > 0 ? 'warning' : 'success'}
            showIcon
            message={`新增 ${result.created.length}、更新 ${result.updated.length}、删除 ${result.removed.length}；模型 ${result.models}、绑定 ${result.bindings}、额度 ${result.limits}`}
            description={
              result.warnings.length > 0 && (
                <Space direction="vertical" size={0}>
                  {result.warnings.map((warning) => (
                    <Typography.Text key={warning} style={{ fontSize: 12 }}>
                      {warning}
                    </Typography.Text>
                  ))}
                </Space>
              )
            }
          />
        )}
      </Space>
    </Card>
  )
}
