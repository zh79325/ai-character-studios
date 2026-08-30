/**
 * 视觉规范页。编辑器本身在 `ArtBibleEditor` 里，这里只负责套上项目外壳。
 *
 * 立项中也放行：art bible 就是对焦阶段沉淀出来的那份，收口前就得看得见、改得动。
 */
import ArtBibleEditor from '@/components/ArtBibleEditor'
import ProjectFrame from '@/components/ProjectFrame'

export default function ArtBiblePage() {
  return (
    <ProjectFrame>
      <ArtBibleEditor />
    </ProjectFrame>
  )
}
