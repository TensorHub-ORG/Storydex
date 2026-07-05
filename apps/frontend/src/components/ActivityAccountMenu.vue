<template>
  <section class="activity-settings-menu activity-account-menu" @click.stop>
    <div class="activity-account-head">
      <div class="activity-account-copy">
        <div class="activity-account-title">账号系统</div>
        <div class="activity-account-subtitle">
          {{ authStore.isAuthenticated ? "已连接数据库账号" : "登录后同步 Storydex 账号" }}
        </div>
      </div>

      <div v-if="authStore.isAuthenticated" class="activity-account-avatar">
        {{ authStore.initials }}
      </div>
    </div>

    <div v-if="authStore.isAuthenticated" class="activity-account-body">
      <div class="activity-account-tabs activity-account-tabs-auth" role="tablist" aria-label="账号功能">
        <button
          class="activity-account-tab"
          :class="{ active: activeAccountView === 'profile' }"
          type="button"
          @click="setActiveAccountView('profile')"
        >
          <span class="material-symbols-rounded activity-account-tab-icon">badge</span>
          <span class="activity-account-tab-label">个人资料</span>
        </button>
        <button
          class="activity-account-tab"
          :class="{ active: activeAccountView === 'password' }"
          type="button"
          @click="setActiveAccountView('password')"
        >
          <span class="material-symbols-rounded activity-account-tab-icon">key</span>
          <span class="activity-account-tab-label">修改密码</span>
        </button>
      </div>

      <section class="activity-account-section activity-account-summary">
        <div class="activity-account-userline">
          <div class="activity-account-name">{{ authStore.displayName }}</div>
          <div class="activity-account-handle">@{{ authStore.user?.username }}</div>
        </div>
        <div class="activity-account-email">{{ authStore.user?.email || "未设置邮箱" }}</div>

        <div class="activity-account-stats">
          <div class="activity-account-stat">
            <span class="activity-account-stat-label">角色</span>
            <span class="activity-account-stat-value">{{ authStore.user?.role || "USER" }}</span>
          </div>
          <div class="activity-account-stat">
            <span class="activity-account-stat-label">额度</span>
            <span class="activity-account-stat-value">{{ quotaLabel }}</span>
          </div>
        </div>
      </section>

      <section v-if="activeAccountView === 'profile'" class="activity-account-section">
        <div class="activity-account-section-title">个人资料</div>

        <label class="activity-account-field">
          <span>昵称</span>
          <input
            v-model.trim="profileForm.nickname"
            class="modal-input"
            type="text"
            placeholder="用于界面显示的昵称"
          />
        </label>

        <label class="activity-account-field">
          <span>邮箱</span>
          <input
            v-model.trim="profileForm.email"
            class="modal-input"
            type="email"
            placeholder="可选，用于账号资料"
          />
        </label>

        <label class="activity-account-field">
          <span>头像地址</span>
          <input
            v-model.trim="profileForm.avatar"
            class="modal-input"
            type="url"
            placeholder="可选，填写头像图片链接"
          />
        </label>

        <div class="activity-account-actions">
          <button
            class="activity-account-btn is-muted"
            type="button"
            :disabled="authStore.isSavingProfile"
            @click="resetProfileForm"
          >
            重置
          </button>
          <button
            class="activity-account-btn is-primary"
            type="button"
            :disabled="authStore.isSavingProfile"
            @click="handleSaveProfile"
          >
            {{ authStore.isSavingProfile ? "保存中..." : "保存资料" }}
          </button>
        </div>
      </section>

      <section v-else class="activity-account-section">
        <div class="activity-account-section-title">修改密码</div>

        <label class="activity-account-field">
          <span>当前密码</span>
          <input
            v-model="passwordForm.oldPassword"
            class="modal-input"
            type="password"
            placeholder="输入当前密码"
          />
        </label>

        <label class="activity-account-field">
          <span>新密码</span>
          <input
            v-model="passwordForm.newPassword"
            class="modal-input"
            type="password"
            placeholder="至少 6 位"
          />
        </label>

        <label class="activity-account-field">
          <span>确认新密码</span>
          <input
            v-model="passwordConfirm"
            class="modal-input"
            type="password"
            placeholder="再次输入新密码"
          />
        </label>

        <div class="activity-account-actions">
          <button
            class="activity-account-btn is-primary"
            type="button"
            :disabled="authStore.isChangingPassword"
            @click="handleChangePassword"
          >
            {{ authStore.isChangingPassword ? "更新中..." : "更新密码" }}
          </button>
        </div>
      </section>

      <section class="activity-account-section activity-account-section-actions">
        <button
          class="activity-account-inline-btn"
          type="button"
          :disabled="authStore.isLoadingSummary"
          @click="handleRefreshSummary"
        >
          {{ authStore.isLoadingSummary ? "同步中..." : "刷新账号信息" }}
        </button>
        <button class="activity-account-inline-btn is-danger" type="button" @click="handleLogout">
          退出登录
        </button>
      </section>
    </div>

    <div v-else class="activity-account-body">
      <div class="activity-account-tabs" role="tablist" aria-label="账号操作">
        <button
          class="activity-account-tab"
          :class="{ active: activeView === 'login' }"
          type="button"
          @click="setActiveView('login')"
        >
          <span class="material-symbols-rounded activity-account-tab-icon">login</span>
          <span class="activity-account-tab-label">登录</span>
        </button>
        <button
          class="activity-account-tab"
          :class="{ active: activeView === 'register' }"
          type="button"
          @click="setActiveView('register')"
        >
          <span class="material-symbols-rounded activity-account-tab-icon">person_add</span>
          <span class="activity-account-tab-label">注册</span>
        </button>
      </div>

      <section class="activity-account-section">
        <template v-if="activeView === 'login'">
          <p class="activity-account-note">使用已存在的数据库账号继续进入当前工作台。</p>

          <label class="activity-account-field">
            <span>用户名</span>
            <input
              v-model.trim="loginForm.username"
              class="modal-input"
              type="text"
              placeholder="输入用户名"
              @keydown.enter.prevent="handleLogin"
            />
          </label>

          <label class="activity-account-field">
            <span>密码</span>
            <input
              v-model="loginForm.password"
              class="modal-input"
              type="password"
              placeholder="输入密码"
              @keydown.enter.prevent="handleLogin"
            />
          </label>

          <div class="activity-account-actions">
            <button
              class="activity-account-btn is-primary"
              type="button"
              :disabled="authStore.isAuthenticating"
              @click="handleLogin"
            >
              {{ authStore.isAuthenticating ? "登录中..." : "登录账号" }}
            </button>
          </div>
        </template>

        <template v-else>
          <p class="activity-account-note">创建一个新账号后会自动登录，并同步到账户面板。</p>

          <label class="activity-account-field">
            <span>用户名</span>
            <input
              v-model.trim="registerForm.username"
              class="modal-input"
              type="text"
              placeholder="用户名不能为空"
              @blur="handleUsernameBlur"
            />
          </label>

          <div v-if="usernameHint" class="activity-account-hint">{{ usernameHint }}</div>

          <label class="activity-account-field">
            <span>邮箱</span>
            <input
              v-model.trim="registerForm.email"
              class="modal-input"
              type="email"
              placeholder="可选，用于账号资料"
            />
          </label>

          <label class="activity-account-field">
            <span>密码</span>
            <input
              v-model="registerForm.password"
              class="modal-input"
              type="password"
              placeholder="至少 6 位"
            />
          </label>

          <label class="activity-account-field">
            <span>确认密码</span>
            <input
              v-model="registerConfirm"
              class="modal-input"
              type="password"
              placeholder="再次输入密码"
              @keydown.enter.prevent="handleRegister"
            />
          </label>

          <div class="activity-account-actions">
            <button
              class="activity-account-btn is-primary"
              type="button"
              :disabled="authStore.isAuthenticating"
              @click="handleRegister"
            >
              {{ authStore.isAuthenticating ? "创建中..." : "注册并登录" }}
            </button>
          </div>
        </template>
      </section>
    </div>

    <div v-if="feedbackMessage" class="activity-account-feedback" :class="{ 'is-error': feedbackTone === 'error' }">
      {{ feedbackMessage }}
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from "vue";
import { checkUsernameAvailability } from "@/api/auth";
import { useAuthStore } from "@/stores/auth";

type AuthView = "login" | "register";
type AccountView = "profile" | "password";
type FeedbackTone = "error" | "success";

const authStore = useAuthStore();

const activeView = ref<AuthView>("login");
const activeAccountView = ref<AccountView>("profile");
const feedbackMessage = ref("");
const feedbackTone = ref<FeedbackTone>("success");
const usernameHint = ref("");

const loginForm = reactive({
  username: "",
  password: ""
});

const registerForm = reactive({
  username: "",
  email: "",
  password: ""
});

const registerConfirm = ref("");

const profileForm = reactive({
  nickname: "",
  email: "",
  avatar: ""
});

const passwordForm = reactive({
  oldPassword: "",
  newPassword: ""
});

const passwordConfirm = ref("");

const quotaLabel = computed(() => {
  const quota = authStore.summary?.quota;
  if (!quota) {
    return authStore.isLoadingSummary ? "同步中..." : "未同步";
  }
  if (quota.isUnlimited) {
    return "无限制";
  }
  return `${quota.balance}`;
});

watch(
  () => authStore.user,
  () => {
    resetProfileForm();
    resetPasswordForm();
    if (authStore.isAuthenticated) {
      activeView.value = "login";
      activeAccountView.value = "profile";
    }
  },
  { immediate: true }
);

watch(
  () => authStore.authError,
  (message) => {
    if (!message) {
      return;
    }
    feedbackMessage.value = message;
    feedbackTone.value = "error";
  }
);

onMounted(() => {
  if (authStore.isAuthenticated) {
    void authStore.refreshSummary({ silentAuthFailure: false });
  }
});

function setActiveView(view: AuthView): void {
  activeView.value = view;
  feedbackMessage.value = "";
  authStore.clearAuthError();
}

function setActiveAccountView(view: AccountView): void {
  activeAccountView.value = view;
  feedbackMessage.value = "";
  authStore.clearAuthError();
}

function setSuccessMessage(message: string): void {
  feedbackMessage.value = message;
  feedbackTone.value = "success";
}

function setErrorMessage(message: string): void {
  feedbackMessage.value = message;
  feedbackTone.value = "error";
}

function resetProfileForm(): void {
  profileForm.nickname = authStore.user?.nickname || "";
  profileForm.email = authStore.user?.email || "";
  profileForm.avatar = authStore.user?.avatar || "";
}

function resetPasswordForm(): void {
  passwordForm.oldPassword = "";
  passwordForm.newPassword = "";
  passwordConfirm.value = "";
}

async function handleLogin(): Promise<void> {
  feedbackMessage.value = "";
  authStore.clearAuthError();

  if (!loginForm.username.trim() || !loginForm.password) {
    setErrorMessage("请输入用户名和密码。");
    return;
  }

  const succeeded = await authStore.login({
    username: loginForm.username.trim(),
    password: loginForm.password
  });

  if (!succeeded) {
    return;
  }

  loginForm.password = "";
  setSuccessMessage("已登录账号，并同步当前资料。");
}

async function handleRegister(): Promise<void> {
  feedbackMessage.value = "";
  authStore.clearAuthError();

  if (!registerForm.username.trim()) {
    setErrorMessage("用户名不能为空。");
    return;
  }

  if (registerForm.password.length < 6) {
    setErrorMessage("密码至少需要 6 位。");
    return;
  }

  if (registerForm.password !== registerConfirm.value) {
    setErrorMessage("两次输入的密码不一致。");
    return;
  }

  const succeeded = await authStore.register({
    username: registerForm.username.trim(),
    password: registerForm.password,
    email: registerForm.email.trim() || null
  });

  if (!succeeded) {
    return;
  }

  registerForm.password = "";
  registerConfirm.value = "";
  setSuccessMessage("账号已创建并自动登录。");
}

async function handleSaveProfile(): Promise<void> {
  feedbackMessage.value = "";
  authStore.clearAuthError();

  const succeeded = await authStore.updateProfile({
    nickname: profileForm.nickname.trim() || null,
    email: profileForm.email.trim() || null,
    avatar: profileForm.avatar.trim() || null
  });

  if (!succeeded) {
    return;
  }

  setSuccessMessage("账号资料已保存。");
}

async function handleChangePassword(): Promise<void> {
  feedbackMessage.value = "";
  authStore.clearAuthError();

  if (!passwordForm.oldPassword || !passwordForm.newPassword) {
    setErrorMessage("请先填写完整的密码信息。");
    return;
  }

  if (passwordForm.newPassword.length < 6) {
    setErrorMessage("新密码至少需要 6 位。");
    return;
  }

  if (passwordForm.newPassword !== passwordConfirm.value) {
    setErrorMessage("两次输入的新密码不一致。");
    return;
  }

  const succeeded = await authStore.changePassword({
    oldPassword: passwordForm.oldPassword,
    newPassword: passwordForm.newPassword
  });

  if (!succeeded) {
    return;
  }

  resetPasswordForm();
  setSuccessMessage("密码已更新。");
}

async function handleRefreshSummary(): Promise<void> {
  feedbackMessage.value = "";
  authStore.clearAuthError();

  const summary = await authStore.refreshSummary({ silentAuthFailure: false });
  if (!summary) {
    return;
  }

  setSuccessMessage("账号信息已刷新。");
}

async function handleLogout(): Promise<void> {
  feedbackMessage.value = "";
  authStore.clearAuthError();
  await authStore.logout();
  resetPasswordForm();
  setSuccessMessage("已退出当前账号。");
}

async function handleUsernameBlur(): Promise<void> {
  usernameHint.value = "";
  const username = registerForm.username.trim();
  if (!username) {
    return;
  }

  try {
    const result = await checkUsernameAvailability(username);
    usernameHint.value = result.data.available ? "该用户名当前可用。" : "该用户名已存在。";
  } catch {
    usernameHint.value = "";
  }
}
</script>

<style scoped>
.activity-account-menu {
  width: 336px;
  max-height: calc(100vh - 40px);
  overflow-y: auto;
}

.activity-account-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
  padding-bottom: 10px;
  border-bottom: 1px solid var(--border-ghost);
}

.activity-account-copy {
  min-width: 0;
}

.activity-account-title {
  font-size: 13px;
  font-weight: 600;
  color: var(--text-main);
}

.activity-account-subtitle {
  margin-top: 4px;
  color: var(--text-muted);
  font-size: 12px;
  line-height: 1.5;
}

.activity-account-avatar {
  width: 28px;
  height: 28px;
  border-radius: 999px;
  display: grid;
  place-items: center;
  background: var(--accent-soft);
  color: var(--accent);
  font-size: 12px;
  font-weight: 700;
  flex-shrink: 0;
}

.activity-account-body {
  display: flex;
  flex-direction: column;
}

.activity-account-section {
  padding: 12px 0;
  border-bottom: 1px solid var(--border-ghost);
}

.activity-account-section:last-of-type {
  border-bottom: 0;
}

.activity-account-summary {
  padding-top: 12px;
}

.activity-account-section-title {
  margin-bottom: 10px;
  color: var(--text-main);
  font-size: 12px;
  font-weight: 600;
}

.activity-account-userline {
  display: flex;
  align-items: baseline;
  gap: 8px;
}

.activity-account-name {
  font-size: 14px;
  font-weight: 600;
  color: var(--text-main);
}

.activity-account-handle,
.activity-account-email,
.activity-account-note,
.activity-account-hint {
  color: var(--text-muted);
  font-size: 12px;
  line-height: 1.5;
}

.activity-account-email {
  margin-top: 4px;
}

.activity-account-stats {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 12px;
  margin-top: 12px;
}

.activity-account-stat {
  display: flex;
  flex-direction: column;
  gap: 3px;
}

.activity-account-stat-label {
  color: var(--text-faint);
  font-size: 11px;
  text-transform: uppercase;
  letter-spacing: 0.08em;
}

.activity-account-stat-value {
  color: var(--text-main);
  font-size: 13px;
  font-weight: 600;
}

.activity-account-field {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.activity-account-field + .activity-account-field {
  margin-top: 10px;
}

.activity-account-field span {
  color: var(--text-soft);
  font-size: 12px;
}

.activity-account-actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  margin-top: 12px;
}

.activity-account-btn,
.activity-account-inline-btn,
.activity-account-tab {
  min-height: 32px;
  border: 1px solid transparent;
  border-radius: 6px;
  background: transparent;
  color: var(--text-main);
  cursor: pointer;
  transition: background 160ms ease, border-color 160ms ease, color 160ms ease;
}

.activity-account-btn {
  padding: 0 12px;
  font-size: 12px;
}

.activity-account-btn.is-muted {
  border-color: var(--border-subtle);
  color: var(--text-soft);
}

.activity-account-btn.is-muted:hover:not(:disabled) {
  background: var(--bg-hover);
  color: var(--text-main);
}

.activity-account-btn.is-primary {
  background: var(--accent);
  color: var(--accent-contrast);
}

.activity-account-btn.is-primary:hover:not(:disabled) {
  background: var(--accent-strong);
}

.activity-account-section-actions {
  display: flex;
  justify-content: space-between;
  gap: 8px;
}

.activity-account-inline-btn {
  flex: 1;
  border-color: var(--border-subtle);
  font-size: 12px;
}

.activity-account-inline-btn:hover:not(:disabled) {
  background: var(--bg-hover);
}

.activity-account-inline-btn.is-danger {
  color: var(--danger);
}

.activity-account-tabs {
  display: flex;
  align-items: flex-end;
  gap: 18px;
  margin-top: 12px;
  padding: 0 2px;
  border-bottom: 1px solid var(--border-ghost);
}

.activity-account-tabs-auth {
  padding-top: 8px;
}

.activity-account-tab {
  position: relative;
  min-height: 38px;
  padding: 0 2px 11px;
  border: 0;
  border-radius: 0;
  background: transparent;
  color: var(--text-muted);
  display: inline-flex;
  align-items: center;
  gap: 8px;
  font-size: 12px;
  font-weight: 600;
}

.activity-account-tab::after {
  content: "";
  position: absolute;
  left: 0;
  right: 0;
  bottom: -1px;
  height: 2px;
  border-radius: 999px;
  background: transparent;
  transition: background 160ms ease, opacity 160ms ease, transform 160ms ease;
  opacity: 0;
  transform: scaleX(0.55);
}

.activity-account-tab:hover:not(:disabled) {
  color: var(--text-main);
}

.activity-account-tab.active {
  color: var(--text-main);
}

.activity-account-tab.active::after {
  background: var(--accent);
  opacity: 1;
  transform: scaleX(1);
}

.activity-account-tab-icon {
  color: var(--text-faint);
  font-size: 15px;
  transition: color 160ms ease, transform 160ms ease;
}

.activity-account-tab:hover:not(:disabled) .activity-account-tab-icon {
  color: var(--text-soft);
}

.activity-account-tab.active .activity-account-tab-icon {
  color: var(--accent);
  transform: translateY(-0.5px);
}

.activity-account-tab-label {
  white-space: nowrap;
  letter-spacing: 0.01em;
}

.activity-account-feedback {
  margin-top: 10px;
  padding-top: 10px;
  border-top: 1px solid var(--border-ghost);
  color: var(--success);
  font-size: 12px;
  line-height: 1.5;
}

.activity-account-feedback.is-error {
  color: var(--danger);
}

.activity-account-btn:disabled,
.activity-account-inline-btn:disabled,
.activity-account-tab:disabled {
  opacity: 0.55;
  cursor: not-allowed;
}
</style>
