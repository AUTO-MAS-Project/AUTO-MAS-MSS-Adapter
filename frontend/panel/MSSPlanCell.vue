<script setup lang="ts">
import { input, selection } from './bridge'
import { difficultyLimit, type PlanKey } from './planSlots'

const props = defineProps<{
  field: keyof PlanKey
  slotValue: PlanKey
  stages: string[]
  label: string
  disabled: boolean
}>()
const emit = defineEmits<{ change: [patch: Partial<PlanKey>] }>()

function changeStage(stage: string) {
  emit('change', {
    TribulationStage: stage,
    Difficulty: Math.min(props.slotValue.Difficulty, difficultyLimit(stage)),
  })
}
</script>

<template>
  <select
    v-if="field === 'TribulationStage'"
    :value="slotValue.TribulationStage"
    :disabled="disabled"
    :aria-label="label"
    @change="changeStage(selection($event).value)"
  >
    <option v-for="stage in stages" :key="stage" :value="stage">
      {{ stage.slice(stage.indexOf('-') + 1) }}
    </option>
  </select>
  <select
    v-else-if="field === 'Difficulty'"
    :value="slotValue.Difficulty"
    :disabled="disabled || slotValue.SkipDifficulty"
    :aria-label="label"
    @change="emit('change', { Difficulty: Number(selection($event).value) })"
  >
    <option v-for="level in difficultyLimit(slotValue.TribulationStage)" :key="level" :value="level">
      {{ level }}
    </option>
  </select>
  <input
    v-else-if="field === 'FightTimes'"
    type="number"
    :value="slotValue.FightTimes"
    :disabled="disabled || slotValue.ConsumeAllEnergy"
    :aria-label="label"
    min="1"
    max="99"
    required
    @change="input($event).reportValidity() && emit('change', { FightTimes: input($event).valueAsNumber })"
  />
  <input
    v-else
    type="checkbox"
    :checked="slotValue[field]"
    :disabled="disabled"
    :aria-label="label"
    @change="emit('change', { [field]: input($event).checked })"
  />
</template>
