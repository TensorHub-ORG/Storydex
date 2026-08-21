<script setup lang="ts">
import { ref } from 'vue'
import { useRouter } from 'vue-router'
import PageHead from '@/components/PageHead.vue'
import CoomiIcon from '@/components/CoomiIcon.vue'
import { submitAndroidFeedback } from '@/utils/feedback'

const router = useRouter()
const category = ref('suggestion')
const description = ref('')
const contact = ref('')
const images = ref<Array<{ name: string; mimeType: string; dataUrl: string }>>([])
const sending = ref(false)
const result = ref<{ tone: 'ok' | 'error'; text: string } | null>(null)

function goBack() {
  if (window.CoomiAndroid?.openDashboard) window.CoomiAndroid.openDashboard()
  else if (window.history.length > 1) router.back()
  else void router.replace('/')
}

function fileToDataUrl(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onload = () => resolve(String(reader.result ?? ''))
    reader.onerror = () => reject(reader.error ?? new Error('图片读取失败'))
    reader.readAsDataURL(file)
  })
}

async function chooseImages(event: Event) {
  const input = event.target as HTMLInputElement
  const selected = Array.from(input.files ?? []).slice(0, 4 - images.value.length)
  const invalid = selected.find(file => !['image/png', 'image/jpeg', 'image/webp'].includes(file.type) || file.size > 5 * 1024 * 1024)
  if (invalid) result.value = { tone: 'error', text: '仅支持 PNG、JPEG、WebP，且每张图片不超过 5 MB' }
  else {
    images.value.push(...await Promise.all(selected.map(async file => ({ name: file.name, mimeType: file.type, dataUrl: await fileToDataUrl(file) }))))
    result.value = null
  }
  input.value = ''
}

function removeImage(index: number) { images.value.splice(index, 1) }

async function submit() {
  if (sending.value || description.value.trim().length < 5) return
  sending.value = true
  result.value = null
  const response = await submitAndroidFeedback({
    source: 'settings', category: category.value, description: description.value, contact: contact.value,
    images: images.value.map(image => ({ name: image.name, mimeType: image.mimeType, dataBase64: image.dataUrl.slice(image.dataUrl.indexOf(',') + 1) })),
  })
  sending.value = false
  if (response.ok) {
    result.value = { tone: 'ok', text: '反馈已提交，感谢你的建议' }
    description.value = ''
    images.value = []
  } else result.value = { tone: 'error', text: `提交失败：${response.error || '未知错误'}` }
}
</script>

<template>
  <div class="page">
    <PageHead title="反馈建议" @back="goBack" />
    <main class="body">
      <p class="privacy">不会上传对话正文、小说内容、API Key 或项目文件。</p>
      <label><span>类型</span><select v-model="category"><option value="bug">功能异常</option><option value="stability">稳定性问题</option><option value="update">更新安装</option><option value="suggestion">建议</option><option value="other">其他</option></select></label>
      <label><span>问题描述</span><textarea v-model="description" rows="8" maxlength="5000" placeholder="请描述发生了什么、预期结果或你的建议" /></label>
      <label><span>联系方式（可选）</span><input v-model="contact" maxlength="200" placeholder="邮箱或其他联系方式" /></label>
      <section class="images-field">
        <div class="images-head"><span>截图（最多 4 张，每张不超过 5 MB）</span><label v-if="images.length < 4" class="image-add" aria-label="添加截图"><CoomiIcon name="plus" :size="18" /><input type="file" accept="image/png,image/jpeg,image/webp" multiple @change="chooseImages" /></label></div>
        <div v-if="images.length" class="image-list"><figure v-for="(image, index) in images" :key="image.dataUrl"><img :src="image.dataUrl" :alt="image.name" /><button aria-label="移除图片" @click="removeImage(index)"><CoomiIcon name="close" :size="15" /></button></figure></div>
      </section>
      <p v-if="result" class="result" :class="result.tone"><CoomiIcon :name="result.tone === 'ok' ? 'check' : 'alert'" :size="15" />{{ result.text }}</p>
      <button class="submit" :disabled="sending || description.trim().length < 5" @click="submit"><CoomiIcon name="send" :size="16" />{{ sending ? '提交中…' : '提交反馈' }}</button>
    </main>
  </div>
</template>

<style scoped>
.page{display:flex;flex-direction:column;height:100%;background:var(--page)}.body{flex:1;overflow-y:auto;padding:16px 14px calc(28px + var(--safe-bottom))}.privacy{margin:0 0 16px;padding:10px 12px;border-radius:7px;background:var(--blue-soft);color:var(--blue);font-size:12px;line-height:1.55}label{display:grid;gap:7px;margin-bottom:16px;color:var(--text-2);font-size:13px}select,input,textarea{width:100%;padding:11px 12px;border:1px solid var(--border-strong);border-radius:7px;background:var(--bg);color:var(--text);font:inherit}textarea{resize:vertical;line-height:1.6}.images-field{margin-bottom:16px;color:var(--text-2);font-size:13px}.images-head{display:flex;align-items:center;justify-content:space-between;gap:10px}.image-add{display:grid;width:36px;height:36px;margin:0;place-items:center;border:1px solid var(--border-strong);border-radius:7px;color:var(--blue)}.image-add input{display:none}.image-list{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px;margin-top:10px}.image-list figure{position:relative;overflow:hidden;margin:0;aspect-ratio:4/3;border:1px solid var(--border);border-radius:7px;background:var(--fill)}.image-list img{width:100%;height:100%;object-fit:cover}.image-list button{position:absolute;top:4px;right:4px;display:grid;width:30px;height:30px;place-items:center;border-radius:50%;background:rgb(0 0 0 / 62%);color:#fff}.result{display:flex;align-items:center;gap:6px;font-size:12.5px}.result.ok{color:var(--ok)}.result.error{color:var(--danger)}.submit{display:flex;align-items:center;justify-content:center;gap:7px;width:100%;min-height:46px;margin-top:18px;border-radius:7px;background:var(--blue);color:var(--on-accent);font-size:14px;font-weight:650}.submit:disabled{opacity:.45}
</style>
