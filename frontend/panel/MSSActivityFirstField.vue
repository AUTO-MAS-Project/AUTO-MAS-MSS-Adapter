<script setup lang="ts">
import { input } from './bridge'

defineProps<{ enabled: boolean; t: (key: string) => string }>()
const emit = defineEmits<{ change: [enabled: boolean] }>()
</script>

<template>
  <label class="switch-tile">
    <input type="checkbox" :checked="enabled" @change="emit('change', input($event).checked)" />
    {{ t('mss.activityFirst') }}
  </label>
  <p class="hint">{{ t('mss.activityFirstHint') }}</p>
</template>
