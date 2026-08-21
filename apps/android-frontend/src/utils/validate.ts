/**
 * 值域收敛的三个原语。
 *
 * 各 store 原先只在**读盘**时校验枚举（清单内联在 readState / runInitialize 里），
 * 写入路径一律不校验。于是同一个字段有两套标准：从磁盘来的一定合法，从调用方来的
 * 可以是任何东西。非法枚举不会抛错，只会让下游所有 `=== '某档'` 判断落到 else，
 * 表现成"设置显示是对的但不起作用"——这类问题很难从现象反推到写入点。
 *
 * 收在这里，读与写共用同一份规则；后续 Agent 的配置工具是第三个调用方，
 * 不必再抄一遍。
 */

/** 取合法枚举值，非法时回退到 fallback。 */
export function pickEnum<T>(value: unknown, allowed: readonly T[], fallback: T): T {
  return allowed.includes(value as T) ? value as T : fallback
}

/** 取布尔值。只认真正的 boolean——'false' / 0 / null 这些一律走 fallback，
 *  避免把字符串 'false' 当成真。 */
export function pickBool(value: unknown, fallback: boolean): boolean {
  return typeof value === 'boolean' ? value : fallback
}

/** 取整数并夹到 [min, max]；非数字回退到 fallback。 */
export function clampInt(value: unknown, min: number, max: number, fallback: number): number {
  const parsed = Number(value)
  return Number.isFinite(parsed) ? Math.min(max, Math.max(min, Math.round(parsed))) : fallback
}

/** 取非空字符串（首尾空白已去掉）；空串或非字符串回退到 fallback。 */
export function pickText(value: unknown, fallback: string): string {
  const next = typeof value === 'string' ? value.trim() : ''
  return next || fallback
}
