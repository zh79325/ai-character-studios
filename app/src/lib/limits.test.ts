/**
 * 限额展示口的行为。
 *
 * 钉两件事：口径的取值域归后端，前端的翻译表只是锦上添花，**没翻译的口径也必须显示出来**
 * （后端加了新口径而这里忘了补，界面上不能变成空白）；上限 0 是「不限量」而不是「一条上限
 * 为 0 的限额」，摘要里不能把它当成配过。
 */
import { describe, expect, it } from 'vitest'

import type { LimitOut, Model } from '@/types/api'

import { kindLabel, limitText } from './limits'

function limit(limit_kind: string, max_value: number): LimitOut {
  return {
    id: 1,
    limit_kind,
    max_value,
    period_expr: 'day',
    group_name: 'default',
    window_text: '今日',
  }
}

function model(limits: LimitOut[]): Model {
  return {
    id: 1,
    provider_code: 'ark',
    model_id: 'seedream-5.0',
    capabilities: ['image'],
    driver: null,
    effective_driver: 'openai_compat',
    api_path: null,
    endpoint: 'https://example.invalid/v3',
    enabled: true,
    sort_no: 0,
    params: {},
    remark: null,
    agents: [],
    limits,
  }
}

describe('口径的人话', () => {
  it('已知口径带上一句解释', () => {
    expect(kindLabel('images')).toBe('images（出图张数）')
    expect(kindLabel('calls')).toBe('calls（接口次数）')
  })

  it('翻译表里没有的口径原样显示', () => {
    expect(kindLabel('seconds')).toBe('seconds')
  })
})

describe('限额摘要', () => {
  it('一条都没配就是不限量', () => {
    expect(limitText(model([]))).toBe('不限量')
  })

  it('上限 0 等于没配', () => {
    expect(limitText(model([limit('calls', 0)]))).toBe('不限量')
  })

  it('多条并排列出来', () => {
    expect(limitText(model([limit('calls', 20), limit('images', 60)]))).toBe(
      'calls 20/day，images 60/day',
    )
  })
})
