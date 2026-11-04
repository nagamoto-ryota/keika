// 記録の確認。右の文を直すたびに、左右の印を機械で付け直す。保存は今までどおりフォームで送る。
(function () {
  function paint(box, spans, cls, tail) {
    box.textContent = "";
    spans.forEach(function (s) {
      if (s[1]) {
        var m = document.createElement("mark");
        m.className = cls;
        m.textContent = s[0];
        box.appendChild(m);
      } else {
        box.appendChild(document.createTextNode(s[0]));
      }
    });
    if (tail) box.appendChild(document.createTextNode("\u200b"));  // 最後の改行の分の高さを保つ
  }

  document.querySelectorAll(".compare-card").forEach(function (card) {
    var left = card.querySelector(".compare-left");
    var mirror = card.querySelector(".compare-mirror");
    var box = card.querySelector("textarea");
    var legend = card.querySelector(".compare-legend");
    var timer = null;
    var turn = 0;

    function refresh() {
      var mine = ++turn;
      var form = new FormData();
      form.append("body", box.value);
      fetch(card.dataset.compare, { method: "POST", body: form }).then(function (r) {
        if (!r.ok) throw new Error("compare");
        return r.json();
      }).then(function (c) {
        if (mine !== turn) return;  // もっと新しい文を送っている
        paint(left, c.left, "diff-old", false);
        paint(mirror, c.right, "diff-new", true);
        legend.textContent = c.legend;
      }).catch(function () {
        if (mine !== turn) return;
        paint(left, [[left.textContent, false]], "diff-old", false);
        legend.textContent = "印を付け直せませんでした。文はそのまま保存できます。";
      });
    }

    box.addEventListener("input", function () {
      paint(mirror, [[box.value, false]], "diff-new", true);  // 古い印が別の文字に重ならないように、いったん消す
      clearTimeout(timer);
      timer = setTimeout(refresh, 250);
    });

    // 表の欄で打ち直すと、記録の文の同じ書き方をまとめて置き換える
    var inputs = Array.prototype.slice.call(card.querySelectorAll(".judge-input"));
    function lockMissing() {  // 全文の方で直された語は、もう置き換えられない
      inputs.forEach(function (input) {
        input.readOnly = !input.dataset.current || box.value.indexOf(input.dataset.current) < 0;
      });
    }
    inputs.forEach(function (input) {
      input.dataset.current = input.value;
      input.addEventListener("keydown", function (e) {
        if (e.key === "Enter") {  // Enter で記録してしまわないように
          e.preventDefault();
          input.blur();
        }
      });
      input.addEventListener("change", function () {
        if (input.readOnly) return;
        box.value = box.value.split(input.dataset.current).join(input.value);
        input.dataset.current = input.value;
        box.dispatchEvent(new Event("input"));
      });
    });
    box.addEventListener("input", lockMissing);
    if (box.value !== mirror.textContent.replace(/\u200b$/, "")) {  // ブラウザが打った文を復元した時
      paint(mirror, [[box.value, false]], "diff-new", true);
      refresh();
    }
  });
})();
