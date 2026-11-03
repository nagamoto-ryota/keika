// 訪問前の1枚: 書いた答えを自動で保存し、コピーにも入れる。
(function () {
  var data = document.getElementById("sheet-data");
  var url = data.dataset.url;
  var state = document.getElementById("note-state");
  var timers = new Map();
  var sending = new Map();

  function rows() { return document.querySelectorAll(".ask, .ask-row"); }

  function valueOf(row) {
    var checked = row.querySelector("input[type=radio]:checked");
    if (checked) return checked.value;
    var field = row.querySelector(".note, .note-line");
    return field ? field.value : "";
  }

  function send(row) {
    clearTimeout(timers.get(row));
    timers.delete(row);
    if (!url) return Promise.resolve();
    var body = new URLSearchParams({ section: row.dataset.section, label: row.dataset.label, answer: valueOf(row) });
    state.textContent = "保存しています";
    // 同じ項目は順番に送り、古い内容が後から上書きしないようにする
    var job = (sending.get(row) || Promise.resolve()).then(function () {
      return fetch(url, { method: "POST", body: body, keepalive: true });
    }).then(function (r) {
      if (!r.ok) throw new Error(r.status);
      if (sending.get(row) === job) sending.delete(row);
      state.textContent = "保存しました";
      state.classList.remove("note-failed");
    }).catch(function (e) {
      if (sending.get(row) === job) sending.delete(row);
      state.textContent = "保存できませんでした。書いた内容はまだ保存されていません";
      state.classList.add("note-failed");
      throw e;
    });
    sending.set(row, job);
    return job;
  }

  function later(row) {
    clearTimeout(timers.get(row));
    timers.set(row, setTimeout(function () { send(row).catch(function () {}); }, 800));
  }

  function flush() {
    var jobs = Array.from(timers.keys()).map(send).concat(Array.from(sending.values()));
    return Promise.all(jobs);
  }

  function grow(field) {
    field.style.height = "auto";
    field.style.height = field.scrollHeight + 2 + "px";
  }

  rows().forEach(function (row) {
    row.querySelectorAll(".note").forEach(function (field) {
      if (field.value) grow(field);
      field.addEventListener("input", function () { grow(field); later(row); });
      field.addEventListener("blur", function () { if (timers.has(row)) send(row).catch(function () {}); });
    });
    row.querySelectorAll(".note-line").forEach(function (field) {
      field.addEventListener("input", function () { later(row); });
      field.addEventListener("blur", function () { if (timers.has(row)) send(row).catch(function () {}); });
    });
    row.querySelectorAll("input[type=radio]").forEach(function (radio) {
      if (radio.checked) row.dataset.picked = radio.value;
      radio.addEventListener("click", function () {
        if (row.dataset.picked === radio.value) { // もう一度押すと外せる
          radio.checked = false;
          row.dataset.picked = "";
        } else {
          row.dataset.picked = radio.value;
        }
        send(row).catch(function () {});
      });
    });
  });

  document.querySelectorAll("form.flush-first").forEach(function (form) {
    form.addEventListener("submit", function (e) {
      e.preventDefault();
      var button = form.querySelector("button");
      flush().then(function () {
        if (form.classList.contains("busy")) {
          button.disabled = true;
          button.textContent = "作っています（1分ほど）";
        }
        form.submit();
      }).catch(function () {}); // 保存できない時は進まない。知らせは state に出ている
    });
  });

  window.addEventListener("pagehide", function () { timers.forEach(function (_, row) { send(row).catch(function () {}); }); });

  function sheetText() {
    var out = [data.dataset.head];
    var section = null;
    rows().forEach(function (row) {
      if (row.dataset.section !== section) {
        section = row.dataset.section;
        out.push("", "【" + section + "】");
      }
      out.push("□ " + row.dataset.label + (row.dataset.past ? "（前回: " + row.dataset.past + "）" : ""));
      out.push("→ " + valueOf(row));
    });
    return out.join("\n") + "\n";
  }
  window.sheetText = sheetText;

  document.getElementById("copy-sheet").addEventListener("click", function () {
    navigator.clipboard.writeText(sheetText());
    this.textContent = "コピーしました";
  });
})();
