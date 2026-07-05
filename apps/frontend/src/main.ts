import { createApp } from "vue";
import { createPinia } from "pinia";
import App from "./App.vue";
import router from "./router";
import "@fontsource/material-symbols-rounded/400.css";
import "./assets/theme.css";
import { applyCachedThemeSnapshot } from "@/utils/appearance";

function markIconFontReady(): void {
	document.documentElement.classList.add("icon-font-ready");
}

function initializeIconFontState(): void {
	if (!("fonts" in document)) {
		markIconFontReady();
		return;
	}

	const fontFaceSet = document.fonts;
	const checkReady = (): void => {
		if (fontFaceSet.check('16px "Material Symbols Rounded"')) {
			markIconFontReady();
		}
	};

	void fontFaceSet.ready.then(checkReady);
	window.setTimeout(checkReady, 1200);
}

initializeIconFontState();
applyCachedThemeSnapshot();

const app = createApp(App);
app.use(createPinia());
app.use(router);
app.mount("#app");
