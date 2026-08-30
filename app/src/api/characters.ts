/**
 * 角色接口。
 *
 * 页面上要分清两件事，接口也是分开的：`review` 只给裁决与理由，`spec/confirm` 才是放行。
 * 哪怕裁决是 `APPROVE` 也得人按一下——所以前端不许拿 `approved` 自动去调 confirm。
 *
 * 评审慢是应该的（一次调用加上最多三轮自动重生），这里不设超时：用户按了「评审」就在等这一
 * 轮的结果。
 */
import { baseUrl, request } from './client'
import type {
  AssetSpec,
  Character,
  Generation,
  RenderResult,
  SpecReview,
  TaskEvent,
  ViewReview,
  ViewSet,
} from '@/types/api'

export function createCharacter(name: string): Promise<Character> {
  return request<Character>('/api/characters', { method: 'POST', body: { name } })
}

export function readCharacter(id: string): Promise<Character> {
  return request<Character>(`/api/characters/${encodeURIComponent(id)}`)
}

export function listCharacterEvents(id: string): Promise<TaskEvent[]> {
  return request<TaskEvent[]>(`/api/characters/${encodeURIComponent(id)}/events`)
}

/**
 * 让 `spec_reviewer` 审最新那一版设定。
 *
 * 带上会话 id 才会在 `REJECT` 后自动把理由发回写手重生——重生得有个会话承载新的一轮，不然
 * 那几轮对话跟用户自己那场对不上号。
 */
export function reviewSpec(id: string, conversationId?: string | null): Promise<SpecReview> {
  return request<SpecReview>(`/api/characters/${encodeURIComponent(id)}/review`, {
    method: 'POST',
    body: { conversation_id: conversationId ?? null },
  })
}

/** 门禁 1：人工确认设定。确认的是磁盘上那一份，没沉淀过后端会 409。 */
export function confirmSpec(id: string, note: string): Promise<Character> {
  return request<Character>(`/api/characters/${encodeURIComponent(id)}/spec/confirm`, {
    method: 'POST',
    body: { note },
  })
}

/** 门禁 1 驳回：状态停在原地，理由记进时间线，下一轮设定会话能看见上次为什么没过。 */
export function rejectSpec(id: string, note: string): Promise<Character> {
  return request<Character>(`/api/characters/${encodeURIComponent(id)}/spec/reject`, {
    method: 'POST',
    body: { note },
  })
}

/** 推进状态。只能往前且一步一步走，没过门禁会拿到 409。 */
export function advanceCharacter(id: string, state: string): Promise<Character> {
  return request<Character>(`/api/characters/${encodeURIComponent(id)}/advance`, {
    method: 'POST',
    body: { state },
  })
}

/**
 * 只让 `prompt_smith` 出一张卡片，不生图。
 *
 * 卡片里的 prompt 就是这张图的全部依据，层序缺一截用户在图上只能看出「不对」而看不出「哪里
 * 不对」，所以先看一眼卡片比直接烧一次额度划得来。
 */
export function draftAssetSpec(id: string, note = '', field = ''): Promise<AssetSpec> {
  return request<AssetSpec>(`/api/characters/${encodeURIComponent(id)}/asset-spec`, {
    method: 'POST',
    body: { note, field },
  })
}

/**
 * 出一张渲染图：后端先拿卡片再生图，产物落 `tmp/`。
 *
 * `field` 给了就是「改某一项重生」，只把那一项发回给写手；只给 `note` 是换方向。生图几十秒是
 * 应该的，这里不设超时。
 */
export function renderCharacter(id: string, note = '', field = ''): Promise<RenderResult> {
  return request<RenderResult>(`/api/characters/${encodeURIComponent(id)}/render`, {
    method: 'POST',
    body: { note, field },
  })
}

/** 渲染图的全部候选，新的在前。门禁上要在几张之间挑，就得能把过往那几张一并列出来。 */
export function listRenders(id: string): Promise<Generation[]> {
  return request<Generation[]>(`/api/characters/${encodeURIComponent(id)}/renders`)
}

/**
 * 一张候选图的地址，直接给 `<img src>`。
 *
 * 不把图转 base64 塞进 JSON：一张 2048 的 png 动辄几 MB，进 JSON 再膨 33%，而浏览器对
 * `<img src>` 本来就会缓存。
 */
export async function renderImageUrl(id: string, generationId: string): Promise<string> {
  const base = await baseUrl()
  const path = `/api/characters/${encodeURIComponent(id)}/renders/${encodeURIComponent(generationId)}/image`
  return `${base}${path}`
}

/**
 * 门禁 2：采用指名的那一张。
 *
 * 必需指名 `generationId`：默认采用「最新一张」在用户连生了几张之后就不是他指的那一张了。
 */
export function confirmRender(id: string, generationId: string, note: string): Promise<Character> {
  return request<Character>(`/api/characters/${encodeURIComponent(id)}/render/confirm`, {
    method: 'POST',
    body: { generation_id: generationId, note },
  })
}

/** 门禁 2 驳回：状态停在「渲染图已生成」，理由进时间线给下一轮重生用。 */
export function rejectRender(id: string, note: string): Promise<Character> {
  return request<Character>(`/api/characters/${encodeURIComponent(id)}/render/reject`, {
    method: 'POST',
    body: { note },
  })
}

/**
 * 出一批四视图：后端拿姿势模版与定稿渲染图当参考图，四张并发。
 *
 * `variants` 空着就是四个角度都生；只给几个就是重生那几张——评审驳回往往只有背面不合格，四
 * 张全重来会把用户已经认可的三张也换掉。四张 4K 图几十秒是应该的，这里不设超时。
 */
export function generateViews(
  id: string,
  variants: string[] = [],
  seed: number | null = null,
): Promise<ViewSet> {
  return request<ViewSet>(`/api/characters/${encodeURIComponent(id)}/views`, {
    method: 'POST',
    body: { variants, seed },
  })
}

/** 四视图的全部候选，新的在前。`variant` 告诉前端这一张是哪个面。 */
export function listViews(id: string): Promise<Generation[]> {
  return request<Generation[]>(`/api/characters/${encodeURIComponent(id)}/views`)
}

/**
 * 让 `vision_reviewer` 看图裁决。粒度跟项目的 `review_mode` 走，`mode` 能盖这一次。
 *
 * `regenerate` 默认关：REJECT 后自动重生要花额度，该不该花得用户说了算。裁决只能拦不能放行，
 * 所以前端不许拿 `approved` 自动去调定稿。
 */
export function reviewViews(
  id: string,
  mode: string | null = null,
  regenerate = false,
): Promise<ViewReview> {
  return request<ViewReview>(`/api/characters/${encodeURIComponent(id)}/views/review`, {
    method: 'POST',
    body: { mode, regenerate },
  })
}

/**
 * 人选输入：把指名的四张拷进定稿位并推到 S5。
 *
 * 四个视角要逐个指名：建模吃的是一整组图，默认取「每个面最新那张」就不是他在界面上挑的那一组。
 */
export function confirmViews(
  id: string,
  picks: Record<string, string>,
  note: string,
): Promise<Character> {
  return request<Character>(`/api/characters/${encodeURIComponent(id)}/views/confirm`, {
    method: 'POST',
    body: { picks, note },
  })
}
