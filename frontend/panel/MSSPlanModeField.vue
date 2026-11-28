<script setup lang="ts">
import { selection } from './bridge'

defineProps<{
  mode: string
  plans: { label: string; value: string }[]
  queuedTaskCount: number
  t: (key: string) => string
}>()
const emit = defineEmits<{ change: [mode: string] }>()
</script>

<template>
  <label class="plan-mode-field">
    {{ t('mss.planMode') }}
    <select :value="mode" @change="emit('change', selection($event).value)">
      <option value="Fixed">{{ t('mss.planFixed') }}</option>
      <option v-for="plan in plans" :key="plan.value" :value="plan.value">{{ plan.label }}</option>
    </select>
  </label>
  <p class="hint">{{ t('mss.planHint') }}</p>
  <p v-if="!queuedTaskCount && mode === 'Fixed'" role="alert" class="field-error">
    {{ t('mss.queueEmpty') }}
  </p>
</template>

<style scoped>
.plan-mode-field {
  max-width: 360px;
}
</style>
