import { createRouter, createWebHashHistory, createWebHistory } from "vue-router";
import FilePreviewView from "@/views/FilePreviewView.vue";
import WorkbenchView from "@/views/WorkbenchView.vue";

function createRouterHistory() {
  if (typeof window !== "undefined" && window.location.protocol === "file:") {
    return createWebHashHistory();
  }
  return createWebHistory();
}

const router = createRouter({
  history: createRouterHistory(),
  routes: [
    {
      path: "/",
      name: "workbench",
      component: WorkbenchView
    },
    {
      path: "/preview",
      name: "preview-file",
      component: FilePreviewView
    }
  ]
});

export default router;
