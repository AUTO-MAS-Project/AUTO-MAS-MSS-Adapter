export type PlanMode = 'ALL' | 'Weekly' | 'Rotate'

export type PlanContext = { mode: PlanMode; rotateDays: number; stages: string[] }

export const weekdays = [
  'Monday',
  'Tuesday',
  'Wednesday',
  'Thursday',
  'Friday',
  'Saturday',
  'Sunday',
]

export function planGroups({ mode, rotateDays }: PlanContext) {
  return mode === 'Rotate'
    ? Array.from({ length: rotateDays }, (_, index) => `Day${index + 1}`)
    : ['ALL', ...weekdays]
}

export type PlanKey = {
  TribulationStage: string
  SkipDifficulty: boolean
  Difficulty: number
  ConsumeAllEnergy: boolean
  FightTimes: number
}

export const fields = [
  'TribulationStage',
  'SkipDifficulty',
  'Difficulty',
  'ConsumeAllEnergy',
  'FightTimes',
] as const

export function difficultyLimit(stage: string) {
  const limits: Record<string, number> = { 基础试炼: 1, 晋升试炼: 9, 技巧试炼: 6, 纹章试炼: 3 }
  return limits[stage.split('-')[0]] ?? 9
}
