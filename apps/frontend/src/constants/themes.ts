export type ThemeCode = "default" | "white" | "snow" | "book" | "dark";

export interface ThemeOption {
  code: ThemeCode;
  label: string;
  description: string;
  preview: string;
}

export const themeOptions: ThemeOption[] = [
  {
    code: "default",
    label: "现代浅色",
    description: "保留 Storydex 默认的清爽浅色工作台，适合长时间编辑。",
    preview: "linear-gradient(135deg, #f7f8fb 0%, #dce6ff 100%)"
  },
  {
    code: "white",
    label: "纯白工作台",
    description: "接近纯白画布的克制界面，减少背景干扰并保留暖橙强调色。",
    preview: "linear-gradient(135deg, #ffffff 0%, #f3f4f6 100%)"
  },
  {
    code: "snow",
    label: "雪纸蓝白",
    description: "更偏冷调的蓝白配色，层次更接近编辑器工作台。",
    preview: "linear-gradient(135deg, #edf5ff 0%, #bfd9ff 100%)"
  },
  {
    code: "book",
    label: "沉浸书卷",
    description: "暖纸色阅读氛围，更适合世界观整理和正文创作。",
    preview: "linear-gradient(135deg, #f3e8cf 0%, #c89f6b 100%)"
  },
  {
    code: "dark",
    label: "纯净暗色",
    description: "低亮度深色界面，延续 VS Code 风格的沉浸感。",
    preview: "linear-gradient(135deg, #2d3547 0%, #121722 100%)"
  }
];

export function isThemeCode(value: unknown): value is ThemeCode {
  return value === "default" || value === "white" || value === "snow" || value === "book" || value === "dark";
}
