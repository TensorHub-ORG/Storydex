export type ThemeCode = "default" | "white" | "snow" | "book" | "dark";

export interface ThemeOption {
  code: ThemeCode;
  label: string;
  description: string;
  preview: string;
}

export const themeOptions: ThemeOption[] = [
  {
    code: "white",
    label: "纯白工作台",
    description: "中性灰画布托起纯白卡片，块与块边界清楚，蓝色强调。",
    preview: "linear-gradient(135deg, #ffffff 0%, #ebedf0 55%, #0b63c4 100%)"
  },
  {
    code: "default",
    label: "现代浅色",
    description: "暖灰画布与白色卡片，暖橙强调，适合长时间编辑。",
    preview: "linear-gradient(135deg, #ffffff 0%, #efede8 55%, #c96a3a 100%)"
  },
  {
    code: "snow",
    label: "雪纸蓝白",
    description: "冷调蓝灰画布与白色卡片，清爽的蓝色强调。",
    preview: "linear-gradient(135deg, #ffffff 0%, #e9eef6 55%, #3f6fe6 100%)"
  },
  {
    code: "book",
    label: "沉浸书卷",
    description: "暖纸色阅读氛围，更适合世界观整理和正文创作。",
    preview: "linear-gradient(135deg, #fffbf5 0%, #eee4d5 55%, #b05c30 100%)"
  },
  {
    code: "dark",
    label: "纯净暗色",
    description: "近黑画布与略亮的卡片，暖橙强调，夜间写作友好。",
    preview: "linear-gradient(135deg, #1c1d20 0%, #131416 55%, #e08a6c 100%)"
  }
];

export function isThemeCode(value: unknown): value is ThemeCode {
  return value === "default" || value === "white" || value === "snow" || value === "book" || value === "dark";
}
