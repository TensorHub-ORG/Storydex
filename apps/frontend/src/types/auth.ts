export interface AuthUser {
  userId: string;
  username: string;
  email: string | null;
  nickname: string | null;
  avatar: string | null;
  role: string;
  isActive: boolean;
  createdAt: string;
  updatedAt: string | null;
  lastLoginAt: string | null;
}

export interface RegisterAccountRequest {
  username: string;
  password: string;
  email?: string | null;
}

export interface RegisterAccountResponse {
  success: boolean;
  message: string;
  user: AuthUser;
}

export interface LoginAccountRequest {
  username: string;
  password: string;
}

export interface LoginAccountResponse {
  accessToken: string;
  userId: string;
  username: string;
  role: string;
  user: AuthUser;
}

export interface PersistedSessionResponse {
  authenticated: boolean;
  accessToken: string;
  user: AuthUser | null;
}

export interface UpdateProfileRequest {
  nickname?: string | null;
  email?: string | null;
  avatar?: string | null;
}

export interface ChangePasswordRequest {
  oldPassword: string;
  newPassword: string;
}

export interface UpdatePasswordRequest {
  currentPassword: string;
  newPassword: string;
}

export interface AccountMessageResponse {
  success: boolean;
  message: string;
}

export interface CheckUsernameResponse {
  available: boolean;
}

export interface AccountSummaryQuota {
  balance: number;
  totalGranted: number;
  totalConsumed: number;
  isUnlimited: boolean;
  lastGrantedAt: string | null;
  lastConsumedAt: string | null;
}

export interface AccountSummaryProfile {
  defaultSessionId: string | null;
  defaultWorldbookId: string | null;
  defaultScriptId: string | null;
  allowPersonalApiKey: boolean;
  allowSystemQuota: boolean;
  quotaCostPerGeneration: number;
}

export interface AccountSummaryAssets {
  stories: number;
  characters: number;
  worldbook: number;
  words: number;
}

export interface AccountSummaryResponse {
  user: AuthUser;
  quota: AccountSummaryQuota;
  profile: AccountSummaryProfile;
  assets: AccountSummaryAssets;
}
