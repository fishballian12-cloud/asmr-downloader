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
      serverFail: false,
      works: [],
      libraryLoading: false,
      detail: null,
      detailRj: "",
      detailLoading: false,
      detailError: "",
      currentAudio: "",
      audioIndex: -1,
      audioError: false,
      previewImage: "",
      imageIndex: -1,
      imageLoading: false,
      imageError: false,
      previewText: "",
      previewTextName: "",
      textLoading: false,
      textTruncated: false,
      toasts: [],
      _toastId: 0,
      retrying: {},
      deleting: {},
      cancelling: {},
      retryFailedBusy: false,
      clearDoneBusy: false,
      refreshBusy: false,
      detailReqSeq: 0,
      audioPage: 1,
      imagePage: 1,
      worksShown: 30,
      pageSize: 40,
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
      // 按「路径+文件名」排序（rel 为作品目录内相对路径），再按类型分组展示
      const files = ((this.detail && this.detail.files) || []).slice().sort((a, b) =>
        (a.rel || a.path).localeCompare(b.rel || b.path, "zh", { numeric: true, sensitivity: "base" })
      );
      files.forEach((f) => {
        if (groups[f.type]) groups[f.type].push(f);
      });
      return groups;
    },
    audioList() {
      return this.detailGroups.audio;
    },
    imageList() {
      return this.detailGroups.image;
    },
    visibleAudio() {
      return this.audioList.slice(0, this.audioPage * this.pageSize);
    },
    visibleImage() {
      return this.imageList.slice(0, this.imagePage * this.pageSize);
    },
    visibleWorks() {
      return this.works.slice(0, this.worksShown);
    },
    hasMoreAudio() {
      return this.visibleAudio.length < this.audioList.length;
    },
    hasMoreImage() {
      return this.visibleImage.length < this.imageList.length;
    },
    hasMoreWorks() {
      return this.visibleWorks.length < this.works.length;
    },
    connText() {
      if (this.serverOk) return "服务已连接";
      return this.serverFail ? "服务连接失败，正在重试…" : "连接中…";
    },
    currentAudioName() {
      const f = this.audioList[this.audioIndex];
      return f ? (f.rel || f.name) : "";
    },
  },
  methods: {
    async api(path, method = "GET", body, timeout = 20000) {
      const ctrl = new AbortController();
      const timer = setTimeout(() => ctrl.abort(), timeout);
      let resp;
      try {
        resp = await fetch(path, {
          method,
          headers: body ? { "Content-Type": "application/json" } : undefined,
          body: body ? JSON.stringify(body) : undefined,
          signal: ctrl.signal,
        });
      } catch (e) {
        clearTimeout(timer);
        if (e.name === "AbortError") throw new Error("请求超时，请检查服务状态");
        throw e;
      }
      clearTimeout(timer);
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok) throw new Error(data.error || `HTTP ${resp.status}`);
      return data;
    },
    async refresh() {
      if (this.refreshBusy) return; // 防重入：上一次轮询未完成时跳过本次
      this.refreshBusy = true;
      try {
        const data = await this.api("/api/tasks");
        const list = data.tasks || [];
        // 取消竞态合并：本地标记 cancelling 的任务，若服务端仍为 running/queued，保持 cancelling 展示
        this.tasks = list.map((t) =>
          this.cancelling[t.id] && ["queued", "running"].includes(t.status)
            ? { ...t, status: "cancelling" }
            : t
        );
        this.serverOk = true;
        this.serverFail = false;
      } catch (e) {
        this.serverOk = false;
        this.serverFail = true;
      } finally {
        this.refreshBusy = false;
      }
    },
    scheduleRefresh() {
      if (this._refreshTimer) return;
      const tick = async () => {
        this._refreshTimer = null;
        if (document.hidden) return; // 页面隐藏暂停轮询
        await this.refresh();
        this.scheduleRefresh();
      };
      this._refreshTimer = setTimeout(tick, 2000);
    },
    async createTasks() {
      if (this.submitting) return;
      const urls = this.urlsText.split("\n").map((s) => s.trim()).filter(Boolean);
      if (!urls.length) return;
      // URL 前端预校验：仅接受 http(s):// 链接或 RJ 编号
      const bad = urls.filter((u) => !/^(https?:\/\/\S+|RJ\d{5,})$/i.test(u));
      if (bad.length) {
        this.createError = "以下输入无法识别为作品地址或 RJ 号：" + bad.slice(0, 3).join("、") + (bad.length > 3 ? " 等" : "");
        return;
      }
      // 数值 clamp：防越界
      this.options.workers = Math.min(16, Math.max(1, Math.round(this.options.workers) || 4));
      this.options.retries = Math.min(10, Math.max(0, Math.round(this.options.retries) || 3));
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
    showToast(msg, type = "info") {
      const id = ++this._toastId;
      this.toasts.push({ id, msg, type });
      setTimeout(() => {
        this.toasts = this.toasts.filter((t) => t.id !== id);
      }, 4000);
    },
    async retryTask(id) {
      if (this.retrying[id]) return; // 防重复提交
      this.retrying[id] = true;
      try {
        await this.api(`/api/tasks/${id}/retry`, "POST", {});
        await this.refresh();
      } catch (e) {
        this.showToast(`重试失败: ${e.message}`, "error");
      } finally {
        this.retrying[id] = false;
      }
    },
    async cancelTask(id) {
      if (this.cancelling[id]) return; // 防重复提交
      const t = this.tasks.find((x) => x.id === id);
      this.cancelling[id] = true;
      if (t) t.status = "cancelling"; // 立即反馈"正在取消…"，并防止轮询覆盖
      try {
        await this.api(`/api/tasks/${id}/cancel`, "POST", {});
        await this.refresh();
      } catch (e) {
        this.showToast(`取消失败: ${e.message}`, "error");
        if (t) this.refresh(); // 失败后复位真实状态
      } finally {
        delete this.cancelling[id];
      }
    },
    async deleteTask(id) {
      if (this.deleting[id]) return; // 防重复提交
      if (!confirm(`确定要删除该任务吗？此操作不可恢复。`)) return;
      this.deleting[id] = true;
      try {
        await this.api(`/api/tasks/${id}`, "DELETE");
        await this.refresh();
      } catch (e) {
        this.showToast(`删除失败: ${e.message}`, "error");
      } finally {
        this.deleting[id] = false;
      }
    },
    async retryFailed() {
      if (this.retryFailedBusy) return;
      const n = this.failedCount;
      if (!n) return;
      this.retryFailedBusy = true;
      try {
        await this.api("/api/tasks/retry-failed", "POST", {});
        await this.refresh();
      } catch (e) {
        this.showToast(`批量重试失败: ${e.message}`, "error");
      } finally {
        this.retryFailedBusy = false;
      }
    },
    async clearDone() {
      if (this.clearDoneBusy) return;
      const n = this.doneCount;
      if (!n) return;
      if (!confirm(`确定要删除全部 ${n} 个已完成任务吗？此操作不可恢复。`)) return;
      this.clearDoneBusy = true;
      try {
        const data = await this.api("/api/tasks/clear-done", "DELETE");
        await this.refresh();
        this.showToast(`已删除 ${data.deleted || 0} 个已完成任务`);
      } catch (e) {
        this.showToast(`批量删除失败: ${e.message}`, "error");
      } finally {
        this.clearDoneBusy = false;
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
      if (this.libraryLoading) return;
      this.libraryLoading = true;
      try {
        const data = await this.api("/api/library");
        this.works = data.works || [];
        this.worksShown = 30;
      } catch (e) {
        this.works = [];
        this.showToast(`作品库加载失败: ${e.message}`, "error");
      } finally {
        this.libraryLoading = false;
      }
    },
    async openWork(rj) {
      const seq = ++this.detailReqSeq; // 竞态序号：旧响应不得覆盖新响应
      this.detailLoading = true;
      this.detail = null;
      this.detailRj = rj;
      this.detailError = "";
      this.currentAudio = "";
      this.audioIndex = -1;
      this.imageIndex = -1;
      try {
        const d = await this.api(`/api/work/${encodeURIComponent(rj)}`);
        if (seq !== this.detailReqSeq) return; // 已被更新的请求取代
        this.detail = d;
      } catch (e) {
        if (seq !== this.detailReqSeq) return;
        this.detailError = e.message; // 失败兜底：展示错误与重试
      } finally {
        if (seq === this.detailReqSeq) this.detailLoading = false;
      }
    },
    retryOpenWork() {
      if (this.detailRj) this.openWork(this.detailRj);
    },
    backToLibrary() {
      this.detail = null;
      this.detailRj = "";
      this.detailError = "";
      this.currentAudio = "";
      this.audioIndex = -1;
      this.imageIndex = -1;
    },
    workFileUrl(rel) {
      return "/files/" + rel.split("/").map(encodeURIComponent).join("/");
    },
    playAudio(f, index) {
      this.audioIndex = index;
      this.audioError = false;
      this.currentAudio = this.workFileUrl(f.path);
    },
    onAudioEnded() {
      if (this.audioIndex >= this.audioList.length - 1) {
        // 末曲播放完毕自动停止，不循环
        this.currentAudio = "";
        this.audioIndex = -1;
      } else {
        this.nextAudio();
      }
    },
    onAudioError() {
      if (!this.currentAudio) return;
      this.audioError = true;
      this.showToast(`音频加载失败，请确认文件存在或格式受支持`, "error");
    },
    prevAudio() {
      if (this.audioIndex > 0) this.playAudio(this.audioList[this.audioIndex - 1], this.audioIndex - 1);
    },
    nextAudio() {
      if (this.audioIndex < this.audioList.length - 1) this.playAudio(this.audioList[this.audioIndex + 1], this.audioIndex + 1);
    },
    openImage(f, index) {
      this.imageIndex = index;
      this.imageLoading = true;
      this.imageError = false;
      this.previewImage = this.workFileUrl(f.path);
    },
    onImageLoaded() {
      this.imageLoading = false;
      this.imageError = false;
    },
    onImageError() {
      this.imageLoading = false;
      this.imageError = true;
      this.showToast(`图片加载失败，文件可能缺失`, "error");
    },
    prevImage() {
      if (this.imageIndex > 0) this.openImage(this.imageList[this.imageIndex - 1], this.imageIndex - 1);
    },
    nextImage() {
      if (this.imageIndex < this.imageList.length - 1) this.openImage(this.imageList[this.imageIndex + 1], this.imageIndex + 1);
    },
    async openText(f) {
      this.textLoading = true;
      this.textTruncated = false;
      try {
        const resp = await fetch(this.workFileUrl(f.path));
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        this.previewTextName = f.rel || f.name;
        let text = await resp.text();
        // 大文本保护：超过 20 万字符截断展示，避免页面卡死
        if (text.length > 200000) {
          text = text.slice(0, 200000);
          this.textTruncated = true;
        }
        this.previewText = text;
      } catch (e) {
        this.previewText = "";
        this.previewTextName = f.rel || f.name;
        this.showToast(`读取文本失败: ${e.message}`, "error");
      } finally {
        this.textLoading = false;
      }
    },
    closeText() {
      this.previewText = "";
      this.textTruncated = false;
    },
    closeImage() {
      this.previewImage = "";
      this.imageLoading = false;
      this.imageError = false;
    },
    progressPct(t) {
      const p = Number(t.progress);
      if (!isFinite(p)) return 0;
      return Math.min(100, Math.max(0, Math.round(p))); // clamp 0-100
    },
    loadMoreAudio() {
      this.audioPage++;
    },
    loadMoreImage() {
      this.imagePage++;
    },
    loadMoreWorks() {
      this.worksShown += 30;
    },
    fmtTime(s) {
      if (!s) return "";
      const d = new Date(s);
      if (isNaN(d.getTime())) return String(s);
      const p = (n) => String(n).padStart(2, "0");
      return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
    },
    handleKeydown(e) {
      // ESC 关闭弹层
      if (e.key === "Escape") {
        if (this.previewText) { this.closeText(); return; }
        if (this.previewImage) { this.closeImage(); return; }
      }
      // 图片预览：左右方向键切换
      if (this.previewImage) {
        if (e.key === "ArrowLeft") { e.preventDefault(); this.prevImage(); return; }
        if (e.key === "ArrowRight") { e.preventDefault(); this.nextImage(); return; }
      }
      // 空格：播放/暂停当前音频
      if (e.code === "Space" && this.currentAudio) {
        const el = this.$refs.playerAudio;
        if (el && !el.paused) { e.preventDefault(); el.pause(); }
        else if (el) { e.preventDefault(); el.play().catch(() => {}); }
      }
    },
    onVisibilityChange() {
      if (document.hidden) {
        clearTimeout(this._refreshTimer);
        this._refreshTimer = null;
      } else {
        this.refresh();
        this.scheduleRefresh();
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
    this._toastId = 0;
    this.refresh();
    this.scheduleRefresh();
    window.addEventListener("keydown", this.handleKeydown);
    document.addEventListener("visibilitychange", this.onVisibilityChange);
  },
  beforeUnmount() {
    clearTimeout(this._refreshTimer);
    window.removeEventListener("keydown", this.handleKeydown);
    document.removeEventListener("visibilitychange", this.onVisibilityChange);
  },
}).mount("#app");
