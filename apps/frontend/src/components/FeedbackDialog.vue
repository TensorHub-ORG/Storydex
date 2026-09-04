<template>
  <div v-if="open" class="feedback-mask" @click.self="emit('close')">
    <section class="feedback-dialog" role="dialog" aria-modal="true" aria-labelledby="feedback-title">
      <header class="feedback-head">
        <div>
          <h2 id="feedback-title">{{ source === "error" ? "反馈报错" : "提交反馈" }}</h2>
          <p>提交内容包括你填写的描述、联系方式、所选截图，以及脱敏后的错误日志和基础诊断；不会自动附加对话正文、小说内容、API Key 或项目文件。</p>
        </div>
        <button type="button" title="关闭" aria-label="关闭" @click="emit('close')">
          <span class="material-symbols-rounded">close</span>
        </button>
      </header>

      <label class="feedback-field">
        <span>类型</span>
        <select v-model="category">
          <option value="bug">功能异常</option>
          <option value="stability">稳定性问题</option>
          <option value="update">更新安装</option>
          <option value="suggestion">建议</option>
          <option value="other">其他</option>
        </select>
      </label>
      <label class="feedback-field">
        <span>问题描述</span>
        <textarea v-model="description" rows="5" maxlength="5000" placeholder="请描述发生了什么、预期结果和复现步骤"></textarea>
      </label>
      <label class="feedback-field">
        <span>联系方式（可选）</span>
        <input v-model="contact" maxlength="200" placeholder="邮箱或其他联系方式" />
      </label>

      <div class="feedback-images">
        <div class="feedback-images-head">
          <span>截图（最多 4 张，每张不超过 5 MB）</span>
          <label v-if="images.length < 4" class="feedback-image-add">
            <span class="material-symbols-rounded">add_photo_alternate</span>
            <input type="file" accept="image/png,image/jpeg,image/webp" multiple @change="handleImages" />
          </label>
        </div>
        <div v-if="images.length" class="feedback-preview-list">
          <figure v-for="(image, index) in images" :key="image.dataUrl">
            <img :src="image.dataUrl" :alt="image.name" />
            <button type="button" title="移除图片" @click="removeImage(index)">
              <span class="material-symbols-rounded">close</span>
            </button>
          </figure>
        </div>
      </div>

      <div v-if="statusMessage" class="feedback-status" :class="statusTone">{{ statusMessage }}</div>
      <footer class="feedback-actions">
        <button type="button" @click="emit('close')">取消</button>
        <button class="primary" type="button" :disabled="submitting || description.trim().length < 5" @click="send">
          <span class="material-symbols-rounded">send</span>
          {{ submitting ? "提交中…" : "提交反馈" }}
        </button>
      </footer>
    </section>
  </div>
</template>

<script setup lang="ts">
import { ref, watch } from "vue";
import { submitFeedback } from "@/api/system";
import type { FeedbackImagePayload } from "@/types/system";

const props = withDefaults(defineProps<{
  open: boolean;
  source: "error" | "settings";
  errorMessage?: string;
  errorType?: string;
  errorDetails?: Record<string, unknown>;
  diagnostics?: Record<string, unknown>;
}>(), {
  errorMessage: "",
  errorType: "",
  errorDetails: () => ({}),
  diagnostics: () => ({})
});
const emit = defineEmits<{ close: []; submitted: [feedbackId: string] }>();
const category = ref(props.source === "error" ? "bug" : "suggestion");
const description = ref(props.source === "error" ? props.errorMessage.slice(0, 1000) : "");
const contact = ref("");
const images = ref<FeedbackImagePayload[]>([]);
const submitting = ref(false);
const statusMessage = ref("");
const statusTone = ref<"success" | "error">("success");

watch(() => props.open, (open) => {
  if (!open) return;
  category.value = props.source === "error" ? "bug" : "suggestion";
  description.value = props.source === "error" ? props.errorMessage.slice(0, 1000) : "";
  contact.value = "";
  images.value = [];
  statusMessage.value = "";
});

async function fileToPayload(file: File): Promise<FeedbackImagePayload> {
  return await new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve({ name: file.name, mimeType: file.type, dataUrl: String(reader.result || "") });
    reader.onerror = () => reject(reader.error || new Error("图片读取失败"));
    reader.readAsDataURL(file);
  });
}

async function handleImages(event: Event): Promise<void> {
  const input = event.target as HTMLInputElement;
  const selected = Array.from(input.files || []).slice(0, 4 - images.value.length);
  const invalid = selected.find((file) => !["image/png", "image/jpeg", "image/webp"].includes(file.type) || file.size > 5 * 1024 * 1024);
  if (invalid) {
    statusTone.value = "error";
    statusMessage.value = "仅支持 PNG、JPEG、WebP，且每张图片不超过 5 MB。";
  } else {
    images.value.push(...await Promise.all(selected.map(fileToPayload)));
    statusMessage.value = "";
  }
  input.value = "";
}

function removeImage(index: number): void {
  images.value.splice(index, 1);
}

async function send(): Promise<void> {
  if (submitting.value || description.value.trim().length < 5) return;
  submitting.value = true;
  statusMessage.value = "";
  try {
    const result = await submitFeedback({
      source: props.source,
      category: category.value,
      description: description.value.trim(),
      contact: contact.value.trim(),
      errorMessage: props.errorMessage,
      errorType: props.errorType,
      errorDetails: props.errorDetails,
      diagnostics: props.diagnostics,
      images: images.value
    });
    statusTone.value = "success";
    statusMessage.value = `反馈已提交（${result.data.feedbackId}）`;
    emit("submitted", result.data.feedbackId);
  } catch (error) {
    statusTone.value = "error";
    statusMessage.value = error instanceof Error ? error.message : String(error);
  } finally {
    submitting.value = false;
  }
}
</script>

<style scoped>
.feedback-mask { position: fixed; inset: 0; z-index: 2400; display: grid; place-items: center; padding: 20px; background: rgb(0 0 0 / 42%); }
.feedback-dialog { width: min(620px, 100%); max-height: min(760px, 92vh); overflow: auto; padding: 20px; border: 1px solid var(--border-subtle); border-radius: var(--radius-lg); background: var(--surface-overlay); color: var(--text-main); box-shadow: var(--shadow-modal); }
.feedback-head, .feedback-actions, .feedback-images-head { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.feedback-head h2 { margin: 0; font-size: 18px; letter-spacing: 0; }.feedback-head p { margin: 4px 0 0; color: var(--text-secondary); font-size: 12px; }
.feedback-head button, .feedback-preview-list button { width: 32px; height: 32px; display: grid; place-items: center; border: 0; background: transparent; color: inherit; cursor: pointer; }
.feedback-field { display: grid; gap: 6px; margin-top: 16px; font-size: 13px; }.feedback-field input, .feedback-field select, .feedback-field textarea { width: 100%; box-sizing: border-box; padding: 9px 10px; border: 1px solid var(--border); border-radius: var(--radius-md); background: var(--input-bg); color: inherit; font: inherit; resize: vertical; }
.feedback-images { margin-top: 16px; }.feedback-image-add { width: 34px; height: 34px; display: grid; place-items: center; border: 1px solid var(--border); border-radius: var(--radius-md); cursor: pointer; }.feedback-image-add input { display: none; }
.feedback-preview-list { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 8px; margin-top: 10px; }.feedback-preview-list figure { position: relative; margin: 0; aspect-ratio: 4 / 3; overflow: hidden; border: 1px solid var(--border); border-radius: var(--radius-md); }.feedback-preview-list img { width: 100%; height: 100%; object-fit: cover; }.feedback-preview-list button { position: absolute; top: 2px; right: 2px; border-radius: var(--radius-full); background: rgb(0 0 0 / 60%); color: white; }
.feedback-status { margin-top: 14px; font-size: 12px; overflow-wrap: anywhere; }.feedback-status.success { color: var(--success); }.feedback-status.error { color: var(--danger); }
.feedback-actions { margin-top: 18px; justify-content: flex-end; }.feedback-actions button { min-height: 36px; padding: 0 14px; border: 1px solid var(--border); border-radius: var(--radius-md); background: transparent; color: inherit; cursor: pointer; }.feedback-actions .primary { display: inline-flex; align-items: center; gap: 6px; border-color: var(--accent); background: var(--accent); color: white; }.feedback-actions button:disabled { opacity: .5; cursor: default; }
@media (max-width: 560px) { .feedback-mask { padding: 8px; }.feedback-dialog { max-height: 96vh; padding: 16px; }.feedback-preview-list { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
</style>
