/* ASMR Downloader Web 管理端 - Vue 3 应用逻辑（浅色 asmr.one 风格改造） */
const { createApp } = Vue;

createApp({
  data() {
    return {
      view: "tasks",
      themeMode: "system", // light | dark | system
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
      // 作品库
      works: [],
      libraryLoading: false,
      searchQuery: "",
      sortBy: "latest",
      worksShown: 30,
      detail: null,
      detailRj: "",
      detailLoading: false,
      detailError: "",
      // 播放器
      currentAudio: "",
      audioIndex: -1,
      audioPlaying: false,
      audioError: false,
      currentTime: 0,
      duration: 0,
      volume: 1,
      muted: false,
      playMode: "order", // order | loop-all | loop-one
      playerCollapsed: false,
      playlistOpen: false,
      // 图片预览
      previewImage: "",
      imageIndex: -1,
      imageLoading: false,
      imageError: false,
      // 文本预览
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
    filteredWorks() {
      const q = this.searchQuery.trim().toLowerCase();
      let list = this.works.filter((w) => this.searchMatch(w, q));
      const s = this.sortBy;
      if (s === "title") {
        list = list.slice().sort((a, b) => (a.title || "").localeCompare(b.title || "", "zh"));
      } else if (s === "rj") {
        list = list.slice().sort((a, b) => this.rjNum(a.rj) - this.rjNum(b.rj));
      } else if (s === "duration") {
        list = list.slice().sort((a, b) => this.metaDur(b.meta) - this.metaDur(a.meta));
      } else if (s === "release") {
        list = list.slice().sort((a, b) => (this.dateText(b.meta) || "").localeCompare(this.dateText(a.meta) || ""));
      }
      // latest: 保持后端返回顺序（最新收录）
      return list;
    },
    visibleWorks() {
      return this.filteredWorks.slice(0, this.worksShown);
    },
    hasMoreAudio() {
      return this.visibleAudio.length < this.audioList.length;
    },
    hasMoreImage() {
      return this.visibleImage.length < this.imageList.length;
    },
    hasMoreWorks() {
      return this.visibleWorks.length < this.filteredWorks.length;
    },
    connText() {
      if (this.serverOk) return "服务已连接";
      return this.serverFail ? "服务连接失败，正在重试…" : "连接中…";
    },
    themeText() {
      return { light: "浅色", dark: "深色", system: "跟随系统" }[this.themeMode] || "跟随系统";
    },
    currentAudioName() {
      const f = this.audioList[this.audioIndex];
      return f ? (f.rel || f.name) : "";
    },
    detailTitle() {
      return (this.detail && this.detail.title) || "本地作品库";
    },
    playModeText() {
      return { order: "顺序", "loop-all": "列表循环", "loop-one": "单曲循环" }[this.playMode] || "顺序";
    },
  },
  watch: {
    volume(v) {
      const el = this.$refs.playerAudio;
      if (el) el.volume = v;
    },
    muted(v) {
      const el = this.$refs.playerAudio;
      if (el) el.muted = v;
    },
  },
  methods: {
    applyTheme() {
      let saved = null;
      try { saved = localStorage.getItem("asmr-theme"); } catch (e) {}
      const mode = (saved === "light" || saved === "dark") ? saved : "system";
      this.themeMode = mode;
      const dark = mode === "dark" ||
        (mode === "system" && window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches);
      document.documentElement.setAttribute("data-theme", dark ? "dark" : "light");
      document.documentElement.setAttribute("data-theme-mode", mode);
    },
    cycleTheme() {
      const next = { light: "dark", dark: "system", system: "light" }[this.themeMode] || "light";
      try { localStorage.setItem("asmr-theme", next); } catch (e) {}
      this.applyTheme();
      this.showToast("主题：已切换为「" + this.themeText + "」");
    },
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
      if (this.refreshBusy) return;
      this.refreshBusy = true;
      try {
        const data = await this.api("/api/tasks");
        const list = data.tasks || [];
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
        if (document.hidden) return;
        await this.refresh();
        this.scheduleRefresh();
      };
      this._refreshTimer = setTimeout(tick, 2000);
    },
    async createTasks() {
      if (this.submitting) return;
      let urls = this.urlsText.split("\n").map((s) => s.trim()).filter(Boolean);
      // 输入区为空时，自动读取剪贴板中的作品链接/RJ 号
      if (!urls.length && navigator.clipboard && navigator.clipboard.readText) {
        try {
          const text = await navigator.clipboard.readText();
          const parsed = this.parseClipboardText(text);
          if (parsed.length) {
            urls = parsed;
            this.urlsText = parsed.join("\n");
          }
        } catch (e) {
          // 剪贴板读取失败或无权限，保持 urls 为空
        }
      }
      if (!urls.length) return;
      const bad = urls.filter((u) => !/^(https?:\/\/\S+|RJ\d{5,})$/i.test(u));
      if (bad.length) {
        this.createError = "以下输入无法识别为作品地址或 RJ 号：" + bad.slice(0, 3).join("、") + (bad.length > 3 ? " 等" : "");
        return;
      }
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
    parseClipboardText(text) {
      if (!text) return [];
      const items = [];
      const seen = new Set();
      const reRj = /RJ\s*(\d{5,})/gi;
      let m;
      while ((m = reRj.exec(text)) !== null) {
        const rj = "RJ" + m[1];
        if (!seen.has(rj)) { seen.add(rj); items.push(rj); }
      }
      const reUrl = /https?:\/\/[^\s"'<>]+/gi;
      while ((m = reUrl.exec(text)) !== null) {
        const u = m[0].replace(/[),.;，。；）]+$/, "");
        const uRj = (u.match(/\bRJ\d{5,}/i) || [""])[0].toUpperCase();
        if (uRj && seen.has(uRj)) continue; // 链接内的 RJ 号已提取，跳过避免重复
        if (!seen.has(u)) { seen.add(u); items.push(u); }
      }
      return items;
    },
    showToast(msg, type = "info") {
      const id = ++this._toastId;
      this.toasts.push({ id, msg, type });
      setTimeout(() => {
        this.toasts = this.toasts.filter((t) => t.id !== id);
      }, 4000);
    },
    async retryTask(id) {
      if (this.retrying[id]) return;
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
      if (this.cancelling[id]) return;
      const t = this.tasks.find((x) => x.id === id);
      this.cancelling[id] = true;
      if (t) t.status = "cancelling";
      try {
        await this.api(`/api/tasks/${id}/cancel`, "POST", {});
        await this.refresh();
      } catch (e) {
        this.showToast(`取消失败: ${e.message}`, "error");
        if (t) this.refresh();
      } finally {
        delete this.cancelling[id];
      }
    },
    async deleteTask(id) {
      if (this.deleting[id]) return;
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
      const seq = ++this.detailReqSeq;
      this.detailLoading = true;
      this.detail = null;
      this.detailRj = rj;
      this.detailError = "";
      this.currentAudio = "";
      this.audioIndex = -1;
      this.audioPlaying = false;
      this.currentTime = 0;
      this.duration = 0;
      this.playerCollapsed = false;
      this.playlistOpen = false;
      this.imageIndex = -1;
      try {
        const d = await this.api(`/api/work/${encodeURIComponent(rj)}`);
        if (seq !== this.detailReqSeq) return;
        this.detail = d;
      } catch (e) {
        if (seq !== this.detailReqSeq) return;
        this.detailError = e.message;
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
      this.audioPage = 1;
      this.imagePage = 1;
      this.playlistOpen = false;
      this.imageIndex = -1;
    },
    workFileUrl(rel) {
      return "/files/" + rel.split("/").map(encodeURIComponent).join("/");
    },
    // ---- 作品库搜索 / 排序辅助 ----
    searchMatch(w, q) {
      if (!q) return true;
      const meta = w.meta || {};
      const hay = [
        w.title || "",
        w.rj || "",
        this.circleText(meta),
        this.vaText(meta),
        this.tagList(meta).join(" "),
      ].join(" ").toLowerCase();
      return q.split(/\s+/).every((kw) => hay.includes(kw));
    },
    rjNum(rj) {
      const m = /RJ(\d+)/i.exec(rj || "");
      return m ? Number(m[1]) : 0;
    },
    metaDur(meta) {
      const d = Number((meta && (meta.duration || meta.duration_sec)) || 0);
      return isFinite(d) ? d : 0;
    },
    // ---- 播放器 ----
    playAudio(f, index) {
      if (!f) return;
      this.audioIndex = index;
      this.audioError = false;
      this.currentTime = 0;
      this.duration = 0;
      this.currentAudio = this.workFileUrl(f.path);
      this.playerCollapsed = false;
      this.playlistOpen = false;
      this.$nextTick(() => {
        const el = this.$refs.playerAudio;
        if (el) el.play().catch(() => {});
      });
    },
    toggleTrack(f, i) {
      if (i === this.audioIndex && this.audioPlaying) {
        const el = this.$refs.playerAudio;
        if (el) el.pause();
      } else {
        this.playAudio(f, i);
      }
    },
    togglePlay() {
      const el = this.$refs.playerAudio;
      if (!el) return;
      if (el.paused) el.play().catch(() => {});
      else el.pause();
    },
    prevAudio() {
      if (this.audioList.length === 0) return;
      let pi = this.audioIndex - 1;
      if (pi < 0) pi = this.audioList.length - 1;
      this.playAudio(this.audioList[pi], pi);
    },
    nextAudio() {
      if (this.audioList.length === 0) return;
      let ni = this.audioIndex + 1;
      if (ni >= this.audioList.length) ni = 0;
      this.playAudio(this.audioList[ni], ni);
    },
    cyclePlayMode() {
      const modes = ["order", "loop-all", "loop-one"];
      const idx = modes.indexOf(this.playMode);
      this.playMode = modes[(idx + 1) % modes.length];
      this.showToast("播放模式: " + this.playModeText);
    },
    onAudioEnded() {
      if (this.playMode === "loop-one") {
        const el = this.$refs.playerAudio;
        if (el) {
          el.currentTime = 0;
          el.play().catch(() => {});
        }
        return;
      }
      if (this.playMode === "loop-all") {
        if (this.audioIndex >= this.audioList.length - 1) {
          this.playAudio(this.audioList[0], 0);
        } else {
          this.nextAudio();
        }
        return;
      }
      // order：末曲播放完毕自动停止
      if (this.audioIndex >= this.audioList.length - 1) {
        this.currentAudio = "";
        this.audioIndex = -1;
        this.audioPlaying = false;
        this.currentTime = 0;
        this.duration = 0;
      } else {
        this.nextAudio();
      }
    },
    onAudioError() {
      if (!this.currentAudio) return;
      this.audioError = true;
      this.showToast(`音频加载失败，请确认文件存在或格式受支持`, "error");
    },
    onTimeUpdate(e) {
      this.currentTime = e.target.currentTime || 0;
    },
    onLoadedMeta(e) {
      this.duration = e.target.duration || 0;
    },
    seekAudio(e) {
      const el = this.$refs.playerAudio;
      if (!el) return;
      const v = Number(e.target.value);
      if (isFinite(v)) {
        el.currentTime = v;
        this.currentTime = v;
      }
    },
    setVolume(e) {
      const v = Number(e.target.value);
      if (!isFinite(v)) return;
      this.volume = Math.min(1, Math.max(0, v));
      this.muted = false;
    },
    toggleMute() {
      this.muted = !this.muted;
    },
    // ---- 图片预览 ----
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
    onThumbError(e) {
      const el = e.target;
      if (!el) return;
      const box = el.closest(".image-thumb");
      if (box) box.classList.add("thumb-error");
      el.style.display = "none";
    },
    prevImage() {
      if (this.imageIndex > 0) this.openImage(this.imageList[this.imageIndex - 1], this.imageIndex - 1);
    },
    nextImage() {
      if (this.imageIndex < this.imageList.length - 1) this.openImage(this.imageList[this.imageIndex + 1], this.imageIndex + 1);
    },
    closeImage() {
      this.previewImage = "";
      this.imageLoading = false;
      this.imageError = false;
    },
    // ---- 文本预览 ----
    async openText(f) {
      this.textLoading = true;
      this.textTruncated = false;
      try {
        const resp = await fetch(this.workFileUrl(f.path));
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        this.previewTextName = f.rel || f.name;
        let text = await resp.text();
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
    // ---- 其他 ----
    progressPct(t) {
      const p = Number(t.progress);
      if (!isFinite(p)) return 0;
      return Math.min(100, Math.max(0, Math.round(p)));
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
    fmtDur(secs) {
      if (secs === undefined || secs === null || isNaN(secs) || secs < 0) return "00:00";
      const total = Math.floor(secs);
      const h = Math.floor(total / 3600);
      const m = Math.floor((total % 3600) / 60);
      const s = total % 60;
      const mm = String(m).padStart(2, "0");
      const ss = String(s).padStart(2, "0");
      return h > 0 ? `${h}:${mm}:${ss}` : `${mm}:${ss}`;
    },
    fmtTime(s) {
      if (!s) return "";
      const d = new Date(s);
      if (isNaN(d.getTime())) return String(s);
      const p = (n) => String(n).padStart(2, "0");
      return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
    },
    handleKeydown(e) {
      if (e.key === "Escape") {
        if (this.playlistOpen) { this.playlistOpen = false; return; }
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
      const va = (meta && (meta.vas || meta.va)) || [];
      return va
        .map((v) => (typeof v === "string" ? v : (v && (v.name || v.va || v.vas)) || ""))
        .filter(Boolean)
        .join("、");
    },
    durationText(meta) {
      const secs = meta && (meta.duration || meta.duration_sec);
      if (secs === undefined || secs === null || isNaN(Number(secs)) || Number(secs) < 0) return "";
      return this.fmtDur(Number(secs));
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
    this.applyTheme();
    this._themeMq = window.matchMedia ? window.matchMedia("(prefers-color-scheme: dark)") : null;
    this._onThemeChange = () => { if (this.themeMode === "system") this.applyTheme(); };
    if (this._themeMq && this._themeMq.addEventListener) this._themeMq.addEventListener("change", this._onThemeChange);
    this.refresh();
    this.scheduleRefresh();
    window.addEventListener("keydown", this.handleKeydown);
    document.addEventListener("visibilitychange", this.onVisibilityChange);
  },
  beforeUnmount() {
    clearTimeout(this._refreshTimer);
    if (this._themeMq && this._themeMq.removeEventListener) this._themeMq.removeEventListener("change", this._onThemeChange);
    window.removeEventListener("keydown", this.handleKeydown);
    document.removeEventListener("visibilitychange", this.onVisibilityChange);
  },
}).mount("#app");
