/* ASMR Downloader Web 管理端 - Vue 3 应用逻辑 */
const { createApp } = Vue;

createApp({
  data() {
    return {
      urlsText: "",
      options: {
        workers: 4,
        retries: 3,
        output: "",
      },
      submitting: false,
      createError: "",
      tasks: [],
      serverOk: false,
    };
  },
  computed: {
    activeTasks() {
      return this.tasks.filter((t) => ["queued", "running"].includes(t.status));
    },
    historyTasks() {
      return this.tasks.filter((t) => ["done", "failed", "cancelled"].includes(t.status));
    },
  },
  methods: {
    async api(path, method = "GET", body) {
      const resp = await fetch(path, {
        method,
        headers: body ? { "Content-Type": "application/json" } : undefined,
        body: body ? JSON.stringify(body) : undefined,
      });
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok) throw new Error(data.error || `HTTP ${resp.status}`);
      return data;
    },
    async refresh() {
      try {
        const data = await this.api("/api/tasks");
        this.tasks = data.tasks || [];
        this.serverOk = true;
      } catch (e) {
        this.serverOk = false;
      }
    },
    async createTasks() {
      const urls = this.urlsText.split("\n").map((s) => s.trim()).filter(Boolean);
      if (!urls.length) return;
      this.submitting = true;
      this.createError = "";
      try {
        await this.api("/api/tasks", "POST", { urls, options: this.options });
        this.urlsText = "";
        await this.refresh();
      } catch (e) {
        this.createError = e.message;
      } finally {
        this.submitting = false;
      }
    },
    async retryTask(id) {
      try {
        await this.api(`/api/tasks/${id}/retry`, "POST", {});
        await this.refresh();
      } catch (e) {
        alert(`重试失败: ${e.message}`);
      }
    },
    async cancelTask(id) {
      try {
        await this.api(`/api/tasks/${id}/cancel`, "POST", {});
        await this.refresh();
      } catch (e) {
        alert(`取消失败: ${e.message}`);
      }
    },
    statusText(s) {
      return { queued: "排队中", running: "下载中", done: "已完成", failed: "失败", cancelled: "已取消" }[s] || s;
    },
    fmtSpeed(kbps) {
      if (!kbps) return "-";
      if (kbps >= 1024) return (kbps / 1024).toFixed(1) + " MB/s";
      return kbps.toFixed(0) + " KB/s";
    },
    shortErr(e) {
      return (e || "").length > 60 ? e.slice(0, 60) + "…" : e;
    },
  },
  mounted() {
    this.refresh();
    setInterval(this.refresh, 2000);
  },
}).mount("#app");
