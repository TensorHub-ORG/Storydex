import { defineStore } from "pinia";
import { describeTransportError, setApiAuthToken } from "@/api/client";
import {
  AuthApiError,
  changeAccountPassword,
  fetchAccountSummary,
  fetchCurrentAccount,
  fetchPersistedSession,
  loginAccount,
  logoutAccount,
  registerAccount,
  updateAccountProfile
} from "@/api/auth";
import type {
  AccountSummaryResponse,
  AuthUser,
  ChangePasswordRequest,
  LoginAccountRequest,
  RegisterAccountRequest,
  UpdateProfileRequest
} from "@/types/auth";

interface AuthState {
  bootstrapped: boolean;
  isBootstrapping: boolean;
  isAuthenticating: boolean;
  isLoadingSummary: boolean;
  isSavingProfile: boolean;
  isChangingPassword: boolean;
  authToken: string;
  user: AuthUser | null;
  summary: AccountSummaryResponse | null;
  authError: string;
}

export const useAuthStore = defineStore("auth", {
  state: (): AuthState => ({
    bootstrapped: false,
    isBootstrapping: false,
    isAuthenticating: false,
    isLoadingSummary: false,
    isSavingProfile: false,
    isChangingPassword: false,
    authToken: "",
    user: null,
    summary: null,
    authError: ""
  }),

  getters: {
    isAuthenticated(state): boolean {
      return state.bootstrapped && Boolean(state.authToken && state.user);
    },

    displayName(state): string {
      if (!state.user) {
        return "未登录";
      }
      return state.user.nickname?.trim() || state.user.username;
    },

    initials(state): string {
      const source = state.user?.nickname?.trim() || state.user?.username?.trim() || "S";
      return source.slice(0, 1).toUpperCase();
    }
  },

  actions: {
    clearAuthError(): void {
      this.authError = "";
    },

    async bootstrap(): Promise<void> {
      if (this.bootstrapped || this.isBootstrapping) {
        return;
      }

      this.isBootstrapping = true;
      this.authError = "";

      try {
        const result = await fetchPersistedSession();
        if (!result.data.authenticated || !result.data.accessToken || !result.data.user) {
          this.clearSession();
          this.bootstrapped = true;
          return;
        }

        this.setSession(result.data.accessToken, result.data.user);
        await this.refreshSummary({ silentAuthFailure: true });
        this.bootstrapped = true;
      } catch (error: unknown) {
        this.clearSession();
        this.authError = normalizeAuthError(error, "加载登录状态失败。");
        this.bootstrapped = true;
      } finally {
        this.isBootstrapping = false;
      }
    },

    async login(payload: LoginAccountRequest): Promise<boolean> {
      this.isAuthenticating = true;
      this.authError = "";

      try {
        const result = await loginAccount(payload);
        this.setSession(result.data.accessToken, result.data.user);
        await this.refreshSummary({ silentAuthFailure: false });
        return true;
      } catch (error: unknown) {
        this.authError = normalizeAuthError(error, "登录失败，请稍后重试。");
        return false;
      } finally {
        this.isAuthenticating = false;
      }
    },

    async register(payload: RegisterAccountRequest): Promise<boolean> {
      this.isAuthenticating = true;
      this.authError = "";

      try {
        await registerAccount(payload);
        const loginSucceeded = await this.login({
          username: payload.username,
          password: payload.password
        });
        if (!loginSucceeded && !this.authError) {
          this.authError = "注册成功，但自动登录失败，请手动登录。";
        }
        return loginSucceeded;
      } catch (error: unknown) {
        this.authError = normalizeAuthError(error, "注册失败，请稍后重试。");
        return false;
      } finally {
        this.isAuthenticating = false;
      }
    },

    async refreshUser(options?: { silentAuthFailure?: boolean }): Promise<AuthUser | null> {
      if (!this.authToken) {
        return null;
      }

      this.authError = "";

      try {
        const result = await fetchCurrentAccount();
        this.user = result.data;
        return result.data;
      } catch (error: unknown) {
        if (shouldClearSession(error)) {
          this.clearSession();
          if (!options?.silentAuthFailure) {
            this.authError = "登录状态已失效，请重新登录。";
          }
          return null;
        }

        this.authError = normalizeAuthError(error, "获取账号信息失败。");
        return null;
      }
    },

    async refreshSummary(options?: { silentAuthFailure?: boolean }): Promise<AccountSummaryResponse | null> {
      if (!this.authToken) {
        this.summary = null;
        return null;
      }

      this.isLoadingSummary = true;
      this.authError = "";

      try {
        const result = await fetchAccountSummary();
        this.summary = result.data;
        this.user = result.data.user;
        return result.data;
      } catch (error: unknown) {
        if (shouldClearSession(error)) {
          this.clearSession();
          if (!options?.silentAuthFailure) {
            this.authError = "登录状态已失效，请重新登录。";
          }
          return null;
        }

        this.authError = normalizeAuthError(error, "获取账号摘要失败。");
        return null;
      } finally {
        this.isLoadingSummary = false;
      }
    },

    async updateProfile(payload: UpdateProfileRequest): Promise<boolean> {
      if (!this.authToken) {
        this.authError = "请先登录。";
        return false;
      }

      this.isSavingProfile = true;
      this.authError = "";

      try {
        const result = await updateAccountProfile(payload);
        this.user = result.data;

        if (this.summary) {
          this.summary = {
            ...this.summary,
            user: result.data
          };
        }

        return true;
      } catch (error: unknown) {
        if (shouldClearSession(error)) {
          this.clearSession();
          this.authError = "登录状态已失效，请重新登录。";
          return false;
        }

        this.authError = normalizeAuthError(error, "保存资料失败。");
        return false;
      } finally {
        this.isSavingProfile = false;
      }
    },

    async changePassword(payload: ChangePasswordRequest): Promise<boolean> {
      if (!this.authToken) {
        this.authError = "请先登录。";
        return false;
      }

      this.isChangingPassword = true;
      this.authError = "";

      try {
        await changeAccountPassword({
          currentPassword: payload.oldPassword,
          newPassword: payload.newPassword
        });
        return true;
      } catch (error: unknown) {
        if (shouldClearSession(error)) {
          this.clearSession();
          this.authError = "登录状态已失效，请重新登录。";
          return false;
        }

        this.authError = normalizeAuthError(error, "修改密码失败。");
        return false;
      } finally {
        this.isChangingPassword = false;
      }
    },

    async logout(): Promise<void> {
      this.authError = "";

      try {
        if (this.authToken) {
          await logoutAccount();
        }
      } catch {
        // keep logout resilient
      } finally {
        this.clearSession();
      }
    },

    setSession(token: string, user: AuthUser): void {
      this.authToken = token.trim();
      this.user = user;
      setApiAuthToken(this.authToken);
    },

    clearSession(): void {
      this.authToken = "";
      this.user = null;
      this.summary = null;
      setApiAuthToken("");
    }
  }
});

function shouldClearSession(error: unknown): boolean {
  return error instanceof AuthApiError && error.status === 401;
}

function normalizeAuthError(error: unknown, fallbackMessage: string): string {
  if (error instanceof AuthApiError) {
    return error.message || fallbackMessage;
  }

  return describeTransportError(error, fallbackMessage);
}
