/* ASMR Downloader Web 管理端 - Vue 3 应用逻辑 */
const { createApp } = Vue;

createApp({
  data() {
    return {
      view: "tasks",
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
      works: [],
      libraryLoading: false,
      detail: null,
      detailLoading: false,
      currentAudio: "",
      previewImage: "",
      previewText: "",
      previewTextName: "",
    };
  },
  computed: {
    activeTasks() {
      return this.tasks.filter((t) => ["queued", "running", "cancelling"].includes(t.status));
    },
    historyTasks() {
      return this.tasks.filter((t) => ["done", "failed", "cancelled"].includes(t.status));
    },
    doneCount() {
      return this.tasks.filter((t) => t.status === "done").length;
    },
    failedCount() {
      return this.tasks.filter((t) => t.status === "failed").length;
    },
    detailGroups() {
      const groups = { audio: [], image: [], text: [], other: [] };
      ((this.detail && this.detail.files) || []).forEach((f) => {
        if (groups[f.type]) groups[f.type].push(f);
      });
      return groups;
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
      const t = this.tasks.find((x) => x.id === id);
      if (t) t.status = "cancelling"; // 立即反馈"正在取消…"，无需等服务端响应
      try {
        await this.api(`/api/tasks/${id}/cancel`, "POST", {});
        await this.refresh();
      } catch (e) {
        alert(`取消失败: ${e.message}`);
        if (t) this.refresh();
      }
    },
    async deleteTask(id) {
      try {
        await this.api(`/api/tasks/${id}`, "DELETE");
        await this.refresh();
      } catch (e) {
        alert(`删除失败: ${e.message}`);
      }
    },
    async retryFailed() {
      const n = this.failedCount;
      if (!n) return;
      try {
        await this.api("/api/tasks/retry-failed", "POST", {});
        await this.refresh();
      } catch (e) {
        alert(`批量重试失败: ${e.message}`);
      }
    },
    async clearDone() {
      const n = this.doneCount;
      if (!n) return;
      if (!confirm(`确定要删除全部 ${n} 个已完成任务吗？此操作不可恢复。`)) return;
      try {
        const data = await this.api("/api/tasks/clear-done", "DELETE");
        await this.refresh();
        alert(`已删除 ${data.deleted || 0} 个已完成任务`);
      } catch (e) {
        alert(`批量删除失败: ${e.message}`);
      }
    },
    statusText(s) {
      return { queued: "排队中", running: "下载中", cancelling: "正在取消…", done: "已完成", failed: "失败", cancelled: "已取消" }[s] || s;
    },
    fmtSpeed(kbps) {
      if (!kbps) return "-";
      if (kbps >= 1024) return (kbps / 1024).toFixed(1) + " MB/s";
      return kbps.toFixed(0) + " KB/s";
    },
    shortErr(e) {
      return (e || "").length > 60 ? e.slice(0, 60) + "…" : e;
    },
    async switchView(v) {
      this.view = v;
      if (v === "library") await this.loadLibrary();
    },
    async loadLibrary() {
      this.libraryLoading = true;
      try {
        const data = await this.api("/api/library");
        this.works = data.works || [];
      } catch (e) {
        this.works = [];
        alert(`作品库加载失败: ${e.message}`);
      } finally {
        this.libraryLoading = false;
      }
    },
    async openWork(rj) {
      this.detailLoading = true;
      this.detail = null;
      this.currentAudio = "";
      try {
        this.detail = await this.api(`/api/work/${encodeURIComponent(rj)}`);
      } catch (e) {
        alert(`加载作品详情失败: ${e.message}`);
      } finally {
        this.detailLoading = false;
      }
    },
    backToLibrary() {
      this.detail = null;
      this.currentAudio = "";
    },
    workFileUrl(rel) {
      return "/files/" + rel.split("/").map(encodeURIComponent).join("/");
    },
    playAudio(f) {
      this.currentAudio = this.workFileUrl(f.path);
    },
    openImage(f) {
      this.previewImage = this.workFileUrl(f.path);
    },
    async openText(f) {
      try {
        const resp = await fetch(this.workFileUrl(f.path));
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        this.previewTextName = f.name;
        this.previewText = await resp.text();
      } catch (e) {
        alert(`读取文本失败: ${e.message}`);
      }
    },
    tagList(meta) {
      const tags = (meta && meta.tags) || [];
      return tags
        .map((t) => (typeof t === "string" ? t : (t && (t.name || t.tag || t.value)) || ""))
        .filter(Boolean);
    },
    vaText(meta) {
      // meta.vas 是声优字段（数组，元素可能是 dict 含 name，或字符串）；va 为历史兼容字段
      const va = (meta && (meta.vas || meta.va)) || [];
      return va
        .map((v) => (typeof v === "string" ? v : (v && (v.name || v.va || v.vas)) || ""))
        .filter(Boolean)
        .join("、");
    },
    durationText(meta) {
      const secs = meta && (meta.duration || meta.duration_sec);
      if (secs === undefined || secs === null || isNaN(Number(secs)) || Number(secs) < 0) return "";
      const total = Math.round(Number(secs));
      const h = Math.floor(total / 3600);
      const m = Math.floor((total % 3600) / 60);
      const s = total % 60;
      const mm = String(m).padStart(2, "0");
      const ss = String(s).padStart(2, "0");
      return h > 0 ? `${h}:${mm}:${ss}` : `${mm}:${ss}`;
    },
    circleText(meta) {
      const c = meta && meta.circle;
      if (!c) return "";
      return typeof c === "string" ? c : (c.name || c.circle || "");
    },
    dateText(meta) {
      return (meta && (meta.release_date || meta.releaseDate)) || "";
    },
    descText(meta) {
      return (meta && (meta.introduction || meta.description || meta.intro)) || "";
    },
    formatSize(bytes) {
      if (bytes === undefined || bytes === null) return "";
      if (bytes < 1024) return bytes + " B";
      if (bytes < 1048576) return (bytes / 1024).toFixed(1) + " KB";
      if (bytes < 1073741824) return (bytes / 1048576).toFixed(1) + " MB";
      return (bytes / 1073741824).toFixed(2) + " GB";
    },
  },
  mounted() {
    this.refresh();
    setInterval(this.refresh, 2000);
  },
}).mount("#app");
