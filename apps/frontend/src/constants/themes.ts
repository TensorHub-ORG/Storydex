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
    description: "灰阶分层的克制界面，蓝色强调，边界靠实色描边区分。",
    preview: "linear-gradient(135deg, #ffffff 0%, #f6f8fa 55%, #005fb8 100%)"
  },
  {
    code: "default",
    label: "现代浅色",
    description: "Storydex 默认浅色工作台，暖橙强调，适合长时间编辑。",
    preview: "linear-gradient(135deg, #ffffff 0%, #f4f6fa 55%, #d06d3b 100%)"
  },
  {
    code: "snow",
    label: "雪纸蓝白",
    description: "更偏冷调的蓝白配色，层次更接近编辑器工作台。",
    preview: "linear-gradient(135deg, #ffffff 0%, #f2f7ff 55%, #4e79ef 100%)"
  },
  {
    code: "book",
    label: "沉浸书卷",
    description: "暖纸色阅读氛围，更适合世界观整理和正文创作。",
    preview: "linear-gradient(135deg, #fffaf3 0%, #f6ede1 55%, #b56033 100%)"
  },
  {
    code: "dark",
    label: "纯净暗色",
    description: "低亮度深色界面，暖橙强调，长时间夜间写作友好。",
    preview: "linear-gradient(135deg, #1a1f27 0%, #14181f 55%, #e2886e 100%)"
  }
];

export function isThemeCode(value: unknown): value is ThemeCode {
  return value === "default" || value === "white" || value === "snow" || value === "book" || value === "dark";
}
