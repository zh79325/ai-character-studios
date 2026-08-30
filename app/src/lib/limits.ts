/**
 * 限额口径的展示口。
 *
 * 取值域始终由后端 `/api/config/options` 的 `limit_kinds` 给，前端不许自己列一份——这里
 * 只做两件事：把口径码翻成人话，把模型上已配的限额压成一行摘要。翻译表里没有的口径原样
 * 显示，后端加了新口径也不会在界面上凭空消失。
 */
import type { Model } from '@/types/api'

const KIND_HINTS: Record<string, string> = {
  tokens: '文本 token',
  calls: '接口次数',
  images: '出图张数',
  credits: 'Meshy 积分',
}

/** 口径码加一句人话，例如 `images（出图张数）`。 */
export function kindLabel(kind: string): string {
  const hint = KIND_HINTS[kind]
  return hint ? `${kind}（${hint}）` : kind
}

/** 限额摘要：`calls 200/day`，一条都没配就是不限量。上限 0 等于没配，不算一条。 */
export function limitText(model: Model): string {
  const live = model.limits.filter((limit) => limit.max_value > 0)
  if (live.length === 0) return '不限量'
  return live
    .map((limit) => `${limit.limit_kind} ${limit.max_value}/${limit.period_expr}`)
    .join('，')
}
