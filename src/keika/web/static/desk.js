// 左に実物、右にAI相談。答えは裏で作るので、別の人へ移っても続く。一覧の印で終わりが分かる。
(function () {
  var desk = document.getElementById("desk");
  var thread = desk.dataset.thread;
  var log = document.getElementById("desk-log");
  var form = document.getElementById("desk-ask");
  var box = form.querySelector("textarea");
  var send = form.querySelector("button");
  var moveBox = document.getElementById("desk-move");
  var lastSeen = null;
  var firstLoad = true;

  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined) e.textContent = text;
    return e;
  }

  function showTab(name, stay) {
    document.querySelectorAll(".desk-tabs [data-tab]").forEach(function (b) { b.classList.toggle("on", b.dataset.tab === name); });
    document.querySelectorAll(".desk-pane").forEach(function (p) { p.hidden = p.id !== "pane-" + name; });
    if (!stay) desk.dataset.view = "real";
  }

  function clearMarks() {
    document.querySelectorAll(".desk-record .desk-body").forEach(function (b) {
      if (b.querySelector("mark")) b.textContent = b.textContent;
    });
  }

  function openSource(file, quote, date, stay) {
    var url = "/desk/" + thread + "/source?file=" + encodeURIComponent(file) + "&quote=" + encodeURIComponent(quote || "") + "&record_date=" + encodeURIComponent(date || "");
    fetch(url).then(function (r) { return r.ok ? r.json() : null; }).then(function (d) {
      if (!d) return;
      clearMarks();
      if (d.kind === "record") {
        showTab("records", stay);
        var rec = document.getElementById("rec-" + d.id);
        rec.querySelector(".desk-body").innerHTML = d.html;  // サーバーでエスケープ済み
        (rec.querySelector("mark") || rec).scrollIntoView({ block: "center" });
        return;
      }
      showTab("docs", stay);
      var body = document.getElementById("doc-body");
      var title = document.getElementById("doc-title");
      var open = document.getElementById("doc-open");
      var frame = document.getElementById("doc-frame");
      title.textContent = d.title;
      title.hidden = false;
      open.textContent = "";
      var a = el("a", "", "書類を開く");
      a.href = "/doc?path=" + encodeURIComponent(file);
      a.target = "_blank";
      a.rel = "noopener";
      open.appendChild(a);
      open.hidden = false;
      frame.hidden = d.kind !== "scan";
      body.hidden = d.kind === "scan";
      if (d.kind === "scan") {  // 文字で光らせられない書類は、実物をそのまま出す
        frame.src = "/doc?path=" + encodeURIComponent(file);
        return;
      }
      frame.removeAttribute("src");
      body.innerHTML = d.html;  // サーバーでエスケープ済み
      var mark = body.querySelector("mark");
      if (mark) mark.scrollIntoView({ block: "center" }); else title.scrollIntoView();
    });
  }

  var madeIds = null;
  function showMade(url, stay) {
    showTab("made", stay);
    var frame = document.getElementById("made-frame");
    frame.hidden = false;
    frame.src = url;
  }

  function renderMade(made) {
    var list = document.getElementById("made-list");
    if (!list) return;
    var ids = made.map(function (m) { return m.id; });
    var fresh = madeIds !== null ? made.filter(function (m) { return madeIds.indexOf(m.id) < 0; }) : [];
    if (madeIds === null || fresh.length) {
      document.getElementById("made-count").textContent = made.length;
      list.textContent = "";
      if (!made.length) list.appendChild(el("li", "muted", "ケイカに頼むと、作った下書きがここに開きます。"));
      made.forEach(function (m) {
        var li = el("li");
        var b = el("button", "linklike", m.title);
        b.type = "button";
        b.addEventListener("click", function () { showMade(m.url, false); });
        li.appendChild(b);
        list.appendChild(li);
      });
    }
    madeIds = ids;
    if (fresh.length) showMade(fresh[0].url, window.matchMedia("(max-width: 900px)").matches);
  }

  function citeLabel(c) {
    if (!c.quote) return c.file.split("/").pop();
    var q = c.quote.length > 24 ? c.quote.slice(0, 24) + "…" : c.quote;
    var name = c.record_date ? c.record_date.slice(5).replace("-", "/") : c.file.split("/").pop().replace(/\.[a-z]+$/, "");
    return name + "「" + q + "」";
  }

  function render(messages) {
    var key = JSON.stringify(messages.map(function (m) { return [m.id, m.status]; }));
    if (key === lastSeen) return;
    var hadWorking = lastSeen !== null && lastSeen.indexOf("working") >= 0;
    lastSeen = key;
    log.textContent = "";
    if (!messages.length) log.appendChild(el("p", "muted", "聞きたい事をそのまま書いてください。"));
    messages.forEach(function (m) {
      var row = el("div", "desk-msg " + m.role + (m.status === "failed" ? " failed" : ""));
      if (m.status === "working") {
        row.appendChild(el("p", "muted", "記録を読んでいます… 別の人へ移っても続きます。"));
      } else {
        row.appendChild(el("p", "desk-text", m.head));
        if (m.more) {
          var more = el("details", "desk-more");
          more.appendChild(el("summary", "small", "詳しく"));
          more.appendChild(el("p", "desk-text", m.more));
          row.appendChild(more);
        }
      }
      if (m.role === "assistant" && m.status === "done") {
        if (m.cites.length) {
          var fold = el("details", "desk-more");
          fold.appendChild(el("summary", "small", "根拠 " + m.cites.length + "件"));
          var list = el("p", "desk-cites");
          m.cites.forEach(function (c) {
            var b = el("button", "linklike small", citeLabel(c));
            b.type = "button";
            b.title = c.file;
            b.addEventListener("click", function () { openSource(c.file, c.quote, c.record_date, false); });
            list.appendChild(b);
            if (c.unverified) list.appendChild(el("span", "small muted", "（スキャンのため未照合）"));
          });
          fold.appendChild(list);
          row.appendChild(fold);
        } else if (thread !== "all" && m.text.length > 60) {
          row.appendChild(el("p", "small muted", "この答えに根拠の引用はありません。"));
        }
      }
      if (m.role === "assistant" && m.status === "done" && thread !== "all") {
        var toRecord = el("button", "linklike small", "記録の入力欄へ送る");
        toRecord.type = "button";
        toRecord.addEventListener("click", function () {
          try { sessionStorage.setItem("keika_prefill", (m.head + "\n" + m.more).trim()); } catch (e) { return; }
          location.href = "/records/new";
        });
        row.appendChild(toRecord);
      }
      log.appendChild(row);
    });
    var lastRow = log.lastElementChild;
    log.scrollTop = lastRow ? lastRow.offsetTop - log.offsetTop : 0;  // 答えの先頭から読めるように
    var working = messages.some(function (m) { return m.status === "working"; });
    send.disabled = working || box.disabled;
    if ((hadWorking || firstLoad) && !working && messages.length) {  // 一番関係する根拠を左に光らせておく
      var last = messages[messages.length - 1];
      if (last.cites && last.cites.length) {  // 文字で確かめられた根拠を先に開く
        var c = last.cites.filter(function (x) { return !x.unverified; })[0] || last.cites[0];
        openSource(c.file, c.quote, c.record_date, window.matchMedia("(max-width: 900px)").matches);
      }
    }
    firstLoad = false;
  }

  function marks(state) {
    document.querySelectorAll(".desk-people a").forEach(function (a) {
      var s = state[a.dataset.thread];
      var mark = a.querySelector(".desk-mark");
      mark.textContent = s === "working" ? "考え中" : s === "new" ? "●" : "";
      mark.className = "desk-mark " + (s || "");
    });
  }

  function poll() {
    return fetch("/desk/" + thread + "/messages").then(function (r) { return r.json(); }).then(function (d) {
      render(d.messages);
      renderMade(d.made || []);
      marks(d.threads);
    }).catch(function () {});
  }

  function ask(question, stay) {
    var data = new FormData();
    data.append("question", question);
    if (stay) data.append("stay", "1");
    return fetch("/desk/" + thread + "/ask", { method: "POST", body: data }).then(function (r) { return r.json().then(function (d) { return [r.status, d]; }); }).then(function (res) {
      if (res[0] === 409) {
        moveBox.hidden = false;
        moveBox.textContent = res[1].detail || "答えを作っている途中です。";
        poll();
        return;
      }
      var d = res[1];
      if (d.status === "move") { offerMove(question, d.people); return; }
      box.value = "";
      moveBox.hidden = true;
      poll();
    });
  }

  function offerMove(question, people) {
    moveBox.textContent = "";
    moveBox.hidden = false;
    var names = people.map(function (p) { return p.name + "さん"; }).join("、");
    moveBox.appendChild(el("p", "", names + "の話ですか。この枠では" + names + "の記録は読めません。"));
    people.forEach(function (p) {
      var b = el("button", "", p.name + "さんの枠へ移る");
      b.type = "button";
      b.addEventListener("click", function () {
        var data = new FormData();
        data.append("question", question);
        data.append("stay", "1");
        fetch("/desk/" + p.id + "/ask", { method: "POST", body: data }).then(function () { location.href = "/desk?t=" + p.id; });
      });
      moveBox.appendChild(b);
    });
    var stay = el("button", "plain", "ここで続ける");
    stay.type = "button";
    stay.addEventListener("click", function () { ask(question, true); });
    moveBox.appendChild(stay);
  }

  form.addEventListener("submit", function (e) {
    e.preventDefault();
    if (box.value.trim()) ask(box.value.trim(), false);
  });
  box.addEventListener("keydown", function (e) {
    if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) form.requestSubmit();
  });

  document.querySelectorAll(".desk-tabs [data-tab]").forEach(function (b) {
    b.addEventListener("click", function () { showTab(b.dataset.tab); });
  });
  document.querySelectorAll(".desk-docs [data-file]").forEach(function (b) {
    b.addEventListener("click", function () { openSource(b.dataset.file, "", ""); });
  });
  document.querySelectorAll(".desk-views [data-show]").forEach(function (b) {
    b.addEventListener("click", function () { desk.dataset.view = b.dataset.show; });
  });

  poll();
  setInterval(poll, 2000);
})();
