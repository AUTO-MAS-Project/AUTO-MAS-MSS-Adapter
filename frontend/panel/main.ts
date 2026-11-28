import { createApp } from 'vue'
import ScriptPanel from './ScriptPanel.vue'
import UserPanel from './UserPanel.vue'
import MSSPlanTable from './MSSPlanTable.vue'
import './panel.css'

const panels = { script: ScriptPanel, user: UserPanel, plan: MSSPlanTable }
createApp(panels[document.body.dataset.page as keyof typeof panels]).mount('#app')
