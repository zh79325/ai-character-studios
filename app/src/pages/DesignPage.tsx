/**
 * 各类素材的设计页。
 *
 * 目前只有 character 一条流程跑得通，其余类别的页面先摆在这里说明它是什么、要等什么——
 * 菜单上有入口点进来是一片空白，比没有入口更让人怀疑软件坏了。
 */
import { Card, Empty, Typography } from 'antd'
import { Navigate, useParams } from 'react-router-dom'

import CharacterTable from '@/components/CharacterTable'
import ProjectFrame from '@/components/ProjectFrame'
import { DESIGN_ENTRIES, designEntry } from '@/lib/design'

export default function DesignPage() {
  const { category = '' } = useParams()
  const entry = designEntry(category)

  if (!entry) return <Navigate to={`/design/${DESIGN_ENTRIES[0]!.slug}`} replace />

  return (
    <ProjectFrame requireReady>
      {entry.ready ? <CharacterTable /> : <ComingSoon label={entry.label} hint={entry.hint} />}
    </ProjectFrame>
  )
}

function ComingSoon({ label, hint }: { label: string; hint: string }) {
  return (
    <Card size="small" title={label}>
      <Empty
        description={
          <Typography.Text type="secondary">
            {label}（{hint}）还没接上流程。先把视觉规范写完，各类素材都从它取风格与禁止项。
          </Typography.Text>
        }
      />
    </Card>
  )
}
