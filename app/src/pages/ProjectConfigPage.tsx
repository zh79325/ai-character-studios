/**
 * 项目配置页。表单本身在 `ProjectConfigForm` 里，这里只负责套上项目外壳。
 */
import ProjectFrame from '@/components/ProjectFrame'
import ProjectConfigForm from '@/components/ProjectConfigForm'

export default function ProjectConfigPage() {
  return (
    <ProjectFrame requireReady>
      <ProjectConfigForm />
    </ProjectFrame>
  )
}
