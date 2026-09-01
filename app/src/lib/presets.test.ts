/**
 * 套餐预设摊成表单行的行为。
 *
 * 钉的是「只填一个额度数字」这条路上最容易悄悄配错的几处：能力对不上的 Agent 不许绑、
 * 周期得用供应商自己的重置窗口、额度留空要落成不限量而不是上限 0、取消勾选的模型不许建、
 * 预设带的调用参数得原样进库。
 */
import { describe, expect, it } from 'vitest'

import type { AgentDef, ProviderPreset } from '@/types/api'

import { type ModelRow, matchAgents, pickedModels, rowsFromPreset, toModelIn } from './presets'

function agent(agent_code: string, capability: string): AgentDef {
  return {
    agent_code,
    capability,
    role: agent_code,
    role_type: 'specialist',
    focusable: false,
    aliases: [agent_code],
    target_kinds: ['project'],
    stages: [],
    max_turns: 1,
    conversational: false,
    memory_scope: 'none',
    context_budget: 0,
    output_contract: 'text',
    allow_tools: [],
    source_file: `${agent_code}.md`,
  }
}

const preset: ProviderPreset = {
  code: 'ark-coding',
  vendor: '火山方舟',
  plan: 'Coding Plan',
  label: '火山方舟 · Coding Plan',
  base_url: 'https://ark.cn-beijing.volces.com',
  driver: 'openai_compat',
  auth_style: 'bearer',
  key_prefix: null,
  models: [
    {
      model_id: 'glm-5.3',
      capabilities: ['text'],
      driver: 'openai_compat',
      api_path: '/api/coding/v3',
      limit_kind: 'tokens',
      default_period: 'day+11H',
      params: { context_window: 1_000_000 },
      remark: '每天 11 点重置',
    },
    {
      model_id: 'doubao-seedream-5.0',
      capabilities: ['t2i', 'i2i'],
      driver: 'ark_image',
      api_path: '/api/v3/images/generations',
      limit_kind: 'calls',
      default_period: 'day+11H',
      params: {},
      remark: null,
    },
  ],
}

describe('按能力绑 Agent', () => {
  it('只绑能力对得上的那几个', () => {
    const agents = [agent('设计师', 'text'), agent('原画', 't2i'), agent('建模', 'model3d')]
    expect(matchAgents(agents, ['text'])).toEqual(['设计师'])
    expect(matchAgents(agents, ['t2i', 'i2i'])).toEqual(['原画'])
  })

  it('一个都对不上时就是空的，不能顺手绑一个上去', () => {
    expect(matchAgents([agent('建模', 'model3d')], ['text'])).toEqual([])
  })
})

describe('摊成表单行', () => {
  it('默认全勾上，额度留空，周期用供应商自己的重置窗口', () => {
    const rows = rowsFromPreset(preset, [])
    expect(rows.map((row) => row.model_id)).toEqual(['glm-5.3', 'doubao-seedream-5.0'])
    expect(rows.every((row) => row.picked)).toBe(true)
    expect(rows.map((row) => row.max_value)).toEqual([null, null])
    expect(rows.map((row) => row.period_expr)).toEqual(['day+11H', 'day+11H'])
  })

  it('每行各带自己的 driver、端点与计量口径', () => {
    const rows = rowsFromPreset(preset, [])
    expect(rows[1]!.driver).toBe('ark_image')
    expect(rows[1]!.api_path).toBe('/api/v3/images/generations')
    expect(rows[1]!.limit_kind).toBe('calls')
  })

  it('按各行能力分别预绑 Agent，不是一股绑给所有模型', () => {
    const rows = rowsFromPreset(preset, [agent('设计师', 'text'), agent('原画', 't2i')])
    expect(rows[0]!.agents).toEqual(['设计师'])
    expect(rows[1]!.agents).toEqual(['原画'])
  })
})

describe('一行变成一个模型', () => {
  const row = (extra: Partial<ModelRow> = {}): ModelRow => ({
    ...rowsFromPreset(preset, [])[0]!,
    ...extra,
  })

  it('额度留空就不发 limits，不能发一条上限 0 的把模型堵死', () => {
    expect(toModelIn(row({ max_value: null })).limits).toEqual([])
    expect(toModelIn(row({ max_value: 0 })).limits).toEqual([])
  })

  it('填了数字才落一条额度，口径与周期跟着这一行', () => {
    expect(toModelIn(row({ max_value: 1_800_000 })).limits).toEqual([
      {
        limit_kind: 'tokens',
        max_value: 1_800_000,
        period_expr: 'day+11H',
        group_name: 'default',
      },
    ])
  })

  it('建出来就是启用的，否则填完一堆额度还是不干活', () => {
    expect(toModelIn(row()).enabled).toBe(true)
  })

  it('预设带的上下文窗口要原样进库，不然预算只能回落到保守值', () => {
    expect(toModelIn(row()).params).toEqual({ context_window: 1_000_000 })
  })
})

describe('只建勾上的', () => {
  it('取消勾选的模型不许建进去', () => {
    const rows = rowsFromPreset(preset, [])
    rows[0]!.picked = false
    expect(pickedModels(rows).map((one) => one.model_id)).toEqual(['doubao-seedream-5.0'])
  })

  it('一个都没勾就是个光账号，之后再加模型', () => {
    const rows = rowsFromPreset(preset, []).map((one) => ({ ...one, picked: false }))
    expect(pickedModels(rows)).toEqual([])
  })
})
