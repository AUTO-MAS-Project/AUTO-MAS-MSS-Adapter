<script setup lang="ts">
import { computed, onMounted, ref, shallowRef, watch } from 'vue'
import { selection, usePanel } from './bridge'
import MSSPlanCell from './MSSPlanCell.vue'
import { difficultyLimit, fields, planGroups, type PlanContext, type PlanKey } from './planSlots'

const panel = usePanel()
const { config, disabled, error, report, t, working } = panel
const context = shallowRef<PlanContext>()
const contextPending = ref(false)
let contextRevision = 0
const view = ref('config')
const groups = computed(() => (context.value ? planGroups(context.value) : []))
const rows = computed(() => (view.value === 'config' ? fields : groups.value))
const columns = computed(() => (view.value === 'config' ? groups.value : fields))

function groupLabel(group: string) {
  return group.startsWith('Day') ? `${t('plan.day')} ${group.slice(3)}` : t('plan.' + group)
}

function slotValue(group: string): PlanKey {
  const slot = config.value[group]?.Key as Partial<PlanKey> | undefined
  const stage = slot?.TribulationStage ?? context.value!.stages[0]
  return {
    TribulationStage: stage,
    SkipDifficulty: slot?.SkipDifficulty ?? false,
    Difficulty: Math.min(slot?.Difficulty ?? 1, difficultyLimit(stage)),
    ConsumeAllEnergy: slot?.ConsumeAllEnergy ?? false,
    FightTimes: slot?.FightTimes ?? 1,
  }
}

function canEdit(group: string) {
  return context.value?.mode === 'ALL' ? group === 'ALL' : group !== 'ALL'
}

async function reload() {
  const revision = ++contextRevision
  contextPending.value = true
  try {
    const next = await panel.request<PlanContext>('GET')
    if (revision === contextRevision) context.value = next
  } finally {
    if (revision === contextRevision) contextPending.value = false
  }
}

async function initialize() {
  if (!panel.target.value) await panel.initialize()
  await reload()
}

onMounted(() => void panel.run(initialize))
watch(config, (_next, previous) => {
  if (Object.keys(previous).length) void reload().catch(report)
})

function edit(group: string, patch: Partial<PlanKey>) {
  return panel.run(() => panel.saveField(group, 'Key', { ...slotValue(group), ...patch }))
}

function fieldFor(row: string, column: string) {
  return (view.value === 'config' ? row : column) as keyof PlanKey
}

function groupFor(row: string, column: string) {
  return view.value === 'config' ? column : row
}
</script>

<template>
  <p v-if="working" role="status">{{ t('working') }}</p>
  <div v-if="error" class="actions">
    <p role="alert" class="field-error">{{ error }}</p>
    <button type="button" :disabled="working" @click="panel.run(initialize)">
      {{ t('refreshPreview') }}
    </button>
  </div>
  <fieldset v-if="context" :disabled="disabled || contextPending" class="stack">
    <label class="plan-view">
      {{ t('plan.view') }}
      <select :value="view" @change="view = selection($event).value">
        <option value="config">{{ t('plan.config') }}</option>
        <option value="simple">{{ t('plan.simple') }}</option>
      </select>
    </label>
    <div class="plan-scroll" role="region" :aria-label="t('plan.config')" tabindex="0">
      <table class="plan-table">
        <thead>
          <tr>
            <th scope="col">{{ t(view === 'config' ? 'plan.field' : 'plan.time') }}</th>
            <th v-for="column in columns" :key="column" scope="col">
              {{ view === 'config' ? groupLabel(column) : t('plan.' + column) }}
            </th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in rows" :key="row">
            <th scope="row">{{ view === 'config' ? t('plan.' + row) : groupLabel(row) }}</th>
            <td v-for="column in columns" :key="column">
              <MSSPlanCell
                :field="fieldFor(row, column)"
                :slot-value="slotValue(groupFor(row, column))"
                :stages="context.stages"
                :label="`${groupLabel(groupFor(row, column))} · ${t('plan.' + fieldFor(row, column))}`"
                :disabled="!canEdit(groupFor(row, column))"
                @change="edit(groupFor(row, column), $event)"
              />
            </td>
          </tr>
        </tbody>
      </table>
    </div>
  </fieldset>
</template>

<style scoped>
.plan-view {
  max-width: 220px;
}

.plan-scroll {
  overflow-x: auto;
}

.plan-table {
  width: 100%;
  border-collapse: collapse;
}

.plan-table :is(th, td) {
  min-width: 110px;
  padding: 6px 8px;
  border: 1px solid var(--mas-border, GrayText);
  text-align: center;
}

.plan-table th:first-child {
  position: sticky;
  left: 0;
  background: var(--mas-bg-base, Canvas);
}
</style>
