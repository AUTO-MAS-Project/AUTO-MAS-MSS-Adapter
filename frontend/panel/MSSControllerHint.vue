<script setup lang="ts">
defineProps<{ controllerType?: string; t: (key: string) => string }>()
</script>

<template>
  <p v-if="controllerType && controllerType !== 'Win32'" role="alert" class="field-error">
    {{ t('mss.controllerHint') }}
  </p>
</template>
