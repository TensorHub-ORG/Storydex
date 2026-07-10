import { createApp } from "vue";
import { createPinia } from "pinia";
import App from "./App.vue";
import router from "./router";
import "@fontsource/material-symbols-rounded/400.css";
import "./assets/theme.css";
import { applyCachedThemeSnapshot } from "@/utils/appearance";
import { initializeIconFontState } from "@/utils/iconFont";

initializeIconFontState();
applyCachedThemeSnapshot();

const app = createApp(App);
app.use(createPinia());
app.use(router);
app.mount("#app");
