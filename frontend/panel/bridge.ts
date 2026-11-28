import { computed, ref, shallowRef } from 'vue'
import locale from '../../src/mss_adapter/i18n/zh-CN.json'

type Value = string | number | boolean | null
type ConfigValue = Value | ConfigValue[] | { [key: string]: ConfigValue }
type Config = Record<string, Record<string, ConfigValue>>
type Snapshot = { config: Config }
type Target =
  | { kind: 'script' | 'setup' | 'user'; scriptId: string; userId: string | null }
  | { kind: 'plan'; planId: string }
type State = { locale: string; i18nRevision: number; locked: boolean; busy: boolean }
type Method = 'GET' | 'POST' | 'PUT'
type Change = { group: string; field: string; value: ConfigValue }
type Reply<T> = {
  ok: boolean
  status: number
  data: T & { code?: number; message?: string; detail?: string }
}

declare function connectPanel(): {
  getContext(): Promise<State & Snapshot & { target: Target }>
  translate(keys: string[]): Promise<{ messages: Record<string, string> }>
  request<T>(method: Method, path: string, body?: unknown): Promise<Reply<T>>
  saveConfig(changes: Change[], phase: 'commit'): Promise<{ ok: boolean; message?: string }>
  pickPath(options: {
    mode: 'file' | 'folder'
    defaultPath?: string
  }): Promise<{ path: string | null }>
  confirm(options: { message: string; danger?: boolean }): Promise<{ confirmed: boolean }>
  onConfigReloaded(callback: (snapshot: Snapshot) => void): void
  onStateChange(callback: (state: State) => void): void
}

const prefix = 'plugins.mss_adapter.'
const labels = Object.keys(locale).filter(key => key.startsWith(prefix + 'panel.'))
const translationKeys = labels.map(key => key.slice(prefix.length))

export function usePanel() {
  const panel = connectPanel()
  const config = shallowRef<Config>({})
  const state = shallowRef<State>()
  const target = shallowRef<Target>()
  const messages = shallowRef<Record<string, string>>({})
  const working = ref(false)
  const error = ref('')
  const status = ref('')
  const disabled = computed(
    () => !state.value || state.value.locked || state.value.busy || working.value
  )
  let translationVersion = 0

  function t(key: string) {
    const full = 'panel.' + key
    return messages.value[full] ?? (locale as Record<string, string>)[prefix + full] ?? key
  }

  async function applyState(next: State) {
    const previous = state.value
    state.value = next
    if (previous?.locale === next.locale && previous.i18nRevision === next.i18nRevision) return
    const version = ++translationVersion
    const { messages: translated } = await panel.translate(translationKeys)
    if (version === translationVersion) {
      messages.value = translated
      document.documentElement.lang = next.locale
    }
  }

  function report(cause: unknown) {
    error.value = cause instanceof Error ? cause.message : String(cause)
  }

  async function initialize() {
    const initial = await panel.getContext()
    target.value = initial.target
    config.value = initial.config
    panel.onConfigReloaded(next => {
      config.value = next.config
    })
    panel.onStateChange(next => void applyState(next).catch(report))
    await applyState(initial)
  }

  async function run(operation: () => Promise<void>) {
    if (working.value) return
    working.value = true
    error.value = ''
    status.value = ''
    try {
      await operation()
    } catch (cause) {
      report(cause)
    } finally {
      working.value = false
    }
  }

  async function request<T>(method: Method, suffix = '', body?: unknown): Promise<T> {
    const active = target.value!
    const path =
      active.kind === 'plan'
        ? `plan/${active.planId}`
        : active.userId
          ? `user/${active.scriptId}/${active.userId}`
          : `script/${active.scriptId}`
    const { ok, status, data } = await panel.request<T>(method, `/editor/${path}${suffix}`, body)
    if (!ok || (data?.code !== undefined && data.code !== 200)) {
      const message = data?.message || data?.detail
      throw new Error(typeof message === 'string' ? message : `HTTP ${status}`)
    }
    return data
  }

  async function save(changes: Change[]) {
    const result = await panel.saveConfig(changes, 'commit')
    if (!result.ok) throw new Error(result.message)
  }

  function saveField(group: string, field: string, value: ConfigValue) {
    return save([{ group, field, value }])
  }

  function value(group: string, field: string) {
    return config.value[group]?.[field] as Value | undefined
  }

  async function pickPath(mode: 'file' | 'folder', defaultPath?: string) {
    return (await panel.pickPath({ mode, defaultPath })).path
  }

  async function confirm(message: string, danger = false) {
    return (await panel.confirm({ message, danger })).confirmed
  }

  return {
    config,
    target,
    state,
    disabled,
    working,
    error,
    status,
    t,
    initialize,
    report,
    request,
    save,
    saveField,
    value,
    pickPath,
    confirm,
    run,
  }
}

export type Panel = ReturnType<typeof usePanel>

export const input = (event: Event) => event.target as HTMLInputElement
export const selection = (event: Event) => event.target as HTMLSelectElement
