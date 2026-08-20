import { createApp } from "vue";
import { createPinia } from "pinia";
import App from "./App.vue";
import router from "./router";
import "@fontsource/material-symbols-rounded/400.css";
import "./assets/theme.css";
import { applyCachedThemeSnapshot } from "@/utils/appearance";
import { initializeIconFontState } from "@/utils/iconFont";
import { installTauriDesktopBridge } from "@/desktop/tauriDesktop";
import { installTauriUpdaterBridge } from "@/desktop/tauriUpdater";

async function bootstrap(): Promise<void> {
  initializeIconFontState();
  applyCachedThemeSnapshot();
  await installTauriDesktopBridge();
  await installTauriUpdaterBridge();

  const app = createApp(App);
  app.use(createPinia());
  app.use(router);
  await router.isReady();
  app.mount("#app");
}

void bootstrap();
