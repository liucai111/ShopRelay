import { createApp } from 'vue'
import {
  ElAlert, ElButton, ElDialog, ElForm, ElFormItem, ElIcon, ElInput,
  ElSkeleton, ElTag,
} from 'element-plus'
import { ElLoadingDirective as loadingDirective } from 'element-plus/es/components/loading/index.mjs'
import App from './App.vue'
import 'element-plus/theme-chalk/base.css'
import 'element-plus/theme-chalk/el-alert.css'
import 'element-plus/theme-chalk/el-button.css'
import 'element-plus/theme-chalk/el-dialog.css'
import 'element-plus/theme-chalk/el-form.css'
import 'element-plus/theme-chalk/el-form-item.css'
import 'element-plus/theme-chalk/el-icon.css'
import 'element-plus/theme-chalk/el-input.css'
import 'element-plus/theme-chalk/el-loading.css'
import 'element-plus/theme-chalk/el-message.css'
import 'element-plus/theme-chalk/el-message-box.css'
import 'element-plus/theme-chalk/el-overlay.css'
import 'element-plus/theme-chalk/el-skeleton.css'
import 'element-plus/theme-chalk/el-tag.css'
import './style.css'

const app = createApp(App)
for (const component of [ElAlert, ElButton, ElDialog, ElForm, ElFormItem, ElIcon, ElInput, ElSkeleton, ElTag]) {
  app.component(component.name!, component)
}
app.directive('loading', loadingDirective)
app.mount('#app')
