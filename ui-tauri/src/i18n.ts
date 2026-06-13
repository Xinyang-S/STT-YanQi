export const product = {
  zhName: "言栖",
  enName: "Vernest",
  displayName: "言栖 Vernest",
  version: "0.6.7",
  copyright: "Copyright © 2026 孙欣阳. All rights reserved.",
};

export const locales = {
  zh: {
    ready: "待命",
    recording: "正在录音",
    paused: "已暂停",
    settings: "设置",
    appearance: "外观",
  },
  en: {
    ready: "Ready",
    recording: "Recording",
    paused: "Paused",
    settings: "Settings",
    appearance: "Appearance",
  },
} as const;

export type LocaleCode = keyof typeof locales;
