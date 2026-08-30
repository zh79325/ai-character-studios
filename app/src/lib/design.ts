/**
 * 项目里能干的事，一处列全。
 *
 * 立项页的推荐操作与顶栏的「当前项目」菜单指的是同一批入口，两边各写一份必然会有一天对不上。
 *
 * `ready` 为假的类别只有入口没有流程：后端目前只跑得通 character 一条，但菜单里先摆着，
 * 用户才知道这个平台打算往哪走。
 */
export interface DesignEntry {
  /** 也是路由：`/design/{slug}`。 */
  slug: string
  label: string
  hint: string
  ready: boolean
}

export const DESIGN_ENTRIES: DesignEntry[] = [
  {
    slug: 'characters',
    label: '人物设计',
    hint: '聊定设定 → 渲染图 → 四视图 → 模型',
    ready: true,
  },
  { slug: 'equipment', label: '元素设计', hint: '武器、道具、装备件', ready: false },
  { slug: 'maps', label: '地图设计', hint: '关卡地图与俯视图', ready: false },
  { slug: 'scenes', label: '场景设计', hint: '场景概念图与环境资产', ready: false },
]

export function designPath(slug: string): string {
  return `/design/${slug}`
}

export function designEntry(slug: string): DesignEntry | undefined {
  return DESIGN_ENTRIES.find((one) => one.slug === slug)
}

/** 项目本身的几张表：立项对焦、配置、视觉规范。 */
export const PROJECT_ENTRIES = [
  { key: '/project', label: '立项对焦', hint: '跟设计师聊项目要什么' },
  { key: '/project/config', label: '项目配置', hint: '风格基调与生产参数' },
  { key: '/project/art-bible', label: '视觉规范', hint: '视觉真相，禁止项会进 negative' },
]
